import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock
from types import SimpleNamespace
from aiogram import Bot, Dispatcher
from aiogram.types import Message, Update, Chat, User, MessageEntity, ChatMemberAdministrator
from booking.access import Access
from booking.domain import OWNER_ID
from booking.service import Service
from booking.store import Store
from booking.telegram import make_router


class CommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.bot=Bot('123456:test-token')
        self.bot.get_me=AsyncMock(return_value=User(id=123456,is_bot=True,first_name='Бот',username='ck_test_bot'))
        self.bot.get_chat_member=AsyncMock(return_value=ChatMemberAdministrator(
            user=User(id=123456,is_bot=True,first_name='Бот'),is_anonymous=False,can_be_edited=False,
            can_manage_chat=True,can_delete_messages=False,can_manage_video_chats=False,
            can_restrict_members=False,can_promote_members=False,can_change_info=False,
            can_invite_users=True,can_post_stories=False,can_edit_stories=False,can_delete_stories=False))
        self.bot.session=AsyncMock()
        self.s=Service(Store(Path(self.tmp.name)/'b.sqlite3'),self.bot)
        self.access=Access(self.s.store,self.bot,'https://example.org')
        self.dp=Dispatcher();self.dp.include_router(make_router(self.s,self.access))
    async def asyncTearDown(self):self.tmp.cleanup()
    async def send(self,uid,text='/register',forum=False,private=False):
        m=Message(message_id=1,date=datetime.now(),chat=Chat(id=uid if private else -10042,type='private' if private else 'supergroup',title='Команда',is_forum=forum),
                  from_user=User(id=uid,is_bot=False,first_name='Иван'),text=text,
                  entities=[MessageEntity(type='bot_command',offset=0,length=len(text))])
        await self.dp.feed_update(self.bot,Update(update_id=1,message=m))
    async def test_only_owner_registers(self):
        await self.send(55);self.assertIsNone(self.s.settings()['chat_id']);self.bot.session.assert_not_called()
        await self.send(OWNER_ID);self.assertEqual(self.s.settings()['chat_id'],'-10042')
        self.assertIn('Рабочий чат зарегистрирован',self.bot.session.call_args.args[1].text)
    async def test_named_command_and_forum_rejected(self):
        await self.send(OWNER_ID,forum=True);self.assertIsNone(self.s.settings()['chat_id'])
        await self.send(OWNER_ID,'/register@ck_test_bot');self.assertEqual(self.s.settings()['chat_id'],'-10042')

    async def test_iamworker_rechecks_existing_user_and_updates_menu(self):
        self.s.register(-10042,'Команда',OWNER_ID)
        self.access.remember({'id':55,'first_name':'Иван'})
        self.bot.set_chat_menu_button=AsyncMock()
        for status,kind in [('left','commands'),('member','web_app'),('left','commands')]:
            self.bot.get_chat_member.return_value=SimpleNamespace(status=status)
            await self.send(55,'/iamworker',private=True)
            self.bot.get_chat_member.assert_awaited_with(chat_id=-10042,user_id=55)
            self.bot.set_chat_menu_button.assert_awaited()
            args=self.bot.set_chat_menu_button.call_args.kwargs
            self.assertEqual(args['chat_id'],55)
            self.assertEqual(args['menu_button'].type,kind)
            text=self.bot.session.call_args.args[1].text
            self.assertIn('Доступ подтверждён' if kind=='web_app' else 'Доступ не подтверждён',text)
        self.assertEqual(self.bot.get_chat_member.await_count,3)

    async def test_iamworker_does_not_override_manual_block(self):
        self.s.register(-10042,'Команда',OWNER_ID)
        self.s.block(OWNER_ID,55,True)
        self.bot.set_chat_menu_button=AsyncMock()
        await self.send(55,'/iamworker',private=True)
        self.assertEqual(self.bot.set_chat_menu_button.call_args.kwargs['menu_button'].type,'commands')
        self.assertIn('Доступ не подтверждён',self.bot.session.call_args.args[1].text)
        with self.s.store.connect() as db:
            self.assertEqual(db.execute('SELECT blocked FROM people WHERE id=55').fetchone()[0],1)

    async def test_iamworker_private_only(self):
        await self.send(55,'/iamworker')
        self.bot.session.assert_not_called()
