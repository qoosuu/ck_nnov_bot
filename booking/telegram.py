import logging
from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import ChatMemberUpdated, Message, MenuButtonCommands
from .domain import OWNER_ID, Problem


def make_router(service, access):
    router = Router(name='booking_commands')

    @router.message(Command('register'), F.chat.type.in_({'group', 'supergroup'}))
    async def register(message: Message):
        if not message.from_user or message.from_user.id != OWNER_ID or message.sender_chat:
            return
        if message.chat.is_forum:
            await message.answer('Для бронирований нужен отдельный рабочий чат без топиков.')
            return
        try:
            me = await service.bot.get_me()
            member = await service.bot.get_chat_member(message.chat.id, me.id)
            if member.status not in ('administrator', 'creator'):
                await message.answer('Сначала назначьте меня администратором этого чата, затем повторите /register.')
                return
            service.register(message.chat.id, message.chat.title or 'Рабочий чат', message.from_user.id)
            # Remove stale per-user buttons after changing the registered group.
            with service.store.connect() as db:
                known = [dict(r) for r in db.execute('SELECT id FROM people')]
            for user in known:
                try:
                    await access.menu(user)
                except (TelegramAPIError, Problem):
                    logging.info('Could not refresh menu for user %s', user['id'])
            await message.answer('Рабочий чат зарегистрирован. Сюда будут приходить сводки и изменения броней.\n'
                                 'Каждому сотруднику нужно открыть личный чат со мной и нажать /start. Если уже пользовались ботом — отправьте /iamworker.\n'
                                 'Время дайджеста: ' + service.settings()['digest_time'] + ' (Москва).')
        except TelegramAPIError:
            await message.answer('Не удалось проверить права бота. Повторите /register позже.')

    @router.message(Command('bookings', 'iamworker'), F.chat.type == 'private')
    async def open_bookings(message: Message):
        try:
            permitted = await access.menu(message.from_user.model_dump())
            if not permitted:
                await message.answer('Доступ не подтверждён. Бронирования доступны участникам рабочего чата, которым владелец не отключил доступ.')
            elif not access.url:
                await message.answer('Мини-приложение ещё не настроено: владельцу нужно указать MINI_APP_URL на сервере.')
            else:
                await message.answer('Доступ подтверждён. Кнопка «Бронирования» обновлена — откройте её в меню внизу чата.')
        except (TelegramAPIError, Problem):
            await message.answer('Сейчас не удалось проверить доступ. Попробуйте позже.')

    @router.chat_member()
    async def membership(event: ChatMemberUpdated):
        configured = service.settings()['chat_id']
        user = event.new_chat_member.user
        if not configured or event.chat.id != int(configured) or user.is_bot:
            return
        access.remember(user.model_dump())
        try:
            await access.menu(user.model_dump())
        except (TelegramAPIError, Problem):
            # A menu can be installed after the employee has started the bot.
            pass

    @router.message(F.migrate_to_chat_id)
    async def migration(message: Message):
        configured = service.settings()['chat_id']
        if configured and int(configured) == message.chat.id:
            service.register(message.migrate_to_chat_id, message.chat.title or 'Рабочий чат', OWNER_ID)

    return router
