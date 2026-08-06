import asyncio
import logging

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import Message
from dotenv import load_dotenv
import os

load_dotenv()

BOT_TOKEN: str = os.environ["BOT_TOKEN"]
GROUP_CHAT_ID: int = int(os.environ["GROUP_CHAT_ID"])

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "Привет! 👋\n\n"
        "Присылай фото, видео или кружочки — я передам их куда надо 🎬"
    )


@router.message(F.photo)
async def handle_photo(message: Message, bot: Bot) -> None:
    await bot.send_photo(
        chat_id=GROUP_CHAT_ID,
        photo=message.photo[-1].file_id,
        caption=_caption(message),
    )
    await message.answer("Спасибо, фото получено! 📸")


@router.message(F.video)
async def handle_video(message: Message, bot: Bot) -> None:
    await bot.send_video(
        chat_id=GROUP_CHAT_ID,
        video=message.video.file_id,
        caption=_caption(message),
    )
    await message.answer("Спасибо, видео получено! 🎥")


@router.message(F.video_note)
async def handle_video_note(message: Message, bot: Bot) -> None:
    await bot.send_video_note(
        chat_id=GROUP_CHAT_ID,
        video_note=message.video_note.file_id,
    )
    # Отдельно шлём подпись с отправителем — video_note не поддерживает caption
    await bot.send_message(
        chat_id=GROUP_CHAT_ID,
        text=_caption(message),
    )
    await message.answer("Спасибо, кружочек получен! ⭕")


@router.message()
async def handle_other(message: Message) -> None:
    await message.answer(
        "Я принимаю только фото, видео и кружочки 🙏\n"
        "Отправь один из этих форматов."
    )


def _caption(message: Message) -> str:
    user = message.from_user
    name = user.full_name
    username = f" @{user.username}" if user.username else ""
    return f"От: {name}{username}"


async def main() -> None:
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(router)

    logging.info("ck_nnov_bot starting...")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
