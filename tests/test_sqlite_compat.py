"""Replay real application SQL with an optional legacy SQLite CLI.

SQLITE_LEGACY_BIN=/path/to/sqlite3 python -m unittest discover -s tests -q
The CLI must be a locally installed test binary; no server or Telegram is used.
"""
import os
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from booking.access import Access
from booking.domain import MSK, OWNER_ID
from booking.service import Service
from booking.store import Store


class SQLiteCompatibilityTests(unittest.TestCase):
    def test_profile_settings_and_digest_updates_preserve_fields(self):
        self.exercise()

    @unittest.skipUnless(os.environ.get('SQLITE_LEGACY_BIN'), 'Set SQLITE_LEGACY_BIN to test an old SQLite binary')
    def test_sql_on_legacy_engine(self):
        self.exercise(os.environ['SQLITE_LEGACY_BIN'])

    def exercise(self, legacy=None):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'current.db'
            statements = []
            connect = sqlite3.connect

            def traced(*args, **kwargs):
                db = connect(*args, **kwargs)
                db.set_trace_callback(statements.append)
                return db

            with patch('sqlite3.connect', traced):
                store = Store(path)
                now = datetime(2026, 10, 3, 15, tzinfo=MSK)
                service = Service(store, SimpleNamespace(), now=lambda: now)
                access = Access(store, SimpleNamespace(), '')
                service.register(-10042, 'Первое название', OWNER_ID)
                service.register(-10042, 'Новое название', OWNER_ID)
                service.change_time(OWNER_ID, '16:00')
                user = {'id': 55, 'first_name': 'Иван', 'username': 'ivan'}
                access.remember(user)
                service.block(OWNER_ID, 55, True)
                access.remember(dict(user, first_name='Новое имя', username=None))
                with store.connect() as db:
                    person = dict(db.execute('SELECT * FROM people WHERE id=55').fetchone())
                    self.assertEqual((person['name'], person['username'], person['blocked']), ('Новое имя', '', 1))
                service.block(OWNER_ID, 55, False)
                payload = dict(shift_date='2026-10-03', start_time='19:00', end_time='20:00',
                               guest_name='Тест', guest_link='', table_no=1, guests=2, comment='')
                service.mutate(55, 'create', payload, str(uuid4()))
                service.manual_digest(OWNER_ID, '2026-10-03')
                now = now.replace(hour=16)
                service.schedule()
                service.manual_digest(OWNER_ID, '2026-10-03')
                service.schedule()
                service.add_ban(OWNER_ID, dict(payload, start_time='22:00', end_time='03:00', reason='Мероприятие'))
                self.assertEqual(len(service.day_view('2026-10-03')['bans']), 1)

            checked_path = path
            if legacy:
                checked_path = Path(tmp) / 'legacy.db'
                sql = '.bail on\n' + '\n'.join(s.rstrip().rstrip(';') + ';' for s in statements)
                result = subprocess.run([legacy, str(checked_path)], input=sql, text=True, capture_output=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
            with connect(checked_path) as db:
                self.assertEqual(db.execute("SELECT value FROM settings WHERE key='staff_chat_title'").fetchone()[0], 'Новое название')
                self.assertEqual(db.execute('SELECT name,username,blocked FROM people WHERE id=55').fetchone(), ('Новое имя', '', 0))
                checked, announced = db.execute('SELECT checked_at,announced FROM digests').fetchone()
                self.assertIsNotNone(checked)
                self.assertEqual(announced, 1)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 3)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM booking_bans').fetchone()[0], 1)
