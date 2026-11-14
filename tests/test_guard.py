import asyncio
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import EditMessageText, RestrictChatMember, SendMessage
from aiogram.types import Chat, ChatPermissions, Message, MessageEntity, Update, User
from booking.store import Store
from booking.domain import OWNER_ID
from guard import Guard, make_guard_router, EditEphemeral, DeleteEphemeral


class FakeBot:
    def __init__(self):
        self.get_me=AsyncMock(return_value=SimpleNamespace(id=100))
        self.get_chat_member=AsyncMock(return_value=SimpleNamespace(status='member'))
        self.get_chat=AsyncMock(return_value=SimpleNamespace(permissions=ChatPermissions(can_send_messages=True),join_to_send_messages=True))
        self.restrict_chat_member=AsyncMock()
        self.unban_chat_member=AsyncMock()
        self.send_message=AsyncMock(return_value=SimpleNamespace(message_id=88,ephemeral_message_id=99))
        self.edit_message_text=AsyncMock()
        self.delete_message=AsyncMock()
        self.answer_callback_query=AsyncMock()
        self.request=AsyncMock(return_value=True)
    async def __call__(self,method):
        if isinstance(method,SendMessage):
            return await self.send_message(**method.model_dump(exclude_unset=True))
        return await self.request(method)


class GuardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(Path(self.tmp.name)/'db.sqlite3')
        self.bot=FakeBot();self.now=1000
        self.g=Guard(self.store,self.bot,now=lambda:self.now)
        with self.store.connect() as db:db.execute("INSERT INTO guard_chats VALUES (-42,'private')")
        self.user=User(id=55,first_name='Гость <test>',is_bot=False)
    async def asyncTearDown(self):self.tmp.cleanup()
    def row(self):return self.g.row(-42,55)
    def query(self,row=None,index=None,uid=55,data=None):
        row=row or self.row()
        return SimpleNamespace(id='callback',message=SimpleNamespace(chat=SimpleNamespace(id=-42)),from_user=SimpleNamespace(id=uid),
            data=data or f"guard:55:{row['nonce']}:{row['answer'] if index is None else index}")
    async def wrong(self):
        row=self.row();await self.g.callback(self.query(row,index=(row['answer']+1)%6))
    async def test_private_mute_success_and_cleanup(self):
        await self.g.joined(-42,self.user)
        self.assertEqual(self.row()['deadline'],87400)
        self.assertFalse(self.bot.restrict_chat_member.call_args.kwargs['permissions'].can_send_messages)
        sent=self.bot.send_message.call_args.kwargs
        self.assertEqual(sent['ephemeral_message_parameters'],{'receiver_user_id':55})
        self.assertIsNone(sent['parse_mode'])
        await self.g.callback(self.query())
        self.assertEqual(self.row()['state'],'done')
        self.assertTrue(self.bot.restrict_chat_member.call_args.kwargs['permissions'].can_send_messages)
        self.assertIsInstance(self.bot.request.call_args.args[0],DeleteEphemeral)
        self.bot.unban_chat_member.assert_not_called()
    async def test_three_wrong_edit_same_message_pause_and_restart(self):
        await self.g.joined(-42,self.user)
        previous=self.row()
        for n in range(1,4):
            await self.wrong();r=self.row()
            self.assertEqual(r['attempts'],n)
            self.assertNotEqual(r['nonce'],previous['nonce'])
            self.assertNotEqual(json.loads(r['choices'])[r['answer']],json.loads(previous['choices'])[previous['answer']])
            edit=self.bot.request.call_args.args[0]
            self.assertIsInstance(edit,EditEphemeral);self.assertEqual(edit.ephemeral_message_id,99)
            previous=r
        self.assertEqual(self.bot.send_message.await_count,1)
        self.assertEqual(self.row()['cooldown'],1060)
        self.g=Guard(Store(self.store.path),self.bot,now=lambda:self.now)
        await self.g.callback(self.query());self.assertEqual(self.row()['attempts'],3)
        self.now=1061;await self.g.tick()
        self.assertEqual(self.row()['attempts'],0);self.assertEqual(self.row()['deadline'],87400)
        self.assertEqual(self.bot.send_message.await_count,1)
    async def test_other_user_stale_and_concurrent_clicks(self):
        await self.g.joined(-42,self.user);r=self.row()
        await self.g.callback(self.query(uid=77));self.assertEqual(self.row()['attempts'],0)
        q=self.query(index=(r['answer']+1)%6)
        await asyncio.gather(self.g.callback(q),self.g.callback(q))
        self.assertEqual(self.row()['attempts'],1)
        await self.g.callback(self.query(r));self.assertEqual(self.row()['state'],'pending')
    async def test_expiry_removes_allows_rejoin(self):
        await self.g.joined(-42,self.user);self.now=87400
        await self.g.callback(self.query())
        self.bot.unban_chat_member.assert_awaited_once_with(-42,55,only_if_banned=False)
        self.assertEqual(self.row()['state'],'done')
        await self.g.joined(-42,self.user)
        self.assertEqual(self.row()['deadline'],self.now+86400)
    async def test_recovery_does_not_reset_attempts(self):
        await self.g.joined(-42,self.user);await self.wrong()
        await self.g.callback(self.query(data='guard:recover'))
        self.assertEqual(self.row()['attempts'],1);self.assertEqual(self.row()['deadline'],87400)
        self.assertEqual(self.bot.send_message.await_count,2)
    async def test_disabled_chat_existing_restricted_and_admin_are_untouched(self):
        await self.g.joined(-99,self.user);self.assertIsNone(self.g.row(-99,55))
        for status in ('administrator','creator','restricted'):
            self.bot.get_chat_member.return_value=SimpleNamespace(status=status)
            await self.g.joined(-42,self.user)
        self.assertEqual(self.row()['state'],'done');self.bot.restrict_chat_member.assert_not_called()
    async def test_expiry_does_not_unban_moderator_ban(self):
        await self.g.joined(-42,self.user);self.now=87400
        self.bot.get_chat_member.return_value=SimpleNamespace(status='kicked')
        await self.g.tick();self.bot.unban_chat_member.assert_not_called()
    async def test_left_not_kicked_later(self):
        await self.g.joined(-42,self.user);await self.g.left(-42,55)
        self.now=90000;await self.g.tick();self.bot.unban_chat_member.assert_not_called()
    async def test_public_wrong_edits_existing_not_sends(self):
        with self.store.connect() as db:db.execute("UPDATE guard_chats SET mode='public'")
        await self.g.joined(-42,self.user);await self.wrong()
        self.assertEqual(self.bot.send_message.await_count,1)
        self.assertEqual(self.bot.edit_message_text.call_args.kwargs['message_id'],88)
        self.bot.request.assert_not_called()
    async def test_failed_edit_retries_same_message(self):
        with self.store.connect() as db:db.execute("UPDATE guard_chats SET mode='public'")
        await self.g.joined(-42,self.user)
        self.bot.edit_message_text.side_effect=TelegramNetworkError(method=EditMessageText(text='x'),message='offline')
        with self.assertRaises(TelegramNetworkError):await self.wrong()
        self.assertEqual(self.row()['dirty'],1)
        self.bot.edit_message_text.side_effect=None
        await self.g.tick();self.assertEqual(self.row()['dirty'],0)
        self.assertEqual(self.bot.send_message.await_count,1)
    async def test_failed_unlock_persisted_retried(self):
        await self.g.joined(-42,self.user)
        self.bot.restrict_chat_member.side_effect=TelegramNetworkError(method=RestrictChatMember(chat_id=-42,user_id=55,permissions=ChatPermissions()),message='offline')
        with self.assertRaises(TelegramNetworkError):await self.g.callback(self.query())
        self.assertEqual(self.row()['state'],'unlock')
        self.bot.restrict_chat_member.side_effect=None;await self.g.tick()
        self.assertEqual(self.row()['state'],'done')
    async def test_enable_requires_rights_and_join_before_comment(self):
        with self.assertRaises(ValueError):await self.g.enable(-77)
        self.bot.get_chat_member.return_value=SimpleNamespace(status='administrator',can_restrict_members=True,can_delete_messages=True)
        self.bot.get_chat.return_value.join_to_send_messages=False
        with self.assertRaises(ValueError):await self.g.enable(-77)
        self.bot.get_chat.return_value.join_to_send_messages=True
        await self.g.enable(-77);self.assertEqual(self.g.mode(-77),'private')
    async def test_router_only_owner_can_activate(self):
        bot=Bot('123456:test-token');bot.session=AsyncMock()
        dp=Dispatcher();dp.include_router(make_guard_router(self.g))
        self.g.enable=AsyncMock()
        for uid in (77,OWNER_ID):
            msg=Message(message_id=1,date=datetime.now(),chat=Chat(id=-42,type='supergroup'),
                from_user=User(id=uid,first_name='Илья',is_bot=False),text='/guard_on',
                entities=[MessageEntity(type='bot_command',offset=0,length=9)])
            await dp.feed_update(bot,Update(update_id=uid,message=msg))
        self.g.enable.assert_awaited_once_with(-42,'private')
        await bot.session.close()

    async def test_duplicate_join_after_success_does_not_remute(self):
        await self.g.joined(-42,self.user,joined_at=1000)
        await self.g.callback(self.query())
        count=self.bot.restrict_chat_member.await_count
        self.now=1005
        await self.g.joined(-42,self.user,joined_at=1000)
        self.assertEqual(self.row()['state'],'done')
        self.assertEqual(self.bot.restrict_chat_member.await_count,count)

    async def test_join_lookup_failure_retains_pending(self):
        self.bot.get_chat_member.side_effect=TelegramNetworkError(method=EditMessageText(text='x'),message='offline')
        with self.assertRaises(TelegramNetworkError):await self.g.joined(-42,self.user)
        self.assertEqual(self.row()['state'],'pending')
        self.bot.get_chat_member.side_effect=None
        await self.g.tick()
        self.assertEqual(self.row()['restricted'],1)

    async def test_cleanup_retried_without_second_unlock(self):
        await self.g.joined(-42,self.user)
        self.bot.request.side_effect=TelegramNetworkError(method=EditMessageText(text='x'),message='offline')
        with self.assertRaises(TelegramNetworkError):await self.g.callback(self.query())
        self.assertEqual(self.row()['state'],'cleanup')
        count=self.bot.restrict_chat_member.await_count
        self.bot.request.side_effect=None
        await self.g.tick()
        self.assertEqual(self.row()['state'],'done')
        self.assertEqual(self.bot.restrict_chat_member.await_count,count)

    async def test_ephemeral_transport_keeps_new_api_fields(self):
        from aiogram.methods import SendMessage
        from aiogram.client.session.aiohttp import AiohttpSession
        method=SendMessage(chat_id=-42,text='test',ephemeral_message_parameters={'receiver_user_id':55})
        bot=Bot('123456:test-token')
        session=AiohttpSession()
        form=session.build_form_data(bot,method)
        self.assertIn('ephemeral_message_parameters',[field[0]['name'] for field in form._fields])
        response=session.check_response(bot,method,200,json.dumps({'ok':True,'result':{'message_id':0,'ephemeral_message_id':99,'date':1000,'chat':{'id':-42,'type':'supergroup'},'text':'test'}}))
        self.assertEqual(response.result.ephemeral_message_id,99)
        edit=EditEphemeral(chat_id=-42,receiver_user_id=55,ephemeral_message_id=99,text='edit',reply_markup=self.g.content(dict(name='test',cooldown=0,choices='["a","b"]',answer=0,attempts=0,user_id=55,nonce='abc'))[1])
        self.assertEqual(edit.__api_method__,'editEphemeralMessageText')
        self.assertTrue(session.check_response(bot,edit,200,'{"ok":true,"result":true}').result)
        await session.close();await bot.session.close()

    async def test_real_dispatcher_join_duplicates_and_guard_off(self):
        bot=Bot('123456:test-token');bot.session=AsyncMock()
        dp=Dispatcher();dp.include_router(make_guard_router(self.g))
        user=self.user.model_dump()
        event={'chat':{'id':-42,'type':'supergroup'},'from':user,'date':1000,
               'old_chat_member':{'status':'left','user':user},'new_chat_member':{'status':'member','user':user}}
        await dp.feed_update(bot,Update.model_validate({'update_id':1,'chat_member':event}))
        self.assertEqual(self.row()['state'],'pending')
        await self.g.callback(self.query())
        # Service join message arrives later than the membership event.
        await dp.feed_update(bot,Update.model_validate({'update_id':2,'message':{'message_id':2,'date':1000,
            'chat':{'id':-42,'type':'supergroup'},'from':user,'new_chat_members':[user]}}))
        self.assertEqual(self.row()['state'],'done');self.assertEqual(self.bot.send_message.await_count,1)
        self.now=2000;await self.g.joined(-42,self.user)
        await dp.feed_update(bot,Update.model_validate({'update_id':3,'message':{'message_id':3,'date':2000,
            'chat':{'id':-42,'type':'supergroup'},'from':{'id':OWNER_ID,'is_bot':False,'first_name':'Owner'},
            'text':'/guard_off','entities':[{'type':'bot_command','offset':0,'length':10}]}}))
        self.assertIsNone(self.g.mode(-42));self.assertEqual(self.row()['state'],'done')
        await bot.session.close()

    async def test_real_bot_onboarding_send_edit_and_restart_retry(self):
        # Keep Bot methods real: a permissive send_message mock hid a signature error.
        from aiogram.methods import GetChatMember, SendMessage, GetChat
        bot=Bot('123456:test-token')
        original_session=bot.session
        calls=[]
        async def transport(client,method,**kwargs):
            calls.append(method)
            if isinstance(method,GetChatMember):
                return SimpleNamespace(status='member')
            if isinstance(method,GetChat):
                return SimpleNamespace(permissions=ChatPermissions(can_send_messages=True))
            if isinstance(method,SendMessage):
                self.assertEqual(method.model_dump()['ephemeral_message_parameters'],{'receiver_user_id':55})
                return original_session.check_response(bot,method,200,json.dumps({'ok':True,'result':{
                    'message_id':0,'ephemeral_message_id':99,'date':1000,'chat':{'id':-42,'type':'supergroup'}}})).result
            return True
        bot.session=AsyncMock(side_effect=transport)
        self.g=Guard(self.store,bot,now=lambda:self.now)
        try:
            await self.g.joined(-42,self.user)
            self.assertEqual(self.row()['message_id'],99)
            await self.wrong()
            self.assertIsInstance(calls[-1],EditEphemeral)
            self.assertEqual(calls[-1].ephemeral_message_id,99)
            self.assertEqual(sum(isinstance(c,SendMessage) for c in calls),1)
            # Reproduce production state: already muted, pending message never sent.
            self.g.save(self.row(),message_id=None,dirty=1)
            deadline=self.row()['deadline']
            self.g=Guard(Store(self.store.path),bot,now=lambda:self.now)
            await self.g.tick()
            self.assertEqual(self.row()['message_id'],99)
            self.assertEqual(self.row()['dirty'],0)
            self.assertEqual(self.row()['attempts'],1)
            self.assertEqual(self.row()['deadline'],deadline)
            await self.g.callback(self.query())
            self.assertEqual(self.row()['state'],'done')
        finally:
            await original_session.close()
