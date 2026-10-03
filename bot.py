import asyncio
from contextlib import suppress
import logging
import os
from pathlib import Path
from urllib.parse import urlparse

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import MenuButtonCommands
from dotenv import load_dotenv

load_dotenv()

import media
from topics import TopicStore
from booking.access import Access
from booking.service import Service
from booking.store import Store
from booking.telegram import make_router
from booking.web import make_app

ROOT = Path(__file__).resolve().parent


async def main():
    url = os.getenv('MINI_APP_URL', '').rstrip('/')
    if url:
        parsed = urlparse(url)
        if parsed.scheme != 'https' or not parsed.netloc or parsed.path not in ('', '/') or parsed.query or parsed.fragment:
            raise RuntimeError('MINI_APP_URL must be an HTTPS origin, e.g. https://booking.example.ru')
    bot = Bot(os.environ['BOT_TOKEN'], default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    media.topics = TopicStore(os.getenv('TOPICS_DB_PATH', str(ROOT / 'topics.sqlite3')))
    store = Store(os.getenv('BOOKINGS_DB_PATH', str(ROOT / 'bookings.sqlite3')))
    access = Access(store, bot, url)
    service = Service(store, bot)
    dp = Dispatcher(access=access)
    dp.include_router(make_router(service, access))
    dp.include_router(media.router)
    runner = web.AppRunner(make_app(service, access, os.environ['BOT_TOKEN']), access_log=None)
    tasks = []
    try:
        await runner.setup()
        await web.TCPSite(runner, os.getenv('WEB_HOST', '127.0.0.1'), int(os.getenv('WEB_PORT', '8080'))).start()
        # Never advertise the app globally: menus are assigned per private chat.
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        tasks = [asyncio.create_task(service.worker()), asyncio.create_task(access.refresh_menus())]
        logging.info('Photo forwarding and booking modules started')
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task
        await runner.cleanup()
        await bot.session.close()


if __name__ == '__main__':
    asyncio.run(main())
