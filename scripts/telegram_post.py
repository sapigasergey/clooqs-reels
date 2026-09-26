"""Отправляет в Telegram записи из telegram-queue.json, у которых подошло время.

Запускается GitHub Actions по расписанию (и вручную). Токен — секрет TELEGRAM_BOT_TOKEN.
Запись очереди:
  {"at": "2026-09-27T18:40:00+03:00", "chat": "-100…" (по умолчанию @clooqs),
   "video": "https://…mp4", "caption": "…"}                       — ролик в канал
  {"at": "…", "chat": "-100…", "messages": [                          — пакет сообщений по порядку
      {"text": "…"}, {"document": "https://…mp4", "name": "x.mp4"}, {"text": "…"}]}
Отправленные помечаются sent=true (+ message_id), файл коммитится обратно.
Файлы качаются и загружаются multipart (лимит бота 50 МБ; по URL Telegram берёт только до 20 МБ).
"""
import datetime, json, os, sys, urllib.parse, urllib.request, uuid

TOKEN = os.environ['TELEGRAM_BOT_TOKEN']
DEFAULT_CHAT = os.environ.get('TELEGRAM_CHAT', '@clooqs')
QUEUE = 'telegram-queue.json'
API = 'https://api.telegram.org/bot%s/' % TOKEN


def call(method, fields, files=None):
    if files:
        boundary = uuid.uuid4().hex
        body = b''
        for k, v in fields.items():
            body += ('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n' % (boundary, k, v)).encode()
        for k, (name, data) in files.items():
            body += ('--%s\r\nContent-Disposition: form-data; name="%s"; filename="%s"\r\nContent-Type: application/octet-stream\r\n\r\n' % (boundary, k, name)).encode() + data + b'\r\n'
        body += ('--%s--\r\n' % boundary).encode()
        req = urllib.request.Request(API + method, data=body, headers={'Content-Type': 'multipart/form-data; boundary=' + boundary})
    else:
        req = urllib.request.Request(API + method, data=urllib.parse.urlencode(fields).encode())
    try:
        return json.load(urllib.request.urlopen(req, timeout=300))
    except urllib.error.HTTPError as e:
        return json.loads(e.read() or b'{}')


def fetch(url):
    return urllib.request.urlopen(url, timeout=300).read()


def send_video(chat, url, caption):
    return call('sendVideo', {'chat_id': chat, 'caption': caption, 'supports_streaming': 'true'},
                {'video': (url.rsplit('/', 1)[-1], fetch(url))})


queue = json.load(open(QUEUE))
now = datetime.datetime.now(datetime.timezone.utc)
changed = False
for item in queue:
    if item.get('sent') or datetime.datetime.fromisoformat(item['at']) > now:
        continue
    chat = item.get('chat', DEFAULT_CHAT)
    ok, ids, err = True, [], None
    if 'messages' in item:
        for m in item['messages']:
            if 'text' in m:
                r = call('sendMessage', {'chat_id': chat, 'text': m['text'], 'disable_web_page_preview': 'true'})
            else:
                r = call('sendDocument', {'chat_id': chat}, {'document': (m.get('name') or m['document'].rsplit('/', 1)[-1], fetch(m['document']))})
            if not r.get('ok'):
                ok, err = False, r.get('description', 'unknown')
                break
            ids.append(r['result']['message_id'])
    else:
        r = send_video(chat, item['video'], item.get('caption', ''))
        ok = r.get('ok', False)
        if ok:
            ids.append(r['result']['message_id'])
        else:
            err = r.get('description', 'unknown')
    if ok:
        item['sent'] = True
        item['message_id'] = ids[0] if len(ids) == 1 else ids
        item['sent_at'] = now.isoformat()
        print('sent', chat, ids)
    else:
        item['last_error'] = err
        print('ERROR', chat, err, file=sys.stderr)
    changed = True

if changed:
    json.dump(queue, open(QUEUE, 'w'), ensure_ascii=False, indent=2)
