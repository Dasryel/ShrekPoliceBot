"""Shrek Recall: administrator-group archive with events and batch reconciliation."""
import asyncio
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import sqlite3
import time
import msvcrt
import os
import copy

from telethon import TelegramClient, events, errors, functions, types, utils
from telethon.extensions import BinaryReader

ROOT = Path(__file__).resolve().parent
log = logging.getLogger('shrek')

def unpack(blob):
    with BinaryReader(blob) as reader:
        return reader.tgread_object()

def classify(ids, messages):
    wanted = set(ids)
    return {m.id for m in messages if isinstance(m, types.MessageEmpty) and m.id in wanted}

def restored_poll(media):
    poll = copy.deepcopy(media.poll)
    poll.id = poll.hash = 0
    # Original deadlines may have expired; restored polls start without a timer.
    poll.close_date = poll.close_period = None
    correct_options = {r.option for r in (media.results.results or []) if r.correct}
    correct = [i for i,a in enumerate(poll.answers) if a.option in correct_options]
    if poll.quiz and not correct:
        return None
    return types.InputMediaPoll(poll,correct_answers=correct if poll.quiz else None,
        solution=media.results.solution,solution_entities=media.results.solution_entities)

class Archive:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS groups (id INTEGER PRIMARY KEY, basic INTEGER, title TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS messages (chat INTEGER, id INTEGER, date REAL, blob BLOB, file TEXT, stage INTEGER DEFAULT 0, retry REAL DEFAULT 0, PRIMARY KEY(chat,id))')
        if 'sender' not in {r[1] for r in self.db.execute('PRAGMA table_info(messages)')}:
            self.db.execute('ALTER TABLE messages ADD COLUMN sender TEXT')
        if 'display_name' not in {r[1] for r in self.db.execute('PRAGMA table_info(messages)')}:
            self.db.execute('ALTER TABLE messages ADD COLUMN display_name TEXT')
        if 'edit_notice' not in {r[1] for r in self.db.execute('PRAGMA table_info(messages)')}:
            self.db.execute('ALTER TABLE messages ADD COLUMN edit_notice INTEGER')
        self.db.execute('CREATE TABLE IF NOT EXISTS edits (chat INTEGER, id INTEGER, stamp REAL, text TEXT, PRIMARY KEY(chat,id,stamp))')
        self.db.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, blob BLOB)')
        self.db.commit()
    def remember(self, chat, message, file=None, sender=None, display_name=None):
        # Edits replace the stored content without creating a second restoration.
        self.db.execute('INSERT INTO messages(chat,id,date,blob,file,sender,display_name) VALUES(?,?,?,?,?,?,?) ON CONFLICT(chat,id) DO UPDATE SET blob=excluded.blob,file=COALESCE(excluded.file,messages.file),sender=COALESCE(excluded.sender,messages.sender),display_name=COALESCE(excluded.display_name,messages.display_name)',
                        (chat,message.id,message.date.timestamp(),bytes(message),file,sender,display_name))
        self.db.commit()
    def row(self, chat, mid):
        return self.db.execute('SELECT blob,file,stage FROM messages WHERE chat=? AND id=?',(chat,mid)).fetchone()
    def stage(self, chat, mid, stage, retry=0):
        self.db.execute('UPDATE messages SET stage=?,retry=? WHERE chat=? AND id=?',(stage,retry,chat,mid))
        self.db.commit()
    def remember_edit(self, chat, mid, stamp, text):
        self.db.execute('INSERT OR IGNORE INTO edits(chat,id,stamp,text) VALUES(?,?,?,?)',(chat,mid,stamp,text))
        self.db.commit()
    def edits(self, chat, mid):
        return self.db.execute('SELECT stamp,text FROM edits WHERE chat=? AND id=? ORDER BY stamp',(chat,mid)).fetchall()
    def edit_notice(self, chat, mid):
        row = self.db.execute('SELECT edit_notice FROM messages WHERE chat=? AND id=?',(chat,mid)).fetchone()
        return row[0] if row else None
    def set_edit_notice(self, chat, mid, notice):
        self.db.execute('UPDATE messages SET edit_notice=? WHERE chat=? AND id=?',(notice,chat,mid))
        self.db.commit()

class Bot:
    def __init__(self, config):
        self.cfg = config
        self.archive = Archive(ROOT/'archive.sqlite3')
        self.client = TelegramClient(str(ROOT/'shrek-bot'), int(config['api_id']), config['api_hash'],
                                     flood_sleep_threshold=0, request_retries=0,
                                     auto_reconnect=True, sequential_updates=False)
        self.me = None
        self.admin_cache = {}
        self.check_lock = asyncio.Lock()
        self.next_read = 0
        self.blocked = 0
        self.send_lock = asyncio.Lock()
        self.next_send = {}
        self.inflight = set()
        self.tasks = set()
        self.old_cursor = {}
        self.next_old = {}
        self.media_limit = asyncio.Semaphore(2)
        self.excluded = set(config.get('excluded_group_ids', []))
    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
    async def admin(self, chat, force=False):
        if chat in self.excluded:
            return False
        cached = self.admin_cache.get(chat)
        if not force and cached and cached[1] > time.monotonic():
            return cached[0]
        try:
            permissions = await self.client.get_permissions(chat, self.me)
            allowed = bool(permissions.is_admin or permissions.is_creator)
            self.admin_cache[chat] = (allowed,time.monotonic()+(300 if allowed else 15))
            return allowed
        except errors.FloodWaitError as error:
            self.blocked = max(self.blocked,time.monotonic()+error.seconds+1)
            return False
        except Exception:
            self.admin_cache[chat] = (False,time.monotonic()+15)
            return False
    async def incoming(self, event, edit=False):
        try:
            if not event.is_group or not await self.admin(event.chat_id):
                return
            chat = await event.get_chat()
            self.archive.db.execute('INSERT OR REPLACE INTO groups VALUES (?,?,?)',
                                    (event.chat_id,int(isinstance(chat,types.Chat)),getattr(chat,'title','group')))
            self.archive.db.commit()
            msg = event.message
            if event.sender_id == self.me.id or isinstance(msg,types.MessageService):
                return
            sender = await event.get_sender()
            if getattr(sender,'bot',False):
                return
            if getattr(msg,'noforwards',False):
                log.info('Skipped protected message in %s',event.chat_id)
                return
            file = None
            previous_text = msg.message or ''
            old_message = None
            if edit and self.archive.row(event.chat_id,msg.id):
                # Never attach an older media file to newly edited media.
                old = self.archive.row(event.chat_id,msg.id)
                old_message = unpack(old[0])
                previous_text = old_message.message or ''
                # Telegram reaction changes can arrive through MessageEdited.
                # They are not message edits and must not trigger any bot action.
                if previous_text == (msg.message or '') and old_message.media == msg.media:
                    return
                if old_message.media != msg.media:
                    self.archive.db.execute('UPDATE messages SET file=NULL WHERE chat=? AND id=?',(event.chat_id,msg.id))
            name = '@'+sender.username if getattr(sender,'username',None) else utils.get_display_name(sender)
            name = name or f'User {event.sender_id}'
            self.archive.remember(event.chat_id,msg,file,sender=name,display_name=utils.get_display_name(sender) or name)
            log.info('Saved message %s in group %s',msg.id,event.chat_id)
            if msg.photo or msg.document:
                self.spawn(self.save_media(event.chat_id,msg))
            if edit and old_message:
                self.archive.remember_edit(event.chat_id,msg.id,
                    (getattr(old_message,'edit_date',None) or old_message.date).timestamp(),previous_text)
                self.archive.remember_edit(event.chat_id,msg.id,
                    (getattr(msg,'edit_date',None) or msg.date).timestamp(),msg.message or '')
                await self.update_edit_notice(event.chat_id,msg.id,name,utils.get_display_name(sender) or name)
        except Exception as error:
            log.exception('Incoming message processing failed')
    async def save_media(self, chat, msg):
        async with self.media_limit:
            try:
                size = getattr(msg.file,'size',0) or 0
                if size > self.cfg.get('max_media_mb',100)*1024*1024:
                    log.warning('Media exceeds configured download limit for %s/%s',chat,msg.id)
                    return
                folder = ROOT/'media'/str(chat)
                folder.mkdir(parents=True,exist_ok=True)
                saved = await self.client.download_media(msg,file=str(folder/str(msg.id)))
                row = self.archive.row(chat,msg.id)
                if saved and row and bytes(unpack(row[0]).media) == bytes(msg.media):
                    self.archive.db.execute('UPDATE messages SET file=? WHERE chat=? AND id=?',(saved,chat,msg.id))
                    self.archive.db.commit()
            except Exception as error:
                log.warning('Media archive failed for %s/%s (%s)',chat,msg.id,type(error).__name__)
    async def update_edit_notice(self, chat, mid, username, display_name):
        heading = f'Kiinni jäi, {username}'
        lines = []
        for stamp, text in self.archive.edits(chat,mid):
            time_label = datetime.fromtimestamp(stamp).astimezone().strftime('%H:%M')
            lines.append(f'{time_label} {display_name}: {text or "[empty message]"}')
        body = heading + '\n\n' + '\n'.join(lines)
        # Telegram messages are limited to 4096 characters. Keep the newest
        # revisions if a long-running message history exceeds that limit.
        if len(body) > 4096:
            kept = []
            remaining = 4096 - len(heading) - len('\n\n[Earlier edits omitted]\n')
            for line in reversed(lines):
                if len(line) + (1 if kept else 0) > remaining:
                    break
                kept.append(line)
                remaining -= len(line) + (1 if kept else 0)
            body = heading + '\n\n[Earlier edits omitted]\n' + '\n'.join(reversed(kept))
        notice = self.archive.edit_notice(chat,mid)
        if notice:
            await self.send(chat,lambda:self.client.edit_message(chat,notice,body,parse_mode=None))
        else:
            sent = await self.send(chat,lambda:self.client.send_message(chat,body,parse_mode=None))
            self.archive.set_edit_notice(chat,mid,sent.id)
    async def read(self, chat, ids):
        async with self.check_lock:
            wait = max(self.next_read,self.blocked)-time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                peer = await self.client.get_input_entity(chat)
                inputs = [types.InputMessageID(mid) for mid in ids]
                if isinstance(peer,types.InputPeerChannel):
                    result = await self.client(functions.channels.GetMessagesRequest(utils.get_input_channel(peer),inputs))
                else:
                    result = await self.client(functions.messages.GetMessagesRequest(inputs))
                return classify(ids,result.messages)
            except errors.FloodWaitError as error:
                self.blocked = time.monotonic()+error.seconds+1
                log.warning('Telegram flood wait: %s seconds',error.seconds)
                return None
            except Exception as error:
                log.warning('Read failed in %s (%s); no deletion inferred',chat,type(error).__name__)
                return None
            finally:
                self.next_read = time.monotonic()+0.5
    async def verify(self, chat, ids):
        selected = []
        for mid in ids:
            key = (chat,mid)
            row = self.archive.row(chat,mid)
            if row and row[2] == 0 and key not in self.inflight:
                self.inflight.add(key)
                selected.append(mid)
        if not selected:
            return
        try:
            if not await self.admin(chat):
                return
            first = await self.read(chat,selected)
            if not first:
                return
            await asyncio.sleep(0.5)
            second = await self.read(chat,sorted(first))
            for mid in (first & second if second is not None else set()):
                self.archive.stage(chat,mid,1)
                log.info('Confirmed deletion %s/%s',chat,mid)
        finally:
            for mid in selected:
                self.inflight.discard((chat,mid))
    async def deletion(self,event):
        if event.chat_id:
            self.spawn(self.verify(event.chat_id,event.deleted_ids))
        else:
            # Small-group deletion updates lack the chat ID. Only basic groups
            # use this message-ID namespace; supergroup IDs must not be matched.
            for (chat,) in self.archive.db.execute('SELECT id FROM groups WHERE basic=1').fetchall():
                self.spawn(self.verify(chat,event.deleted_ids))
    async def send(self,chat,operation):
        async with self.send_lock:
            wait = max(self.next_send.get(chat,0),self.blocked)-time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                return await operation()
            except errors.FloodWaitError as error:
                self.blocked = time.monotonic()+error.seconds+1
                raise
            finally:
                # Source groups still have message-send limits, despite fast reads.
                self.next_send[chat] = time.monotonic()+3.1
    async def restore(self,chat,mid):
        row = self.archive.row(chat,mid)
        if not row or not await self.admin(chat):
            return
        msg = unpack(row[0])
        media_path = row[1]
        stage = row[2]
        thread = getattr(getattr(msg,'reply_to',None),'reply_to_top_id',None)
        extra = {'reply_to':thread} if thread else {}
        text = msg.message or ''
        saved_sender = self.archive.db.execute('SELECT display_name,sender FROM messages WHERE chat=? AND id=?',(chat,mid)).fetchone()
        display_name = saved_sender[0] if saved_sender and saved_sender[0] else (saved_sender[1] if saved_sender and saved_sender[1] else 'Unknown user')
        timestamp = msg.date.astimezone().strftime('%H:%M')
        restored_text = f'{timestamp} {display_name}: {text or "[media]"}'
        has_media = bool(msg.media) and not isinstance(msg.media,types.MessageMediaWebPage)
        if has_media and stage == 1:
            if isinstance(msg.media,types.MessageMediaPoll):
                poll = restored_poll(msg.media)
                if poll:
                    await self.send(chat,lambda:self.client.send_file(chat,poll,caption='',**extra))
                    await self.send(chat,lambda:self.client.send_message(chat,restored_text,parse_mode=None,**extra))
                else:
                    # Telegram does not disclose an unanswered quiz's correct answer.
                    summary = msg.media.poll.question.text+'\n\n'+'\n'.join(f'{i+1}. {a.text.text}' for i,a in enumerate(msg.media.poll.answers))
                    summary += '\n\n(Quiz restored as text: correct answer unavailable.)'
                    await self.send(chat,lambda:self.client.send_message(chat,restored_text+'\n\n'+summary,parse_mode=None,**extra))
            elif media_path and Path(media_path).is_file():
                async def upload_original():
                    file = media_path
                    if msg.document:
                        uploaded = await self.client.upload_file(media_path)
                        file = types.InputMediaUploadedDocument(uploaded,
                            mime_type=msg.document.mime_type,
                            attributes=msg.document.attributes,
                            spoiler=getattr(msg.media,'spoiler',None))
                    return await self.client.send_file(chat,file,caption=restored_text,formatting_entities=msg.entities or [],**extra)
                await self.send(chat,upload_original)
            elif isinstance(msg.media,types.MessageMediaContact):
                contact = types.InputMediaContact(msg.media.phone_number,msg.media.first_name,
                    msg.media.last_name,msg.media.vcard)
                await self.send(chat,lambda:self.client.send_file(chat,contact,caption=restored_text,**extra))
            elif msg.photo or msg.document:
                await self.send(chat,lambda:self.client.send_file(chat,msg.media,caption=restored_text,formatting_entities=msg.entities or [],**extra))
            elif text:
                await self.send(chat,lambda:self.client.send_message(chat,restored_text,formatting_entities=msg.entities or [],**extra))
            else:
                log.warning('Unsupported media for %s/%s; no media copy available',chat,mid)
                await self.send(chat,lambda:self.client.send_message(chat,restored_text+' [This message type cannot be restored automatically.]',parse_mode=None,**extra))
            self.archive.stage(chat,mid,3)
            log.info('Restored media %s/%s',chat,mid)
            return
        if stage == 1:
            if media_path and Path(media_path).is_file():
                await self.send(chat,lambda:self.client.send_file(chat,media_path,caption=restored_text,formatting_entities=msg.entities or [],**extra))
            elif msg.photo or msg.document:
                # Stored file references sometimes remain usable after deletion.
                # If expired, retain the pending row and retry; never claim success.
                await self.send(chat,lambda:self.client.send_file(chat,msg.media,caption=restored_text,parse_mode=None,**extra))
            elif text:
                await self.send(chat,lambda:self.client.send_message(chat,restored_text,formatting_entities=msg.entities or [],**extra))
            else:
                await self.send(chat,lambda:self.client.send_message(chat,restored_text+' [This message type cannot be restored automatically.]',**extra))
            self.archive.stage(chat,mid,2)
        if stage in (1,2):
            self.archive.stage(chat,mid,3)
            log.info('Restored %s/%s',chat,mid)
    async def supervise(self,worker):
        while True:
            try:
                await worker()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log.warning('Restarting background worker %s (%s)',worker.__name__,type(error).__name__)
                await asyncio.sleep(5)
    async def restore_loop(self):
        while True:
            rows = self.archive.db.execute('SELECT chat,id,stage FROM messages WHERE stage IN(1,2) AND retry<=? ORDER BY date LIMIT 1',(time.time(),)).fetchall()
            for chat,mid,stage in rows:
                try:
                    await self.restore(chat,mid)
                    # Pauses while admin status is absent, rather than busy looping.
                    self.archive.db.execute('UPDATE messages SET retry=? WHERE chat=? AND id=?',(time.time()+15,chat,mid))
                    self.archive.db.commit()
                except Exception as error:
                    log.warning('Repost deferred %s/%s (%s)',chat,mid,type(error).__name__)
                    self.archive.stage(chat,mid,self.archive.row(chat,mid)[2],time.time()+max(15,getattr(error,'seconds',0)+1))
            await asyncio.sleep(0.2)
    async def reconcile(self):
        while True:
            start = time.monotonic()
            for (chat,) in self.archive.db.execute('SELECT id FROM groups').fetchall():
                if not await self.admin(chat):
                    continue
                rows = self.archive.db.execute('SELECT id FROM messages WHERE chat=? AND stage=0 ORDER BY date DESC,id DESC',(chat,)).fetchall()
                latest = [r[0] for r in rows[:200]]
                if latest:
                    self.spawn(self.verify(chat,latest))
                older = [r[0] for r in rows[200:]]
                if older and time.monotonic() >= self.next_old.get(chat,0):
                    cursor = self.old_cursor.get(chat,0) % len(older)
                    self.spawn(self.verify(chat,older[cursor:cursor+200]))
                    self.old_cursor[chat] = cursor+200
                    self.next_old[chat] = time.monotonic()+5
            await asyncio.sleep(max(0.2,self.cfg.get('recent_check_seconds',2)-(time.monotonic()-start)))
    async def prune(self):
        while True:
            expiry = time.time()-self.cfg.get('retention_hours',24)*3600
            files = self.archive.db.execute('SELECT file FROM messages WHERE date<? AND stage IN(0,3)',(expiry,)).fetchall()
            self.archive.db.execute('DELETE FROM messages WHERE date<? AND stage IN(0,3)',(expiry,))
            self.archive.db.commit()
            for (file,) in files:
                if file:
                    path = Path(file).resolve()
                    if path.is_relative_to((ROOT/'media').resolve()):
                        path.unlink(missing_ok=True)
            await asyncio.sleep(60)
    async def run(self):
        # A fresh bot session can need a retry while its authorization settles.
        for attempt in range(3):
            try:
                await self.client.start(bot_token=self.cfg['bot_token'])
                break
            except ValueError:
                if attempt == 2:
                    raise
                await self.client.disconnect()
                await asyncio.sleep(2)
        self.me = await self.client.get_me()
        if not self.me.bot:
            raise RuntimeError('The session belongs to a personal account; refusing to run')
        self.client.add_event_handler(self.incoming,events.NewMessage())
        async def edited(event):
            await self.incoming(event,edit=True)
        self.client.add_event_handler(edited,events.MessageEdited())
        self.client.add_event_handler(self.deletion,events.MessageDeleted())
        # Prime configured testing group; other groups register automatically
        # when their first message or service update arrives.
        if self.cfg.get('group_id') and await self.admin(self.cfg['group_id'],force=True):
            entity = await self.client.get_entity(self.cfg['group_id'])
            self.archive.db.execute('INSERT OR IGNORE INTO groups VALUES(?,?,?)',(self.cfg['group_id'],int(isinstance(entity,types.Chat)),entity.title))
            self.archive.db.commit()
        log.info('ONLINE @%s; automatically monitors administrator groups; retention %sh',self.me.username,self.cfg.get('retention_hours',24))
        workers = [asyncio.create_task(self.supervise(coro)) for coro in (self.reconcile,self.restore_loop,self.prune)]
        try:
            await self.client.run_until_disconnected()
        finally:
            for task in workers+list(self.tasks):
                task.cancel()
            await asyncio.gather(*workers,*list(self.tasks),return_exceptions=True)
            await self.client.disconnect()
            self.archive.db.close()

def main():
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s',handlers=[logging.StreamHandler(),logging.FileHandler(ROOT/'mtproto.log',encoding='utf-8')])
    logging.getLogger('telethon').setLevel(logging.WARNING)
    config = json.loads((ROOT/'config.json').read_text(encoding='utf-8-sig'))
    if not all(config.get(k) for k in ('bot_token','api_id','api_hash')):
        raise RuntimeError('Bot and application credentials are missing from config.json')
    lock = open(ROOT/'bot.lock','a+b')
    lock.seek(0,2)
    if lock.tell() == 0:
        lock.write(b'0');lock.flush()
    lock.seek(0)
    try:
        msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
    except OSError:
        print('Shrek Recall is already running. Close that instance before starting another.')
        return
    try:
        (ROOT/'bot.pid').write_text(str(os.getpid()),encoding='utf-8')
        asyncio.run(Bot(config).run())
    except KeyboardInterrupt:
        pass
    except Exception as error:
        detail = str(error)
        for key in ('bot_token','api_hash'):
            detail = detail.replace(str(config[key]),'[redacted]')
        log.error('Bot stopped (%s): %s',type(error).__name__,detail)
        raise SystemExit(1)
    finally:
        lock.close()

if __name__ == '__main__':
    main()





