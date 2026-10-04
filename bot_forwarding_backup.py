"""Bot API archive and experimental forwarding-based deletion monitor."""
import json
import logging
import os
from pathlib import Path
import queue
import sqlite3
import threading
import time
import urllib.error
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parent
log = logging.getLogger('bot')

class APIError(Exception):
    def __init__(self, data):
        self.description = data.get('description', 'Telegram error')
        self.retry_after = data.get('parameters', {}).get('retry_after', 0)
        super().__init__(self.description)

def missing(error):
    return error.description.lower() == 'bad request: message to forward not found'

class Telegram:
    def __init__(self, token):
        self.url = 'https://api.telegram.org/bot' + token + '/'
        self.lock = threading.Lock()
        self.next_send = 0
        self.blocked_until = 0
        self.send_interval = 1.1

    def call(self, method, **params):
        # All requests honor server backoff; message sends share a 1.1s limiter.
        sends = method.startswith('send') or method == 'forwardMessage'
        with self.lock:
            delay = max(self.blocked_until, self.next_send if sends else 0) - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            if sends:
                self.next_send = time.monotonic() + self.send_interval
        headers = {'Content-Type': 'application/json'}
        if method == 'sendAnimation' and isinstance(params.get('animation'), Path):
            path = params.pop('animation')
            boundary = uuid.uuid4().hex
            chunks = []
            for key, value in params.items():
                chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
            chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="animation"; filename="reaction.gif"\r\nContent-Type: image/gif\r\n\r\n'.encode() + path.read_bytes() + b'\r\n')
            chunks.append(f'--{boundary}--\r\n'.encode())
            payload = b''.join(chunks)
            headers = {'Content-Type': 'multipart/form-data; boundary=' + boundary}
        else:
            payload = json.dumps(params).encode()
        request = urllib.request.Request(self.url + method, payload, headers)
        try:
            with urllib.request.urlopen(request, timeout=40) as response:
                data = json.load(response)
        except urllib.error.HTTPError as error:
            data = json.load(error)
        if not data.get('ok'):
            error = APIError(data)
            if error.retry_after:
                with self.lock:
                    self.blocked_until = time.monotonic() + error.retry_after + 1
            raise error
        return data['result']

def receive(api, inbox, offset):
    while True:
        try:
            updates = api.call('getUpdates', offset=offset, timeout=25, allowed_updates=['message', 'edited_message'])
            for update in updates:
                done = threading.Event()
                inbox.put((update, done))
                done.wait()  # Persist before acknowledging Telegram's update.
                offset = update['update_id'] + 1
        except Exception as error:
            log.warning('Update fetch failed (%s); retrying', type(error).__name__)
            time.sleep(5)

def load_config():
    config = json.loads((ROOT / 'config.json').read_text())
    token = os.environ.get('TELEGRAM_BOT_TOKEN') or config.get('bot_token')
    if not token or token == 'PUT_BOT_TOKEN_HERE':
        raise ValueError('Set TELEGRAM_BOT_TOKEN or bot_token in config.json')
    if not config.get('group_id') or not config.get('test_channel_id'):
        raise ValueError('Set group_id and test_channel_id in config.json')
    if config['group_id'] == config['test_channel_id']:
        raise ValueError('Test channel must be separate from the group')
    return config, token

def restore(api, db, config, message):
    chat = config['group_id']
    sender = message.get('from', {}).get('first_name', 'Someone')
    label = f'A message from {sender} was deleted (saved copy):'
    if 'text' in message:
        api.call('sendMessage', chat_id=chat, text=message['text'], entities=message.get('entities', []))
    else:
        types = {'photo': 'sendPhoto', 'video': 'sendVideo', 'animation': 'sendAnimation', 'document': 'sendDocument', 'audio': 'sendAudio', 'voice': 'sendVoice', 'sticker': 'sendSticker', 'video_note': 'sendVideoNote'}
        kind = next((k for k in types if k in message), None)
        if not kind:
            api.call('sendMessage', chat_id=chat, text=label + '\n[Unsupported message type]')
        else:
            item = message[kind][-1] if kind == 'photo' else message[kind]
            params = {'chat_id': chat, kind: item['file_id']}
            if kind not in ('sticker', 'video_note'):
                params['caption'] = (label + '\n' + message.get('caption', ''))[:1024]
            api.call(types[kind], **params)

def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    config, token = load_config()
    api = Telegram(token)
    me = api.call('getMe')
    log.info('Running @%s', me['username'])
    api.call('getChat', chat_id=config['group_id'])
    test_chat = api.call('getChat', chat_id=config['test_channel_id'])
    if test_chat['type'] in ('group', 'supergroup'):
        api.send_interval = 3.1
        log.info('Test destination is a group; sends spaced at least 3.1 seconds apart')
    db = sqlite3.connect(ROOT / 'messages.sqlite3')
    db.execute('CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY, body TEXT, expires REAL, due REAL, misses INTEGER DEFAULT 0, state TEXT DEFAULT "active")')
    db.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)')
    db.commit()
    def meta(key, default=None):
        row = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return row[0] if row else default
    def setmeta(key, value):
        db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, str(value)))
    inbox = queue.Queue()
    threading.Thread(target=receive, args=(api, inbox, int(meta('offset', '0'))), daemon=True).start()
    next_probe = 0
    while True:
        while not inbox.empty():
            update, done = inbox.get()
            message = update.get('message') or update.get('edited_message')
            if message and message['chat']['id'] == config['group_id'] and not message.get('from', {}).get('is_bot'):
                now = time.time()
                expires = message['date'] + config.get('tracking_minutes', 20) * 60
                supported = any(k in message for k in ('text', 'photo', 'video', 'animation', 'document', 'audio', 'voice', 'sticker', 'video_note'))
                if supported and not message.get('has_protected_content') and expires > now:
                    db.execute('INSERT OR IGNORE INTO messages (id,body,expires,due) VALUES (?,?,?,?)', (message['message_id'], json.dumps(message), expires, now + config.get('check_interval_seconds', 30)))
                    db.execute('UPDATE messages SET body=? WHERE id=? AND state="active"', (json.dumps(message), message['message_id']))
            setmeta('offset', update['update_id'] + 1)
            db.commit()
            done.set()
        now = time.time()
        db.execute('DELETE FROM messages WHERE expires<? AND state!="restoring"', (now,))
        row = db.execute('SELECT id,body,misses,state FROM messages WHERE due<=? AND state!="done" ORDER BY due LIMIT 1', (now,)).fetchone()
        if row and time.monotonic() >= next_probe:
            mid, body, misses, state = row
            due = now + config.get('check_interval_seconds', 30)
            try:
                if state == 'restoring':
                    restore(api, db, config, json.loads(body))
                    db.execute('UPDATE messages SET state="gif" WHERE id=?', (mid,))
                    db.commit()
                    state = 'gif'
                if state == 'gif':
                    gif = meta('gif_file_id') or ROOT / config.get('gif_path', 'cringe-shrek.gif')
                    sender = json.loads(body).get('from', {}).get('first_name', 'Someone')
                    result = api.call('sendAnimation', chat_id=config['group_id'], animation=gif, caption=f'A message from {sender} was deleted. Saved copy above.')
                    setmeta('gif_file_id', result['animation']['file_id'])
                    db.execute('UPDATE messages SET state="done" WHERE id=?', (mid,))
                    log.info('Restored message %s', mid)
                elif state == 'active':
                    result = api.call('forwardMessage', chat_id=config['test_channel_id'], from_chat_id=config['group_id'], message_id=mid, disable_notification=True)
                    db.execute('UPDATE messages SET misses=0 WHERE id=?', (mid,))
                    try:
                        api.call('deleteMessage', chat_id=config['test_channel_id'], message_id=result['message_id'])
                    except Exception:
                        log.warning('Test-copy cleanup failed; check channel permissions')
            except APIError as error:
                if state == 'active' and missing(error):
                    misses += 1
                    due = time.time() + 3
                    db.execute('UPDATE messages SET misses=?, state=? WHERE id=?', (misses, 'restoring' if misses >= 2 else 'active', mid))
                else:
                    log.warning('Message %s: %s', mid, error.description)
                    due = time.time() + max(30, error.retry_after + 1)
            except Exception as error:
                log.warning('Request failed for %s (%s); retrying later', mid, type(error).__name__)
            db.execute('UPDATE messages SET due=? WHERE id=?', (due, mid))
            db.commit()
            next_probe = time.monotonic() + api.send_interval
        db.commit()
        time.sleep(0.1)

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as error:
        print(f'Startup failed: {error}')
        raise SystemExit(1)
