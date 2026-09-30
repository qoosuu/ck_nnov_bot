import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

os.environ.setdefault('BOT_TOKEN', '123456:TEST')
os.environ.setdefault('GROUP_CHAT_ID', '-100123')
import bot
from topics import TopicStore
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.methods import SendPhoto


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'topics.sqlite3'
        bot.topics = TopicStore(self.path)
        self.api = SimpleNamespace(
            create_forum_topic=AsyncMock(side_effect=[SimpleNamespace(message_thread_id=42), SimpleNamespace(message_thread_id=43)]),
            send_photo=AsyncMock(), send_video=AsyncMock(), send_video_note=AsyncMock(),
            reopen_forum_topic=AsyncMock(),
        )
        self.msg = SimpleNamespace(
            from_user=SimpleNamespace(id=1, full_name='Иван <Кот>', username='ivan'),
            photo=[SimpleNamespace(file_id='photo')], video=SimpleNamespace(file_id='video'),
            video_note=SimpleNamespace(file_id='circle'), answer=AsyncMock(),
        )

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_all_media_same_topic_and_restart(self):
        for handler in (bot.handle_photo, bot.handle_video, bot.handle_video_note):
            await handler(self.msg, self.api)
        bot.topics = TopicStore(self.path)
        await bot.handle_photo(self.msg, self.api)
        self.api.create_forum_topic.assert_awaited_once_with(chat_id=bot.GROUP_CHAT_ID, name='Иван <Кот> @ivan')
        for method in (self.api.send_photo, self.api.send_video, self.api.send_video_note):
            self.assertEqual(method.call_args.kwargs['message_thread_id'], 42)
        self.assertEqual(self.api.send_photo.call_args.kwargs['caption'], 'От: Иван &lt;Кот&gt; @ivan')

    async def test_concurrent_and_different_users(self):
        ids = await asyncio.gather(*(bot.topics.get(self.api, -100123, self.msg.from_user) for _ in range(10)))
        self.assertEqual(ids, [42] * 10)
        self.msg.from_user.id = 2
        self.msg.from_user.username = None
        self.assertEqual(await bot.topics.get(self.api, -100123, self.msg.from_user), 43)
        self.assertEqual(self.api.create_forum_topic.call_args.kwargs['name'], 'Иван <Кот> · ID 2')

    async def test_long_name_keeps_username(self):
        self.msg.from_user.full_name = 'Я' * 200
        await bot.topics.get(self.api, bot.GROUP_CHAT_ID, self.msg.from_user)
        name = self.api.create_forum_topic.call_args.kwargs['name']
        self.assertEqual(len(name), 128)
        self.assertTrue(name.endswith(' @ivan'))

    async def test_group_messages_are_ignored(self):
        self.msg.chat = SimpleNamespace(type='supergroup')
        self.assertFalse((await bot.router.message.check_root_filters(self.msg))[0])
        self.msg.chat.type = 'private'
        self.assertTrue((await bot.router.message.check_root_filters(self.msg))[0])

    async def test_deleted_topic_recreated(self):
        self.api.send_photo.side_effect = [TelegramBadRequest(method=SendPhoto(chat_id=-1, photo='x'), message='Bad Request: message thread not found'), None]
        await bot.handle_photo(self.msg, self.api)
        self.assertEqual(self.api.send_photo.call_args.kwargs['message_thread_id'], 43)
        self.assertEqual(await bot.topics.get(self.api, bot.GROUP_CHAT_ID, self.msg.from_user), 43)

    async def test_closed_topic_reopened(self):
        self.api.send_photo.side_effect = [TelegramBadRequest(method=SendPhoto(chat_id=-1, photo='x'), message='Bad Request: TOPIC_CLOSED'), None]
        await bot.handle_photo(self.msg, self.api)
        self.api.reopen_forum_topic.assert_awaited_once_with(chat_id=bot.GROUP_CHAT_ID, message_thread_id=42)
        self.assertEqual(self.api.send_photo.await_count, 2)

    async def test_permission_error_no_false_success(self):
        self.api.create_forum_topic.side_effect = TelegramForbiddenError(method=SendPhoto(chat_id=-1, photo='x'), message='Forbidden')
        with self.assertLogs(level='ERROR'):
            await bot.handle_photo(self.msg, self.api)
        self.api.send_photo.assert_not_awaited()
        self.assertIn('Не удалось', self.msg.answer.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
