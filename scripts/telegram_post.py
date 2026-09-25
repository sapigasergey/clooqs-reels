"""Отправляет в Telegram-канал ролики из telegram-queue.json, у которых подошло время.

Запускается GitHub Actions по расписанию. Токен — секрет TELEGRAM_BOT_TOKEN, канал — переменная TELEGRAM_CHAT (@clooqs).
Запись очереди: {"at": "2026-09-26T19:00:00+03:00", "video": "https://...mp4", "caption": "...", "sent": false}
Отправленные помечаются sent=true и message_id, файл коммитится обратно.
"""
import datetime, json, os, sys, urllib.parse, urllib.request

TOKEN = os.environ['TELEGRAM_BOT_TOKEN']
CHAT = os.environ.get('TELEGRAM_CHAT', '@clooqs')
QUEUE = 'telegram-queue.json'

queue = json.load(open(QUEUE))
now = datetime.datetime.now(datetime.timezone.utc)
changed = False
for item in queue:
    if item.get('sent'):
        continue
    at = datetime.datetime.fromisoformat(item['at'])
    if at > now:
        continue
    data = urllib.parse.urlencode({'chat_id': CHAT, 'video': item['video'], 'caption': item.get('caption', ''),
                                   'supports_streaming': 'true'}).encode()
    try:
        r = json.load(urllib.request.urlopen('https://api.telegram.org/bot%s/sendVideo' % TOKEN, data=data, timeout=120))
    except urllib.error.HTTPError as e:
        r = json.loads(e.read() or b'{}')
    if r.get('ok'):
        item['sent'] = True
        item['message_id'] = r['result']['message_id']
        item['sent_at'] = now.isoformat()
        print('sent', item['video'], r['result']['message_id'])
    else:
        item['last_error'] = r.get('description', 'unknown')
        print('ERROR', item['video'], item['last_error'], file=sys.stderr)
    changed = True

if changed:
    json.dump(queue, open(QUEUE, 'w'), ensure_ascii=False, indent=2)
