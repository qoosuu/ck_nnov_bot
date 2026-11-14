"""Opt-in newcomer verification. State survives restarts; no Telegram calls on import."""
import asyncio
import json
import logging
import os
import secrets
import time
from typing import ClassVar

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command
from aiogram.methods import TelegramMethod
from aiogram.types import ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup
from booking.domain import OWNER_ID

EMOJI = ['🍋', '🐳', '🎸', '🦊', '🌵', '🚲', '🍕', '🚀', '🐸', '⚽', '🎁', '🍄', '🐙', '🌻', '🍉', '🦋', '🔑', '🧲', '👑', '🦀', '🍒', '🎲', '🧩', '⛵']


class EditEphemeral(TelegramMethod[bool]):
    __returning__: ClassVar = bool
    __api_method__: ClassVar[str] = 'editEphemeralMessageText'
    chat_id: int
    receiver_user_id: int
    ephemeral_message_id: int
    text: str
    reply_markup: InlineKeyboardMarkup


class DeleteEphemeral(TelegramMethod[bool]):
    __returning__: ClassVar = bool
    __api_method__: ClassVar[str] = 'deleteEphemeralMessage'
    chat_id: int
    receiver_user_id: int
    ephemeral_message_id: int


def member_present(member):
    return member.status in ('member', 'administrator', 'creator') or (member.status == 'restricted' and member.is_member)


class Guard:
    def __init__(self, store, bot, now=time.time):
        self.store, self.bot, self.now = store, bot, now
        self.lock = asyncio.Lock()
        with store.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS guard_chats(chat_id INTEGER PRIMARY KEY, mode TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS guard_checks(
                    chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, name TEXT NOT NULL,
                    deadline REAL NOT NULL, joined_at REAL NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    cooldown REAL NOT NULL DEFAULT 0, nonce TEXT NOT NULL, choices TEXT NOT NULL,
                    answer INTEGER NOT NULL, message_id INTEGER, ephemeral INTEGER NOT NULL DEFAULT 0,
                    restricted INTEGER NOT NULL DEFAULT 0, dirty INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY(chat_id,user_id));
            ''')

    def mode(self, chat_id):
        with self.store.connect() as db:
            r = db.execute('SELECT mode FROM guard_chats WHERE chat_id=?', (chat_id,)).fetchone()
            return r['mode'] if r else None

    def row(self, chat_id, user_id):
        with self.store.connect() as db:
            r = db.execute('SELECT * FROM guard_checks WHERE chat_id=? AND user_id=?', (chat_id, user_id)).fetchone()
            return dict(r) if r else None

    def save(self, row, **changes):
        row.update(changes)
        with self.store.connect() as db:
            db.execute('UPDATE guard_checks SET '+','.join(k+'=?' for k in changes)+' WHERE chat_id=? AND user_id=?',
                       (*changes.values(), row['chat_id'], row['user_id']))

    def challenge(self, old=None):
        rng = secrets.SystemRandom()
        choices = rng.sample(EMOJI, 6)
        available = [i for i, x in enumerate(choices) if x != old]
        return dict(nonce=secrets.token_hex(8), choices=json.dumps(choices), answer=rng.choice(available))

    async def enable(self, chat_id, mode='private'):
        if mode not in ('private', 'public'):
            raise ValueError('Используйте /guard_on private или /guard_on public.')
        with self.store.connect() as db:
            staff = db.execute("SELECT value FROM settings WHERE key='staff_chat_id'").fetchone()
        if (staff and int(staff['value']) == chat_id) or str(chat_id) == os.getenv('GROUP_CHAT_ID'):
            raise ValueError('Для проверки новичков нужен отдельный чат обсуждений, не рабочий чат и не чат с фотографиями.')
        me = await self.bot.get_me()
        rights = await self.bot.get_chat_member(chat_id, me.id)
        if rights.status != 'administrator' or not rights.can_restrict_members or not rights.can_delete_messages:
            raise ValueError('Дайте боту права администратора: блокировка участников и удаление сообщений.')
        chat = await self.bot.get_chat(chat_id)
        if not getattr(chat, 'join_to_send_messages', False):
            raise ValueError('Включите в настройках группы вступление перед отправкой сообщений, затем повторите команду.')
        with self.store.connect() as db:
            db.execute('INSERT OR IGNORE INTO guard_chats VALUES (?,?)', (chat_id, mode))
            db.execute('UPDATE guard_chats SET mode=? WHERE chat_id=?', (mode, chat_id))

    async def joined(self, chat_id, user, joined_at=None):
        if user.is_bot or user.id == OWNER_ID or not self.mode(chat_id):
            return
        async with self.lock:
            joined_at = self.now() if joined_at is None else joined_at
            old = self.row(chat_id, user.id)
            if old and joined_at <= old['joined_at']:
                return
            if old:
                await self.cleanup(old)
            c = self.challenge()
            with self.store.connect() as db:
                db.execute('DELETE FROM guard_checks WHERE chat_id=? AND user_id=?', (chat_id, user.id))
                db.execute('''INSERT INTO guard_checks(chat_id,user_id,name,deadline,joined_at,state,nonce,choices,answer)
                    VALUES (?,?,?,?,?,?,?,?,?)''', (chat_id, user.id, user.full_name[:160], joined_at+86400, joined_at,
                                               'pending', c['nonce'], c['choices'], c['answer']))
            await self.progress(self.row(chat_id, user.id))

    def content(self, row):
        prefix = f"{row['name']}, привет! Добро пожаловать в чат.\n\n"
        if row['cooldown'] > self.now():
            text = prefix+'Три попытки закончились. Подожди минуту и нажми «Попробовать снова».\nОбщий срок проверки — 24 часа с момента вступления.'
            buttons = [[InlineKeyboardButton(text='Попробовать снова', callback_data=f"guard:{row['user_id']}:{row['nonce']}:retry")]]
        else:
            choices = json.loads(row['choices'])
            text = prefix+f"Чтобы начать писать, подтверди, что ты человек: выбери кнопку с символом {choices[row['answer']]}.\n\nОсталось попыток: {3-row['attempts']}. На проверку — 24 часа с момента вступления."
            buttons = [[InlineKeyboardButton(text=x, callback_data=f"guard:{row['user_id']}:{row['nonce']}:{i}")
                        for i,x in enumerate(choices)][j:j+3] for j in (0,3)]
        return text, InlineKeyboardMarkup(inline_keyboard=buttons)

    async def display(self, row, recover=False):
        text, markup = self.content(row)
        if row['message_id'] is not None and not recover:
            try:
                if row['ephemeral']:
                    await self.bot(EditEphemeral(chat_id=row['chat_id'], receiver_user_id=row['user_id'],
                        ephemeral_message_id=row['message_id'], text=text, reply_markup=markup))
                else:
                    await self.bot.edit_message_text(chat_id=row['chat_id'], message_id=row['message_id'],
                        text=text, reply_markup=markup, parse_mode=None)
            except TelegramBadRequest as exc:
                if 'message is not modified' not in exc.message.lower():
                    raise
        else:
            if recover and row['message_id'] is not None:
                await self.cleanup(row)
            private = self.mode(row['chat_id']) == 'private'
            extra = {'ephemeral_message_parameters': {'receiver_user_id': row['user_id']}} if private else {}
            message = await self.bot.send_message(chat_id=row['chat_id'], text=text, reply_markup=markup,
                                                 parse_mode=None, **extra)
            mid = getattr(message, 'ephemeral_message_id', None) if private else message.message_id
            if mid is None:
                raise ValueError('Telegram did not return a verification message identifier')
            self.save(row, message_id=mid, ephemeral=int(private))
        self.save(row, dirty=0)

    async def cleanup(self, row):
        if row['message_id'] is None:
            return
        try:
            if row['ephemeral']:
                await self.bot(DeleteEphemeral(chat_id=row['chat_id'], receiver_user_id=row['user_id'], ephemeral_message_id=row['message_id']))
            else:
                await self.bot.delete_message(row['chat_id'], row['message_id'])
        except TelegramBadRequest:
            # Already deleted/expired messages need no further cleanup.
            pass

    async def progress(self, row):
        if row['state'] == 'cleanup':
            await self.cleanup(row)
            self.save(row, state='done')
            return
        if row['state'] == 'pending' and self.now() >= row['deadline']:
            self.save(row, state='kick')
        if row['state'] in ('unlock', 'kick'):
            member = await self.bot.get_chat_member(row['chat_id'], row['user_id'])
            if member.status in ('administrator', 'creator') or not member_present(member):
                self.save(row, state='cleanup')
            elif row['state'] == 'unlock':
                chat = await self.bot.get_chat(row['chat_id'])
                if chat.permissions is None:
                    raise ValueError('Cannot restore missing default group permissions')
                await self.bot.restrict_chat_member(row['chat_id'], row['user_id'], permissions=chat.permissions,
                                                   use_independent_chat_permissions=True)
                self.save(row, state='cleanup')
            else:
                # Telegram removes a current member and allows them to join again.
                await self.bot.unban_chat_member(row['chat_id'], row['user_id'], only_if_banned=False)
                self.save(row, state='cleanup')
            await self.cleanup(row)
            self.save(row, state='done')
            return
        if row['state'] != 'pending':
            return
        if not row['restricted']:
            # Persist first, so a failed rights lookup can be retried after restart.
            current = await self.bot.get_chat_member(row['chat_id'], row['user_id'])
            if current.status != 'member':
                self.save(row, state='cleanup')
                await self.progress(row)
                return
            await self.bot.restrict_chat_member(row['chat_id'], row['user_id'], permissions=ChatPermissions(can_send_messages=False),
                                               use_independent_chat_permissions=True, until_date=0)
            self.save(row, restricted=1)
        if row['cooldown'] and self.now() >= row['cooldown']:
            old = json.loads(row['choices'])[row['answer']]
            self.save(row, attempts=0, cooldown=0, dirty=1, **self.challenge(old))
        if row['dirty']:
            await self.display(row)

    async def callback(self, query):
        try:
            await self.bot.answer_callback_query(query.id)
        except TelegramAPIError:
            pass
        if not query.message or not self.mode(query.message.chat.id):
            return
        chat_id, uid = query.message.chat.id, query.from_user.id
        async with self.lock:
            row = self.row(chat_id, uid)
            if query.data == 'guard:recover':
                if row and row['state'] == 'pending':
                    if self.now() >= row['deadline']:
                        await self.progress(row)
                    else:
                        # Recovery does not reset attempts or the deadline.
                        await self.progress(row)
                        await self.display(row, recover=bool(row['ephemeral']))
                return
            parts = query.data.split(':')
            if len(parts) != 4 or parts[1] != str(uid) or not row or row['state'] != 'pending' or parts[2] != row['nonce']:
                return
            if self.now() >= row['deadline']:
                await self.progress(row)
                return
            if row['cooldown']:
                await self.progress(row)
                return
            if parts[3] not in tuple(str(i) for i in range(6)):
                return
            if int(parts[3]) == row['answer']:
                self.save(row, state='unlock')
            else:
                attempts = row['attempts']+1
                old = json.loads(row['choices'])[row['answer']]
                self.save(row, attempts=attempts, cooldown=self.now()+60 if attempts==3 else 0,
                          dirty=1, **self.challenge(old))
            await self.progress(row)

    async def left(self, chat_id, user_id):
        async with self.lock:
            row = self.row(chat_id,user_id)
            if row and row['state'] in ('pending','unlock','kick'):
                self.save(row,state='cleanup')
                await self.progress(row)

    async def tick(self):
        with self.store.connect() as db:
            keys = [(r['chat_id'],r['user_id']) for r in db.execute("SELECT chat_id,user_id FROM guard_checks WHERE state IN ('pending','unlock','kick','cleanup')")]
        for chat_id,uid in keys:
            async with self.lock:
                row = self.row(chat_id,uid)
                try:
                    await self.progress(row)
                except Exception:
                    logging.exception('Verification retry failed for chat %s user %s',chat_id,uid)
            await asyncio.sleep(.1)

    async def worker(self):
        while True:
            try:
                await self.tick()
            except Exception:
                logging.exception('Verification worker failed; retrying')
            await asyncio.sleep(5)


def make_guard_router(guard):
    router = Router(name='newcomer_guard')

    @router.message(Command('guard_on'), F.chat.type == 'supergroup')
    async def enable(message):
        if not message.from_user or message.from_user.id != OWNER_ID or message.sender_chat:
            return
        mode = message.text.split(maxsplit=1)[1].strip() if len(message.text.split(maxsplit=1))==2 else 'private'
        try:
            await guard.enable(message.chat.id,mode)
            await message.answer('Проверка новых участников включена. Три попытки, затем пауза на минуту. Срок — 24 часа. '
                                 'Закрепите это сообщение: кнопка поможет восстановить исчезнувшую проверку.',
                                 reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='Пройти проверку',callback_data='guard:recover')]]))
        except (ValueError,TelegramAPIError) as exc:
            text = str(exc) if isinstance(exc,ValueError) else 'Не удалось проверить права. Проверьте права бота и повторите команду.'
            await message.answer(text,parse_mode=None)

    @router.message(Command('guard_off'), F.chat.type == 'supergroup')
    async def disable(message):
        if not message.from_user or message.from_user.id != OWNER_ID or message.sender_chat:
            return
        async with guard.lock:
            with guard.store.connect() as db:
                db.execute('DELETE FROM guard_chats WHERE chat_id=?', (message.chat.id,))
                db.execute("UPDATE guard_checks SET state='unlock' WHERE chat_id=? AND state IN ('pending','kick')", (message.chat.id,))
        await guard.tick()
        await message.answer('Новые проверки отключены. Снятие ограничений незавершённых проверок поставлено в обработку.')

    @router.chat_member(lambda e: bool(guard.mode(e.chat.id)))
    async def membership(event):
        if not member_present(event.old_chat_member) and member_present(event.new_chat_member):
            await guard.joined(event.chat.id,event.new_chat_member.user,event.date.timestamp())
        elif member_present(event.old_chat_member) and not member_present(event.new_chat_member):
            await guard.left(event.chat.id,event.new_chat_member.user.id)

    @router.callback_query(F.data.startswith('guard:'))
    async def callback(query):
        try:
            await guard.callback(query)
        except Exception:
            logging.exception('Verification callback failed; persisted operation will be retried')

    @router.message(F.new_chat_members, lambda m: bool(guard.mode(m.chat.id)))
    async def joined_message(message):
        for user in message.new_chat_members:
            await guard.joined(message.chat.id, user,message.date.timestamp())

    def pending(m):
        if not m.from_user or m.sender_chat or not guard.mode(m.chat.id):
            return False
        row = guard.row(m.chat.id, m.from_user.id)
        return bool(row and row['state'] in ('pending', 'unlock', 'kick'))

    @router.message(pending)
    async def pending_message(message):
        if message.from_user and not message.sender_chat:
            row=guard.row(message.chat.id,message.from_user.id)
            if row and row['state'] in ('pending','unlock','kick'):
                await guard.bot.delete_message(message.chat.id,message.message_id)
    return router
