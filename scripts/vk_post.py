"""VK: выкладывает клипы из vk-queue.json в сообщество «Клукс» и собирает просмотры клипов.

Запускается НА МАКЕ (launchd ~/Library/LaunchAgents/com.clooqs.reels-vk.*.plist):
ключ VK живёт час, а refresh_token меняется при каждом обновлении, поэтому ключ хранится
на маке (~/Development/ClooqsWork/vk/token.json, права 600) и в GitHub не уходит.

Почему не отложка: клип виден в разделе «Клипы» с момента загрузки, отложенный пост этого
не меняет, скрытый ролик клипом не становится (проверено 05.11.2026). Поэтому загружаем ровно
во время выхода, сразу с постом на стене (решение Сергея 05.11).

  python3 scripts/vk_post.py post    — выложить записи, у которых подошло время
  python3 scripts/vk_post.py views   — просмотры всех клипов сообщества → vk-views.json
  --dry  — ничего не выкладывать и не коммитить, только показать

Запись очереди: {"at": "2026-11-10T19:00:00+03:00", "video": "https://…/r/….mp4",
                 "title": "Название клипа", "caption": "текст поста и описание клипа"}
После выхода в запись дописываются video_id, post_id, sent_at; ошибка — last_error.
Запись старше 3 часов не выкладывается (мак спал) — пишется last_error, вечерняя проверка покажет ❌.
"""
import datetime, fcntl, json, os, secrets, subprocess, sys, time, urllib.error, urllib.parse, urllib.request, uuid

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN = os.path.expanduser('~/Development/ClooqsWork/vk/token.json')
CLIENT_ID = '54796595'
GROUP = 45457184
TEST_QUEUE = os.environ.get('VK_QUEUE')  # проверка на отдельном файле: без git
QUEUE = TEST_QUEUE or os.path.join(REPO, 'vk-queue.json')
VIEWS = os.path.join(REPO, 'vk-views.json')
LATE = datetime.timedelta(hours=3)
DRY = '--dry' in sys.argv


def log(*a):
    print(datetime.datetime.now().strftime('%F %T'), *a, flush=True)


def git(*args):
    return subprocess.run(['git', '-C', REPO] + list(args), capture_output=True, text=True)


def access_token():
    """Обновляет ключ (access живёт час). Новый refresh_token сразу пишется на диск атомарно."""
    t = json.load(open(TOKEN))
    if time.time() < t.get('got_at', 0) + int(t.get('expires_in', 3600)) - 300:
        return t['access_token']
    d = dict(grant_type='refresh_token', refresh_token=t['refresh_token'], client_id=CLIENT_ID,
             device_id=t['device_id'], state=secrets.token_urlsafe(16))
    try:
        r = json.load(urllib.request.urlopen(urllib.request.Request(
            'https://id.vk.ru/oauth2/auth', urllib.parse.urlencode(d).encode()), timeout=60))
    except urllib.error.HTTPError as e:
        r = json.loads(e.read() or b'{}')
    if 'access_token' not in r:
        raise RuntimeError('ключ VK не обновился (%s) — нужен новый вход Сергея в VK ID' % (r.get('error_description') or r.get('error')))
    t.update({k: v for k, v in r.items() if k in ('access_token', 'refresh_token', 'expires_in', 'scope', 'id_token')})
    t['got_at'] = int(time.time())
    tmp = TOKEN + '.tmp'
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w') as f:
        json.dump(t, f)
    os.replace(tmp, TOKEN)
    return t['access_token']


def api(method, **p):
    p.update(access_token=access_token(), v='5.199')
    r = json.load(urllib.request.urlopen(urllib.request.Request(
        'https://api.vk.ru/method/' + method, urllib.parse.urlencode(p).encode()), timeout=120))
    if 'error' in r:
        raise RuntimeError('%s: %s' % (method, r['error'].get('error_msg')))
    return r['response']


def sync():
    """Свежая очередь с GitHub (её правит Клод при постановке выпуска)."""
    if TEST_QUEUE:
        return
    r = git('pull', '--rebase', '--autostash', '-q', 'origin', 'main')
    if r.returncode:
        log('git pull не прошёл:', r.stderr.strip()[:200])


def push(files, message):
    if DRY or TEST_QUEUE:
        return
    git('add', *files)
    if not git('diff', '--cached', '--quiet').returncode:
        return
    git('-c', 'user.name=clooqs-mac', '-c', 'user.email=mac@clooqs.invalid', 'commit', '-q', '-m', message)
    for _ in range(3):
        if not git('push', '-q', 'origin', 'HEAD:main').returncode:
            return
        git('pull', '--rebase', '-q', 'origin', 'main')
    log('git push не прошёл — запишется при следующем запуске')


def save_queue(queue):
    if not DRY:
        json.dump(queue, open(QUEUE, 'w'), ensure_ascii=False, indent=2)


def upload(item):
    data = urllib.request.urlopen(item['video'], timeout=300).read()
    s = api('video.save', group_id=GROUP, name=item['title'], description=item.get('caption', ''),
            is_private=0, wallpost=0, repeat=1)
    b = uuid.uuid4().hex
    body = ('--%s\r\nContent-Disposition: form-data; name="video_file"; filename="clip.mp4"\r\n'
            'Content-Type: video/mp4\r\n\r\n' % b).encode() + data + ('\r\n--%s--\r\n' % b).encode()
    json.load(urllib.request.urlopen(urllib.request.Request(
        s['upload_url'], body, {'Content-Type': 'multipart/form-data; boundary=' + b}), timeout=600))
    return s['video_id'], s.get('access_key', '')


def wait_clip(video_id, key):
    """VK обрабатывает ролик секунды-минуты; клипом (short_video) он становится после обработки."""
    for _ in range(60):
        v = api('video.get', videos='-%d_%s_%s' % (GROUP, video_id, key), owner_id=-GROUP)['items'][0]
        if not v.get('processing') and v.get('type'):
            return v.get('type')
        time.sleep(10)
    return 'processing'


def post_due():
    sync()
    queue = json.load(open(QUEUE)) if os.path.exists(QUEUE) else []
    now = datetime.datetime.now(datetime.timezone.utc)
    for item in queue:
        if item.get('post_id') or datetime.datetime.fromisoformat(item['at']) > now:
            continue
        if not item.get('video_id') and now - datetime.datetime.fromisoformat(item['at']) > LATE:
            if not item.get('last_error'):
                item['last_error'] = 'не выложен: мак не запустил вовремя (опоздание больше 3 часов)'
                save_queue(queue)
            continue
        log('выкладываю', item['title'])
        if DRY:
            continue
        try:
            if not item.get('video_id'):  # загружен, но пост не вышел — второй раз не грузим
                item['video_id'], item['access_key'] = upload(item)
                save_queue(queue)
            kind = wait_clip(item['video_id'], item.get('access_key', ''))
            item['type'] = kind
            p = api('wall.post', owner_id=-GROUP, from_group=1, message=item.get('caption', ''),
                    attachments='video-%d_%s' % (GROUP, item['video_id']))
            item['post_id'] = p['post_id']
            item['sent_at'] = now.isoformat()
            item['link'] = 'https://vk.com/clip-%d_%s' % (GROUP, item['video_id'])
            item.pop('last_error', None)
            if kind != 'short_video':
                item['last_error'] = 'вышло, но VK записал как %s, а не клип' % kind
            log('вышло', item['link'], 'пост', p['post_id'], kind)
        except Exception as e:
            item['last_error'] = str(e)[:300]
            log('ОШИБКА', e)
        save_queue(queue)
    push(['vk-queue.json'], 'VK: выпуск')


def views():
    """Просмотры клипов по постам стены сообщества (video.get без ключа ручных клипов их не отдаёт)."""
    sync()
    items, offset = [], 0
    while offset < 300:
        r = api('wall.get', owner_id=-GROUP, count=100, offset=offset)
        for x in r['items']:
            for a in x.get('attachments', []):
                v = a.get('video')
                if v and v.get('type') == 'short_video':
                    items.append({'video_id': v['id'], 'post_id': x['id'],
                                  'date': datetime.datetime.fromtimestamp(x['date'], datetime.timezone.utc).isoformat(),
                                  'caption': x.get('text') or v.get('description') or '', 'views': v.get('views', 0)})
        offset += 100
        if offset >= r['count']:
            break
    data = {'updated': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='minutes'), 'clips': items}
    log('клипов:', len(items), 'просмотров:', sum(i['views'] for i in items))
    if not DRY:
        json.dump(data, open(VIEWS, 'w'), ensure_ascii=False, indent=2)
        push(['vk-views.json'], 'VK: просмотры')


if __name__ == '__main__':
    lock = open('/tmp/clooqs-vk.lock', 'w')
    fcntl.flock(lock, fcntl.LOCK_EX)  # пост и просмотры не идут одновременно
    try:
        {'post': post_due, 'views': views}[sys.argv[1]]()
    except Exception as e:
        log('ОШИБКА', e)
        sys.exit(1)
