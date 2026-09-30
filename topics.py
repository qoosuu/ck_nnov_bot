"""Persistent per-user forum topics (one running bot process)."""
import asyncio
import sqlite3
from pathlib import Path


class TopicStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = asyncio.Lock()
        with sqlite3.connect(self.path) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS topics (
                chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                thread_id INTEGER NOT NULL, name TEXT NOT NULL,
                PRIMARY KEY (chat_id, user_id))''')

    async def get(self, bot, chat_id, user):
        # Serialize creation so simultaneous photos cannot create duplicate topics.
        async with self.lock:
            suffix = f" @{user.username}" if user.username else f" · ID {user.id}"
            name = ' '.join(user.full_name.split()) or str(user.id)
            name = name[:128 - len(suffix)] + suffix
            with sqlite3.connect(self.path) as db:
                row = db.execute(
                    'SELECT thread_id, name FROM topics WHERE chat_id=? AND user_id=?',
                    (chat_id, user.id),
                ).fetchone()
            if row:
                return row[0]
            topic = await bot.create_forum_topic(chat_id=chat_id, name=name)
            with sqlite3.connect(self.path) as db:
                db.execute('INSERT INTO topics VALUES (?, ?, ?, ?)',
                           (chat_id, user.id, topic.message_thread_id, name))
            return topic.message_thread_id

    async def forget(self, chat_id, user_id, thread_id):
        async with self.lock:
            with sqlite3.connect(self.path) as db:
                db.execute('DELETE FROM topics WHERE chat_id=? AND user_id=? AND thread_id=?',
                           (chat_id, user_id, thread_id))
