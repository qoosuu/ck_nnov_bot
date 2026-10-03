import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT OR IGNORE INTO settings VALUES ('digest_time', '16:00');
                CREATE TABLE IF NOT EXISTS people(
                    id INTEGER PRIMARY KEY, name TEXT NOT NULL, username TEXT NOT NULL DEFAULT '',
                    blocked INTEGER NOT NULL DEFAULT 0, seen_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS bookings(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, shift_date TEXT NOT NULL,
                    table_no INTEGER NOT NULL CHECK(table_no BETWEEN 1 AND 17),
                    guest_name TEXT NOT NULL, guest_link TEXT NOT NULL,
                    guests INTEGER NOT NULL CHECK(guests > 0), start_time TEXT NOT NULL,
                    end_time TEXT, comment TEXT NOT NULL, created_by INTEGER NOT NULL,
                    updated_by INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1, deleted_at TEXT);
                CREATE INDEX IF NOT EXISTS bookings_day ON bookings(shift_date, deleted_at);
                CREATE TABLE IF NOT EXISTS booking_bans(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, shift_date TEXT NOT NULL,
                    start_time TEXT NOT NULL, end_time TEXT NOT NULL, reason TEXT NOT NULL,
                    created_by INTEGER NOT NULL, created_at TEXT NOT NULL,
                    UNIQUE(shift_date,start_time,end_time,reason));
                CREATE INDEX IF NOT EXISTS booking_bans_day ON booking_bans(shift_date);
                CREATE TABLE IF NOT EXISTS audit(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, actor INTEGER NOT NULL,
                    action TEXT NOT NULL, booking_id INTEGER, before_json TEXT, after_json TEXT,
                    created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS visits(
                    session_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, opened_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS visits_time ON visits(opened_at);
                CREATE TABLE IF NOT EXISTS digests(
                    chat_id INTEGER NOT NULL, shift_date TEXT NOT NULL,
                    checked_at TEXT, announced INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(chat_id, shift_date));
                CREATE TABLE IF NOT EXISTS outbox(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,
                    text TEXT NOT NULL, created_at TEXT NOT NULL,
                    sent_at TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt REAL NOT NULL DEFAULT 0, last_error TEXT);
                CREATE TABLE IF NOT EXISTS requests(
                    user_id INTEGER NOT NULL, request_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, response TEXT NOT NULL,
                    PRIMARY KEY(user_id, request_id));
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def setting(db, key, default=None):
        row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return row['value'] if row else default

    @staticmethod
    def put_setting(db, key, value):
        db.execute('INSERT INTO settings VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, str(value)))

    @staticmethod
    def rows(db, d):
        return [dict(r) for r in db.execute('''SELECT * FROM bookings WHERE shift_date=? AND deleted_at IS NULL
            ORDER BY table_no, CASE WHEN start_time < '18:00' THEN 1 ELSE 0 END, start_time, id''', (str(d),))]

    @staticmethod
    def enqueue(db, chat_id, text, now):
        db.execute('INSERT INTO outbox(chat_id,text,created_at) VALUES (?,?,?)', (chat_id, text, now.isoformat()))

    @staticmethod
    def audit(db, actor, action, now, before=None, after=None, booking_id=None):
        db.execute('INSERT INTO audit(actor,action,booking_id,before_json,after_json,created_at) VALUES (?,?,?,?,?,?)',
                   (actor, action, booking_id, json.dumps(before, ensure_ascii=False), json.dumps(after, ensure_ascii=False), now.isoformat()))
