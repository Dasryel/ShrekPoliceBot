import asyncio
from datetime import datetime,timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock
from types import SimpleNamespace
from telethon import types
from bot_mtproto import Archive,Bot,unpack,classify,restored_poll

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
    async def test_media_gif_first_original_caption_and_retry(self):
        for original_caption in ('original photo caption',''):
            events = []
            media = message(20,original_caption)
            media.media = types.MessageMediaPhoto(photo=types.PhotoEmpty(id=42))
            file = Path(self.temp.name)/'photo.jpg'
            file.write_bytes(b'diagnostic fixture')
            self.bot.archive.remember(-100123,media,str(file),sender='@ali',display_name='Ali')
            self.bot.archive.stage(-100123,20,1)
            async def gif(chat,caption,extra):
                events.append(('gif',caption))
            async def send_file(chat,path,**kwargs):
                events.append(('media',kwargs['caption']))
            async def send(chat,operation):
                return await operation()
            self.bot.gif = gif
            self.bot.send = send
            self.bot.client = SimpleNamespace(send_file=send_file)
            await self.bot.restore(-100123,20)
            self.assertEqual(events[0][0],'gif')
            self.assertEqual(events[1],('media',original_caption))
            if not original_caption:
                self.assertEqual(events[0][1],'Kiinni jäit "@ali",')
            # After the GIF is acknowledged, retry sends media only.
            self.bot.archive.stage(-100123,20,4)
            events.clear()
            await self.bot.restore(-100123,20)
            self.assertEqual(events,[('media',original_caption)])
    async def test_webm_upload_keeps_native_sticker_attributes(self):
        msg=message(30,'')
        attrs=[types.DocumentAttributeSticker('🙂',types.InputStickerSetEmpty()),types.DocumentAttributeVideo(1,512,512)]
        msg.media=types.MessageMediaDocument(document=types.Document(42,1,b'',datetime.now(timezone.utc),'video/webm',4,1,attrs))
        path=Path(self.temp.name)/'sticker.webm'
        path.write_bytes(b'test')
        self.bot.archive.remember(-100123,msg,str(path),sender='@ali',display_name='Ali')
        self.bot.archive.stage(-100123,30,1)
        self.bot.gif=AsyncMock()
        async def send(chat,operation):
            return await operation()
        self.bot.send=send
        self.bot.client=SimpleNamespace(upload_file=AsyncMock(return_value=types.InputFile(1,1,'sticker.webm','')),send_file=AsyncMock())
        await self.bot.restore(-100123,30)
        uploaded=self.bot.client.send_file.call_args.args[1]
        self.assertIsInstance(uploaded,types.InputMediaUploadedDocument)
        self.assertEqual(uploaded.mime_type,'video/webm')
        self.assertEqual([bytes(a) for a in uploaded.attributes],[bytes(a) for a in attrs])

if __name__ == '__main__':
    unittest.main()
