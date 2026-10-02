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
    if not seen:
        results.append(('Telegram', False, 'в канале сообщения не видно', url))
        return
    # вертикаль: без width/height Telegram рисует квадрат (padding-top:100%), нормальный ролик — выше 100%
    import re
    code, emb = http(url + '?embed=1')
    m = re.search(rb'message_video_wrap"[^>]*padding-top:([0-9.]+)%', emb)
    square = m is not None and float(m.group(1)) <= 100.5
    results.append(('Telegram', not square, 'вышло, но КВАДРАТОМ — надо перезалить с размерами' if square else 'вышло', url))


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


# ---------- просмотры: по всем вышедшим роликам, ключ — первая строка подписи ----------
VIEWS = 'views.json'
# подпись → название ролика (как папка в Видео/); пополняется при постановке выпуска
TITLES = json.load(open('titles.json')) if os.path.exists('titles.json') else {}


def num(text):
    """«1.2K» / «12,3 тыс» / «950» → int."""
    t = text.strip().replace(',', '.').replace('\xa0', '').replace(' ', '').upper()
    mult = 1000 if ('K' in t or 'ТЫС' in t) else 1000000 if ('M' in t or 'МЛН' in t) else 1
    digits = ''.join(ch for ch in t if ch.isdigit() or ch == '.')
    return int(float(digits) * mult) if digits else 0


def live_views(platform, url):
    """Свежие просмотры со страницы площадки (Buffer отдаёт их с опозданием на сутки)."""
    import re
    if not url:
        return None
    if platform == 'YouTube':
        vid = url.rstrip('/').split('/')[-1].split('?')[0]
        code, body = http('https://www.youtube.com/watch?v=' + vid, headers={'Accept-Language': 'en'})
        m = re.search(rb'"viewCount":"(\d+)"', body)
    elif platform == 'TikTok':
        code, body = http(url.split('?')[0].replace('://tiktok.com', '://www.tiktok.com'))
        m = re.search(rb'"playCount":(\d+)', body)
    else:
        return None
    return int(m.group(1)) if m else None


def collect_views(now):
    import re
    items = {}

    def add(caption, when, platform, n):
        if n is None or not caption:
            return
        key = ' '.join(caption.lower().split())[:24]  # TikTok и др. склеивают строки — сравниваем начало
        it = items.setdefault(key, {'date': when.astimezone(MSK).date().isoformat(), 'platforms': {}})
        it['title'] = TITLES.get(key) or it.get('title') or caption.strip().split('\n')[0][:40]
        it['date'] = min(it['date'], when.astimezone(MSK).date().isoformat())
        it['platforms'][platform] = max(it['platforms'].get(platform, 0), n)

    start = now - datetime.timedelta(days=90)
    q = '{ posts(first: 100, input: {organizationId: "%s", filter: {dueAt: {start: "%s", end: "%s"}}}) ' \
        '{ edges { node { status dueAt text channelId externalLink metrics { type value } } } } }' % (ORG, iso(start), iso(now))
    for e in buffer(q)['posts']['edges']:
        p = e['node']
        if p['status'] != 'sent':
            continue
        name = BUFFER_CHANNELS.get(p['channelId'])
        n = live_views(name, p['externalLink'])
        if n is None:
            n = next((int(m['value']) for m in (p.get('metrics') or []) if m['type'] == 'views'), None)
        add(p['text'], parse(p['dueAt']), name, n)

    # Zernio: список /analytics отдаёт и посты с отключёнными аккаунтами (в /posts их нет)
    for p in zernio('/analytics?limit=100').get('posts', []):
        name = ZERNIO_PLATFORMS.get(p.get('platform'))
        if name and p.get('status') == 'published':
            add(p.get('content', ''), parse(p.get('publishedAt') or p['scheduledFor']), name, (p.get('analytics') or {}).get('views'))

    code, body = http('https://t.me/s/' + PUBLIC_CHANNEL)
    tg = {int(m.group(1)): num(m.group(2).decode()) for m in re.finditer(
        rb'data-post="%s/(\d+)".*?tgme_widget_message_views">([^<]+)<' % PUBLIC_CHANNEL.encode(), body, re.S)}
    for i in json.load(open('telegram-queue.json')):
        if 'video' in i and i.get('sent') and isinstance(i.get('message_id'), int) and i['message_id'] in tg:
            add(i.get('caption', ''), parse(i['at']), 'Telegram', tg[i['message_id']])

    merged = {}  # один ролик мог выйти с разными подписями (Скорпионс руками) — склеиваем по названию
    for it in items.values():
        m = merged.setdefault(it['title'], {'title': it['title'], 'date': it['date'], 'platforms': {}})
        m['date'] = min(m['date'], it['date'])
        for k, v in it['platforms'].items():
            m['platforms'][k] = max(m['platforms'].get(k, 0), v)
    items = merged
    old = json.load(open(VIEWS)) if os.path.exists(VIEWS) else {}
    for key, it in items.items():
        it['total'] = sum(it['platforms'].values())
        prev = (old.get('items') or {}).get(key, {})
        # прирост считаем к прошлому дню, а не к прошлому запуску (запусков в день бывает несколько)
        base = prev.get('day_start_total') if prev.get('day') == now.date().isoformat() else prev.get('total')
        it['day'] = now.date().isoformat()
        it['day_start_total'] = base if base is not None else it['total']
        it['delta'] = it['total'] - it['day_start_total']
    data = {'updated': now.isoformat(timespec='minutes'), 'items': items}
    if not DRY:
        json.dump(data, open(VIEWS, 'w'), ensure_ascii=False, indent=2)
    return data


def views_lines(data, limit=10):
    ranked = sorted(data['items'].items(), key=lambda kv: -kv[1]['total'])[:limit]
    total = sum(it['total'] for it in data['items'].values())
    delta = sum(it['delta'] for it in data['items'].values())
    out = ['👀 Всего просмотров: %s%s' % (format(total, ',').replace(',', ' '),
                                        (' (+%s за сутки)' % format(delta, ',').replace(',', ' ')) if delta > 0 else ''),
           '', 'По роликам:']
    for key, it in ranked:
        d = datetime.date.fromisoformat(it['date'])
        out.append('%s %s — %s%s' % (d.strftime('%d.%m'), it['title'], format(it['total'], ',').replace(',', ' '),
                                     (' (+%s)' % format(it['delta'], ',').replace(',', ' ')) if it['delta'] > 0 else ''))
    return out


def main():
    now = datetime.datetime.now(MSK)
    today = datetime.date.fromisoformat(os.environ['CHECK_DATE']) if os.environ.get('CHECK_DATE') else now.date()
    if not os.environ.get('CHECK_DATE') and now.hour < 19:
        # до 19:00 сегодняшний ролик ещё не выходил: запоздавший запуск (GitHub бывает опаздывает
        # на часы) проверяет вчерашний выпуск, а не пугает крестиками по сегодняшнему
        today = now.date() - datetime.timedelta(days=1)
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
    try:
        lines += [''] + views_lines(collect_views(now))
    except Exception as e:
        lines += ['', 'Просмотры собрать не вышло: %s' % e]
    if bad or crashes:
        lines.append('')
        lines.append('Напиши Клоду в чат завода рилсов — разберёт и перевыложит.')
    loud = bool(bad or crashes or len(ahead) < QUEUE_DAYS_MIN)
    send('\n'.join(lines), loud)
    # ручной запуск днём не должен отменять вечернюю проверку: отмечаем только проверку после 20:00
    if not DRY and now >= datetime.datetime.combine(today, datetime.time(20), MSK):
        log['last_checked'] = today.isoformat()
        json.dump(log, open(LOG, 'w'), ensure_ascii=False, indent=2)


if __name__ == '__main__':
    main()
