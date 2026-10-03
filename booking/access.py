import asyncio
import logging
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

from aiogram.exceptions import TelegramAPIError
from aiogram.types import MenuButtonCommands, MenuButtonWebApp, WebAppInfo
from .domain import OWNER_ID, Problem, now_msk
from .store import Store


def validate_init_data(raw, token, timestamp=None):
    try:
        if not raw or len(raw) > 16384:
            raise ValueError
        pairs = parse_qsl(raw, strict_parsing=True)
        data = dict(pairs)
        if len(data) != len(pairs):
            raise ValueError
        received = data.pop('hash')
        check = '\n'.join(f'{k}={v}' for k, v in sorted(data.items()))
        secret = hmac.new(b'WebAppData', token.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, received):
            raise ValueError
        age = (time.time() if timestamp is None else timestamp) - int(data['auth_date'])
        if not -30 <= age <= 12 * 3600:
            raise ValueError
        user = json.loads(data['user'])
        if type(user.get('id')) is not int or user['id'] <= 0 or user.get('is_bot'):
            raise ValueError
        return user
    except (ValueError, KeyError, TypeError, AttributeError):
        raise Problem('Откройте приложение заново через меню бота в Telegram.', 401)


class Access:
    def __init__(self, store, bot, url):
        self.store, self.bot, self.url = store, bot, url

    async def allowed(self, user):
        uid = user['id']
        if user.get('is_bot'):
            return False
        if uid == OWNER_ID:
            return True
        with self.store.connect() as db:
            blocked = db.execute('SELECT blocked FROM people WHERE id=?', (uid,)).fetchone()
            chat_id = Store.setting(db, 'staff_chat_id')
        if blocked and blocked['blocked'] or not chat_id:
            return False
        try:
            member = await self.bot.get_chat_member(chat_id=int(chat_id), user_id=uid)
        except TelegramAPIError:
            raise Problem('Не удалось проверить доступ через Telegram. Попробуйте позже.', 503)
        return member.status in ('creator', 'administrator', 'member') or (member.status == 'restricted' and member.is_member)

    def remember(self, user):
        name = ' '.join(filter(None, [user.get('first_name', ''), user.get('last_name', '')])) or str(user['id'])
        with self.store.connect() as db:
            db.execute('''INSERT INTO people(id,name,username,seen_at) VALUES (?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name,username=excluded.username,seen_at=excluded.seen_at''',
                       (user['id'], name, (user.get('username') or ''), now_msk().isoformat()))

    async def require(self, user):
        if not await self.allowed(user):
            raise Problem('Доступ только для сотрудников рабочего чата. Если доступ отключён, обратитесь к владельцу.', 403)
        self.remember(user)

    async def menu(self, user):
        permitted = await self.allowed(user)
        if permitted and 'first_name' in user:
            self.remember(user)
        button = MenuButtonWebApp(text='Бронирования', web_app=WebAppInfo(url=self.url)) if permitted and self.url else MenuButtonCommands()
        await self.bot.set_chat_menu_button(chat_id=user['id'], menu_button=button)
        return permitted

    async def refresh_menus(self):
        """Reconcile known employees, including membership changes missed offline."""
        while True:
            with self.store.connect() as db:
                users = [dict(row) for row in db.execute('SELECT id FROM people')]
            for user in users:
                try:
                    await self.menu(user)
                except (TelegramAPIError, Problem):
                    logging.info('Menu refresh unavailable for user %s', user['id'])
                await asyncio.sleep(0.2)
            await asyncio.sleep(300)
