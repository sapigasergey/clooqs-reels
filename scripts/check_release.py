"""Вечерняя проверка выпуска: вышел ли сегодняшний рилс на всех площадках.

Запускается GitHub Actions (check.yml) в ~21:00 МСК. Смотрит:
  Buffer  — Instagram, TikTok, YouTube: пост на сегодня, статус, живая ссылка;
  Zernio  — Facebook, Threads: то же;
  Telegram — сегодняшняя запись telegram-queue.json отправлена и видна на t.me/s/clooqs;
  очередь — на сколько дней вперёд что-то запланировано.
Итог — сообщение в пульт «Клукс · выпуск». Если всё вышло — тихое, если нет — со звуком и причинами.
Секреты: BUFFER_API_KEY, ZERNIO_API_KEY, TELEGRAM_BOT_TOKEN.
Запуск с --dry печатает сообщение вместо отправки.
"""
import datetime, json, os, sys, time, urllib.parse, urllib.request

MSK = datetime.timezone(datetime.timedelta(hours=3))
ORG = '6a9e818c0cf2fa2884234d28'
BUFFER_CHANNELS = {
    '6a9e86aacd8b9c702c20fb14': 'Instagram',
    '6aaeeb92ea19ca0bde8c9e39': 'TikTok',
    '6a9eb0b1cd8b9c702c21caed': 'YouTube',
}
ZERNIO_PLATFORMS = {'facebook': 'Facebook', 'threads': 'Threads'}
CONTROL_CHAT = '-1004305182424'
PUBLIC_CHANNEL = 'clooqs'
QUEUE_DAYS_MIN = 3
LOG = 'check-log.json'
DRY = '--dry' in sys.argv
UA = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/128 Safari/537.36'}


def http(url, data=None, headers=None, timeout=60):
    err = b''
    for attempt in range(3):  # сетевые сбои повторяем
        req = urllib.request.Request(url, data=data, headers={**UA, **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except Exception as e:  # сеть, таймаут
            err = str(e).encode()
            time.sleep(10)
    return 0, err


def buffer(query):
    code, body = http('https://api.buffer.com', json.dumps({'query': query}).encode(),
                      {'Authorization': 'Bearer ' + os.environ['BUFFER_API_KEY'], 'Content-Type': 'application/json'})
    d = json.loads(body)
    if 'errors' in d:
        raise RuntimeError('Buffer: ' + d['errors'][0].get('message', '?'))
    return d['data']


def zernio(path):
    code, body = http('https://zernio.com/api/v1' + path, headers={'Authorization': 'Bearer ' + os.environ['ZERNIO_API_KEY']})
    if code != 200:
        raise RuntimeError('Zernio %s: HTTP %s %s' % (path, code, body[:120].decode(errors='replace')))
    return json.loads(body)


def link_alive(platform, url):
    """Проверяет ссылку снаружи, без входа. None — жива, иначе текст проблемы.
    Instagram, Facebook и Threads чужих без входа не пускают — там верим статусу сервиса."""
    if not url:
        return 'нет ссылки на пост'
    if platform == 'YouTube':
        check = 'https://www.youtube.com/oembed?format=json&url=' + urllib.parse.quote(url, safe='')
    elif platform == 'TikTok':
        url = url.replace('://tiktok.com', '://www.tiktok.com')
        check = 'https://www.tiktok.com/oembed?url=' + urllib.parse.quote(url.split('?')[0], safe='')
    else:
        return None
    code = 0
    for _ in range(3):  # на свежий пост площадка иногда отвечает ошибкой
        code, _body = http(check)
        if code == 200:
            return None
        time.sleep(20)
    return 'сервис говорит «вышло», но ролик снаружи не открывается (HTTP %s)' % code


def day_bounds(day):
    start = datetime.datetime.combine(day, datetime.time(0), MSK)
    return start, start + datetime.timedelta(days=1)


def iso(dt):
    return dt.astimezone(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def parse(ts):
    return datetime.datetime.fromisoformat(ts.replace('Z', '+00:00'))


def check_buffer(today, results, future_days):
    start, end = day_bounds(today)
    far = end + datetime.timedelta(days=14)
    q = '{ posts(first: 100, input: {organizationId: "%s", filter: {dueAt: {start: "%s", end: "%s"}}}) ' \
        '{ edges { node { id channelId status dueAt externalLink error { message } } } } }' % (ORG, iso(start), iso(far))
    posts = [e['node'] for e in buffer(q)['posts']['edges']]
    for cid, name in BUFFER_CHANNELS.items():
        mine = [p for p in posts if p['channelId'] == cid and parse(p['dueAt']) < end]
        if not mine:
            results.append((name, False, 'на сегодня поста нет', None))
            continue
        mine.sort(key=lambda p: p['dueAt'])  # сегодняшний выпуск — самый поздний пост дня
        sent = [p for p in mine if p['status'] == 'sent']
        if sent:
            url = sent[-1]['externalLink']
            problem = link_alive(name, url)
            results.append((name, problem is None, problem or 'вышло', url))
        else:
            p = mine[-1]
            why = (p.get('error') or {}).get('message') or 'статус Buffer: %s' % p['status']
            results.append((name, False, why, None))
    for p in posts:
        if parse(p['dueAt']) >= end and p['status'] in ('scheduled', 'draft', 'needs_approval', 'sending'):
            future_days.add(parse(p['dueAt']).astimezone(MSK).date())


def check_zernio(today, results, future_days):
    start, end = day_bounds(today)
    posts = zernio('/posts?limit=50').get('posts', [])
    found = {}
    for p in posts:
        when = parse(p.get('scheduledFor') or p.get('publishedAt') or p.get('createdAt'))
        if when >= end and p.get('status') in ('scheduled', 'pending'):
            future_days.add(when.astimezone(MSK).date())
        if not (start <= when < end):
            continue
        for pl in p.get('platforms', []):
            found.setdefault(pl.get('platform'), []).append(pl)
    for key, name in ZERNIO_PLATFORMS.items():
        items = found.get(key)
        if not items:
            results.append((name, False, 'на сегодня поста нет', None))
            continue
        ok = [x for x in items if x.get('status') == 'published']
        if ok:
            url = ok[-1].get('platformPostUrl')
            problem = link_alive(name, url)
            results.append((name, problem is None, problem or 'вышло', url))
        else:
            x = items[-1]
            why = x.get('errorMessage') or 'статус Zernio: %s' % x.get('status')
            results.append((name, False, why, None))


def check_telegram(today, results, future_days):
    start, end = day_bounds(today)
    queue = json.load(open('telegram-queue.json'))
    videos = [i for i in queue if 'video' in i and i.get('chat', '@' + PUBLIC_CHANNEL) == '@' + PUBLIC_CHANNEL]
    for i in videos:
        if parse(i['at']) >= end and not i.get('sent'):
            future_days.add(parse(i['at']).astimezone(MSK).date())
    todays = [i for i in videos if start <= parse(i['at']) < end]
    if not todays:
        results.append(('Telegram', False, 'на сегодня в очереди бота ничего нет', None))
        return
    i = todays[-1]
    if not i.get('sent'):
        why = i.get('last_error') or 'бот ещё не отправил (GitHub не запустил очередь?)'
        results.append(('Telegram', False, why, None))
        return
    url = 'https://t.me/%s/%s' % (PUBLIC_CHANNEL, i['message_id'])
    code, body = http('https://t.me/s/' + PUBLIC_CHANNEL)
    seen = ('data-post="%s/%s"' % (PUBLIC_CHANNEL, i['message_id'])).encode() in body
    results.append(('Telegram', seen, 'вышло' if seen else 'в канале сообщения не видно', url))


def send(text, loud):
    if DRY:
        print(text)
        return
    api = 'https://api.telegram.org/bot%s/sendMessage' % os.environ['TELEGRAM_BOT_TOKEN']
    fields = {'chat_id': CONTROL_CHAT, 'text': text, 'disable_web_page_preview': 'true',
              'disable_notification': 'false' if loud else 'true'}
    code, body = http(api, urllib.parse.urlencode(fields).encode())
    if code != 200:
        print('Telegram не принял сообщение:', body[:300], file=sys.stderr)
        sys.exit(1)


def main():
    now = datetime.datetime.now(MSK)
    today = datetime.date.fromisoformat(os.environ['CHECK_DATE']) if os.environ.get('CHECK_DATE') else now.date()
    log = json.load(open(LOG)) if os.path.exists(LOG) else {}
    if log.get('last_checked') == today.isoformat() and '--force' not in sys.argv and not DRY:
        print('сегодня уже проверяли')  # второй запуск cron — страховка, если первый GitHub пропустил
        return
    results, future_days, crashes = [], set(), []
    for name, fn in (('Buffer', check_buffer), ('Zernio', check_zernio), ('Telegram', check_telegram)):
        try:
            fn(today, results, future_days)
        except Exception as e:
            crashes.append('%s: проверка не прошла — %s' % (name, e))

    bad = [r for r in results if not r[1]]
    nothing = results and all(r[2].startswith('на сегодня') for r in results)
    lines = []
    if nothing and not crashes:
        lines.append('⚠️ %s: сегодня рилс не выходил — ни на одной площадке нет поста.' % today.strftime('%d.%m'))
    elif bad or crashes:
        lines.append('⚠️ %s: рилс вышел не везде' % today.strftime('%d.%m'))
    else:
        lines.append('✅ %s: рилс вышел везде' % today.strftime('%d.%m'))
    if not nothing:
        lines.append('')
        for name, ok, note, url in results:
            lines.append('%s %s — %s%s' % ('✅' if ok else '❌', name, note, (' ' + url) if url else ''))
    lines += [''] + crashes if crashes else []
    lines.append('')
    lines.append('VK ставишь ты — проверь клип: https://vk.com/clips-45457184')
    ahead = sorted(d for d in future_days if d > today)
    if len(ahead) < QUEUE_DAYS_MIN:
        lines.append('')
        lines.append('📭 В очереди дней: %d%s. Нужно минимум %d — пора ставить новые ролики.' % (
            len(ahead), (' (' + ', '.join(d.strftime('%d.%m') for d in ahead) + ')') if ahead else '', QUEUE_DAYS_MIN))
    if bad or crashes:
        lines.append('')
        lines.append('Напиши Клоду в чат завода рилсов — разберёт и перевыложит.')
    loud = bool(bad or crashes or len(ahead) < QUEUE_DAYS_MIN)
    send('\n'.join(lines), loud)
    # ручной запуск днём не должен отменять вечернюю проверку: отмечаем только проверку после 20:00
    if not DRY and now.hour >= 20 and today == now.date():
        log['last_checked'] = today.isoformat()
        json.dump(log, open(LOG, 'w'), ensure_ascii=False, indent=2)


if __name__ == '__main__':
    main()
