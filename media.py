import logging
import sqlite3
from html import escape

from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from topics import TopicStore

from aiogram import Bot, F, Router
from aiogram.filters import CommandStart
from aiogram.types import Message
from dotenv import load_dotenv
import os

load_dotenv()

GROUP_CHAT_ID: int = int(os.environ["GROUP_CHAT_ID"])

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

router = Router()
# Ignore the destination group, including topic service messages.
router.message.filter(F.chat.type == "private")
topics = None


async def _send_media(message: Message, bot: Bot, method, **kwargs) -> bool:
    user = message.from_user
    if user is None:
        return False
    try:
        thread_id = await topics.get(bot, GROUP_CHAT_ID, user)
        try:
            await method(chat_id=GROUP_CHAT_ID, message_thread_id=thread_id, **kwargs)
        except TelegramBadRequest as exc:
            error = exc.message.lower()
            if "message thread not found" in error or "topic_deleted" in error:
                await topics.forget(GROUP_CHAT_ID, user.id, thread_id)
                thread_id = await topics.get(bot, GROUP_CHAT_ID, user)
            elif "topic_closed" in error:
                await bot.reopen_forum_topic(
                    chat_id=GROUP_CHAT_ID, message_thread_id=thread_id,
                )
            else:
                raise
            await method(chat_id=GROUP_CHAT_ID, message_thread_id=thread_id, **kwargs)
        return True
    except (TelegramAPIError, OSError, sqlite3.Error):
        logging.exception("Could not deliver media for user %s", user.id)
        await message.answer("Не удалось передать файл. Попробуй отправить его чуть позже 🙏")
        return False


@router.message(CommandStart())
async def cmd_start(message: Message, access=None) -> None:
    await message.answer(
        "Привет! 👋\n\n"
        "Присылай фото, видео или кружочки — я передам их куда надо 🎬"
    )
    if access is not None:
        try:
            permitted = await access.menu(message.from_user.model_dump())
            if permitted and access.url:
                await message.answer("Для сотрудников: брони доступны через кнопку «Бронирования» в меню.")
        except Exception:
            logging.warning("Could not update booking menu for user %s", message.from_user.id)



@router.message(F.photo)
async def handle_photo(message: Message, bot: Bot) -> None:
    if not await _send_media(
        message, bot, bot.send_photo,
        photo=message.photo[-1].file_id,
        caption=_caption(message),
    ):
        return
    await message.answer("Спасибо, фото получено! 📸")


@router.message(F.video)
async def handle_video(message: Message, bot: Bot) -> None:
    if not await _send_media(
        message, bot, bot.send_video,
        video=message.video.file_id,
        caption=_caption(message),
    ):
        return
    await message.answer("Спасибо, видео получено! 🎥")


@router.message(F.video_note)
async def handle_video_note(message: Message, bot: Bot) -> None:
    if not await _send_media(
        message, bot, bot.send_video_note,
        video_note=message.video_note.file_id,
    ):
        return
    # The topic title identifies the sender; circles do not support captions.
    await message.answer("Спасибо, кружочек получен! ⭕")


@router.message()
async def handle_other(message: Message) -> None:
    await message.answer(
        "Я принимаю только фото, видео и кружочки 🙏\n"
        "Отправь один из этих форматов."
    )


def _caption(message: Message) -> str:
    user = message.from_user
    name = escape(user.full_name)
    username = f" @{escape(user.username)}" if user.username else ""
    return f"От: {name}{username}"
