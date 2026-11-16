import asyncio
from datetime import datetime,timezone
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock
from types import SimpleNamespace
from telethon import types
from bot import Archive,Bot,unpack,classify,restored_poll

def message(mid=1,text='hello'):
    return types.Message(id=mid,peer_id=types.PeerChannel(123),date=datetime.now(timezone.utc),message=text)

class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Archive(Path(self.temp.name)/'test.sqlite3')
    def tearDown(self):
        self.store.db.close();self.temp.cleanup()
    def test_chat_and_id_are_composite_and_edits_preserve_stage(self):
        self.store.remember(-100123,message(text='original'))
        self.store.remember(-100456,message(text='other group'))
        self.store.stage(-100123,1,3)
        self.store.remember(-100123,message(text='edited'))
        self.assertEqual(unpack(self.store.row(-100123,1)[0]).message,'edited')
        self.assertEqual(self.store.row(-100123,1)[2],3)
        self.assertEqual(unpack(self.store.row(-100456,1)[0]).message,'other group')
    def test_explicit_empty_only(self):
        self.assertEqual(classify([1,2,3],[message(1),types.MessageEmpty(id=2,peer_id=None)]),{2})
    def test_poll_preserves_options_and_quiz_answer_indices(self):
        answers=[types.PollAnswer(types.TextWithEntities(t,[]),bytes([i])) for i,t in enumerate(['A','B'])]
        poll=types.Poll(4,types.TextWithEntities('Question',[]),answers,9,multiple_choice=True)
        media=types.MessageMediaPoll(poll,types.PollResults(results=[]))
        restored=restored_poll(media)
        self.assertTrue(restored.poll.multiple_choice)
        self.assertEqual(restored.poll.answers,answers)
        self.assertEqual(poll.id,4)
        poll.quiz=True
        self.assertIsNone(restored_poll(media))
        media.results.results=[types.PollAnswerVoters(b'\x01',0,correct=True)]
        self.assertEqual(restored_poll(media).correct_answers,[1])

class VerificationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.bot = Bot.__new__(Bot)
        self.bot.archive = Archive(Path(self.temp.name)/'test.sqlite3')
        self.bot.archive.remember(-100123,message())
        self.bot.inflight = set()
        self.bot.admin = AsyncMock(return_value=True)
    async def asyncTearDown(self):
        self.bot.archive.db.close();self.temp.cleanup()
    async def test_two_empty_reads_claim_once(self):
        self.bot.read = AsyncMock(side_effect=[{1},{1}])
        await self.bot.verify(-100123,[1])
        await self.bot.verify(-100123,[1])
        self.assertEqual(self.bot.archive.row(-100123,1)[2],1)
        self.assertEqual(self.bot.read.await_count,2)
    async def test_error_or_disagreement_never_reposts(self):
        for replies in ([None],[{1},None],[{1},set()]):
            self.bot.read = AsyncMock(side_effect=replies)
            await self.bot.verify(-100123,[1])
            self.assertEqual(self.bot.archive.row(-100123,1)[2],0)
            self.assertFalse(self.bot.inflight)
    async def test_new_admin_group_is_discovered_without_configured_id(self):
        self.bot.me = SimpleNamespace(id=900)
        self.bot.spawn = lambda task: None
        event = SimpleNamespace(is_group=True,chat_id=-100999,sender_id=1,
            message=message(7,'first message in a new group'),
            get_chat=AsyncMock(return_value=SimpleNamespace(title='New group')),
            get_sender=AsyncMock(return_value=SimpleNamespace(bot=False)))
        await self.bot.incoming(event)
        self.assertIsNotNone(self.bot.archive.row(-100999,7))
        self.assertEqual(self.bot.archive.db.execute('SELECT title FROM groups WHERE id=?',(-100999,)).fetchone()[0],'New group')
    async def test_reaction_only_update_does_not_send_or_record_edit_history(self):
        self.bot.me = SimpleNamespace(id=900)
        self.bot.archive.remember(-100123,message(8,'unchanged'),sender='@ali',display_name='Ali')
        event = SimpleNamespace(is_group=True,chat_id=-100123,sender_id=1,
            message=message(8,'unchanged'),
            get_chat=AsyncMock(return_value=SimpleNamespace(title='Group')),
            get_sender=AsyncMock(return_value=SimpleNamespace(bot=False)))
        self.bot.send = AsyncMock()
        await self.bot.incoming(event,edit=True)
        self.bot.send.assert_not_awaited()
        self.assertEqual(self.bot.archive.edits(-100123,8),[])
    async def test_reaction_on_media_does_not_send_or_record_edit_history(self):
        self.bot.me = SimpleNamespace(id=900)
        original = message(9,'')
        original.media = types.MessageMediaPhoto(photo=types.PhotoEmpty(id=42))
        update = message(9,'')
        update.media = types.MessageMediaPhoto(photo=types.PhotoEmpty(id=42))
        self.bot.archive.remember(-100123,original,sender='@ali',display_name='Ali')
        event = SimpleNamespace(is_group=True,chat_id=-100123,sender_id=1,message=update,
            get_chat=AsyncMock(return_value=SimpleNamespace(title='Group')),
            get_sender=AsyncMock(return_value=SimpleNamespace(bot=False)))
        self.bot.send = AsyncMock()
        await self.bot.incoming(event,edit=True)
        self.bot.send.assert_not_awaited()
        self.assertEqual(self.bot.archive.edits(-100123,9),[])
    async def test_media_restores_with_timestamped_sender_caption(self):
        for original_caption in ('original photo caption',''):
            events = []
            media = message(20,original_caption)
            media.media = types.MessageMediaPhoto(photo=types.PhotoEmpty(id=42))
            file = Path(self.temp.name)/'photo.jpg'
            file.write_bytes(b'diagnostic fixture')
            self.bot.archive.remember(-100123,media,str(file),sender='@ali',display_name='Ali')
            self.bot.archive.stage(-100123,20,1)
            async def send_file(chat,path,**kwargs):
                events.append(('media',kwargs['caption']))
            async def send(chat,operation):
                return await operation()
            self.bot.send = send
            self.bot.client = SimpleNamespace(send_file=send_file)
            await self.bot.restore(-100123,20)
            self.assertEqual(len(events),1)
            self.assertRegex(events[0][1],r'^\d{2}:\d{2} Ali: ')
            self.assertTrue(events[0][1].endswith(original_caption or '[media]'))
            self.assertEqual(self.bot.archive.row(-100123,20)[2],3)
    async def test_text_restores_with_timestamp_and_sender(self):
        sent = []
        self.bot.archive.remember(-100123,message(21,'delete me'),sender='@ali',display_name='Ali')
        self.bot.archive.stage(-100123,21,1)
        async def send_message(chat, text, **kwargs):
            sent.append(text)
        async def send(chat, operation):
            return await operation()
        self.bot.send = send
        self.bot.client = SimpleNamespace(send_message=send_message)
        await self.bot.restore(-100123,21)
        self.assertRegex(sent[0],r'^\d{2}:\d{2} Ali: delete me$')
    async def test_edit_history_reuses_one_bot_message(self):
        sent = []
        edited = []
        async def send_message(chat, text, **kwargs):
            sent.append((chat,text))
            return SimpleNamespace(id=77)
        async def edit_message(chat, mid, text, **kwargs):
            edited.append((chat,mid,text))
        async def send(chat, operation):
            return await operation()
        self.bot.send = send
        self.bot.client = SimpleNamespace(send_message=send_message,edit_message=edit_message)
        stamp = time.time()
        self.bot.archive.remember_edit(-100123,1,stamp,'test1')
        self.bot.archive.remember_edit(-100123,1,stamp + 300,'test2')
        await self.bot.update_edit_notice(-100123,1,'@ali','Ali')
        self.assertEqual(len(sent),1)
        self.assertIn('Kiinni jäi, @ali',sent[0][1])
        self.assertIn('test1',sent[0][1])
        self.assertIn('test2',sent[0][1])
        self.bot.archive.remember_edit(-100123,1,stamp + 360,'test3')
        await self.bot.update_edit_notice(-100123,1,'@ali','Ali')
        self.assertEqual(len(sent),1)
        self.assertEqual(edited[0][1],77)
        self.assertIn('test3',edited[0][2])
    async def test_webm_upload_keeps_native_sticker_attributes(self):
        msg=message(30,'')
        attrs=[types.DocumentAttributeSticker('🙂',types.InputStickerSetEmpty()),types.DocumentAttributeVideo(1,512,512)]
        msg.media=types.MessageMediaDocument(document=types.Document(42,1,b'',datetime.now(timezone.utc),'video/webm',4,1,attrs))
        path=Path(self.temp.name)/'sticker.webm'
        path.write_bytes(b'test')
        self.bot.archive.remember(-100123,msg,str(path),sender='@ali',display_name='Ali')
        self.bot.archive.stage(-100123,30,1)
        async def send(chat,operation):
            return await operation()
        self.bot.send=send
        self.bot.client=SimpleNamespace(upload_file=AsyncMock(return_value=types.InputFile(1,1,'sticker.webm','')),send_file=AsyncMock())
        await self.bot.restore(-100123,30)
        uploaded=self.bot.client.send_file.call_args.args[1]
        self.assertIsInstance(uploaded,types.InputMediaUploadedDocument)
        self.assertEqual(uploaded.mime_type,'video/webm')
        self.assertEqual([bytes(a) for a in uploaded.attributes],[bytes(a) for a in attrs])
    async def test_contact_is_restored_as_a_contact(self):
        msg=message(40,'Call me')
        msg.media=types.MessageMediaContact('+358401234567','Ali','Example','BEGIN:VCARD\nEND:VCARD',0)
        self.bot.archive.remember(-100123,msg,sender='@ali',display_name='Ali')
        self.bot.archive.stage(-100123,40,1)
        async def send(chat,operation):
            return await operation()
        self.bot.send=send
        self.bot.client=SimpleNamespace(send_file=AsyncMock())
        await self.bot.restore(-100123,40)
        contact=self.bot.client.send_file.call_args.args[1]
        self.assertIsInstance(contact,types.InputMediaContact)
        self.assertEqual(contact.phone_number,'+358401234567')
        self.assertEqual(contact.first_name,'Ali')
        self.assertEqual(contact.last_name,'Example')

if __name__ == '__main__':
    unittest.main()
