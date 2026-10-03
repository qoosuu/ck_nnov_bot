import asyncio
import hashlib
import json
import logging
from datetime import timedelta
from uuid import UUID

from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from .domain import (OWNER_ID, Problem, booking_text, clock, current_shift, day,
                     digest_texts, due_at, in_notice_window, now_msk, shift_end, validate_booking)
from .store import Store


class Service:
    def __init__(self, store, bot, now=now_msk):
        self.store, self.bot, self.now = store, bot, now
        self.delivery_lock = asyncio.Lock()

    def settings(self):
        with self.store.connect() as db:
            return dict(chat_id=Store.setting(db, 'staff_chat_id'),
                        chat_title=Store.setting(db, 'staff_chat_title', ''),
                        digest_time=Store.setting(db, 'digest_time'))

    def register(self, chat_id, title, actor):
        self.require_owner(actor)
        with self.store.connect() as db:
            old = Store.setting(db, 'staff_chat_id')
            if old and int(old) != chat_id:
                # Pending messages must not leak to the previous staff group.
                db.execute('DELETE FROM outbox WHERE sent_at IS NULL')
            Store.put_setting(db, 'staff_chat_id', chat_id)
            Store.put_setting(db, 'staff_chat_title', title)
            Store.audit(db, actor, 'register', self.now(), before={'chat_id': old}, after={'chat_id': chat_id})

    @staticmethod
    def require_owner(actor):
        if actor != OWNER_ID:
            raise Problem('Этот раздел доступен только владельцу.', 403)

    def _schedule(self, db, now):
        chat_id = Store.setting(db, 'staff_chat_id')
        if not chat_id:
            return
        digest_time = Store.setting(db, 'digest_time')
        for d in (now.date() - timedelta(days=1), now.date()):
            if not due_at(d, digest_time) <= now < shift_end(d):
                continue
            row = db.execute('SELECT checked_at FROM digests WHERE chat_id=? AND shift_date=?', (chat_id, str(d))).fetchone()
            if row and row['checked_at']:
                continue
            texts = digest_texts(d, Store.rows(db, d))
            for text in texts:
                Store.enqueue(db, chat_id, text, now)
            db.execute('''INSERT INTO digests(chat_id,shift_date,checked_at,announced) VALUES (?,?,?,?)
                ON CONFLICT(chat_id,shift_date) DO UPDATE SET checked_at=excluded.checked_at,
                announced=MAX(digests.announced,excluded.announced)''',
                       (chat_id, str(d), now.isoformat(), int(bool(texts))))

    def schedule(self):
        with self.store.connect() as db:
            self._schedule(db, self.now())

    def _notice(self, db, b, heading, now):
        chat_id = Store.setting(db, 'staff_chat_id')
        if not chat_id:
            return
        d = day(b['shift_date'])
        row = db.execute('SELECT announced FROM digests WHERE chat_id=? AND shift_date=?', (chat_id, str(d))).fetchone()
        if in_notice_window(d, now, Store.setting(db, 'digest_time'), bool(row and row['announced'])):
            text = heading + f"\nСмена {d.strftime('%d.%m.%Y')}\n\n" + booking_text(b)
            Store.enqueue(db, chat_id, text, now)

    def list_bookings(self, date_string):
        d = day(date_string)
        with self.store.connect() as db:
            return Store.rows(db, d)

    def mutate(self, actor, operation, data, request_id, booking_id=None):
        try:
            UUID(request_id)
        except (ValueError, TypeError, AttributeError):
            raise Problem('Не указан идентификатор операции.')
        fingerprint = hashlib.sha256(json.dumps([operation, booking_id, data], sort_keys=True).encode()).hexdigest()
        now = self.now()
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT * FROM requests WHERE user_id=? AND request_id=?', (actor, request_id)).fetchone()
            if previous:
                if previous['fingerprint'] != fingerprint:
                    raise Problem('Идентификатор операции уже использован.', 409)
                return json.loads(previous['response'])
            # Capture the pre-change digest if the scheduler has not ticked yet.
            self._schedule(db, now)
            before = None
            if operation != 'create':
                row = db.execute('SELECT * FROM bookings WHERE id=? AND deleted_at IS NULL', (booking_id,)).fetchone()
                if not row:
                    raise Problem('Бронь уже удалена или не найдена.', 404)
                before = dict(row)
                if type(data.get('version')) is not int or data['version'] != before['version']:
                    raise Problem('Бронь уже изменил другой сотрудник. Закройте карточку и откройте заново.', 409)
            if operation == 'delete':
                db.execute('UPDATE bookings SET deleted_at=?,updated_at=?,updated_by=?,version=version+1 WHERE id=?',
                           (now.isoformat(), now.isoformat(), actor, booking_id))
                self._notice(db, before, 'Бронь отменена', now)
                after = dict(before, deleted_at=now.isoformat(), version=before['version'] + 1)
            else:
                fields = validate_booking(data)
                if operation == 'create':
                    fields.update(created_by=actor, updated_by=actor, created_at=now.isoformat(), updated_at=now.isoformat())
                    columns = ','.join(fields)
                    placeholders = ','.join('?' for _ in fields)
                    booking_id = db.execute(f'INSERT INTO bookings({columns}) VALUES ({placeholders})', tuple(fields.values())).lastrowid
                elif operation == 'update':
                    fields.update(updated_by=actor, updated_at=now.isoformat(), version=before['version'] + 1)
                    assignments = ','.join(f'{key}=?' for key in fields)
                    db.execute(f'UPDATE bookings SET {assignments} WHERE id=?', (*fields.values(), booking_id))
                else:
                    raise Problem('Неизвестная операция.')
                after = dict(db.execute('SELECT * FROM bookings WHERE id=?', (booking_id,)).fetchone())
                if before and before['shift_date'] != after['shift_date']:
                    self._notice(db, before, 'Бронь перенесена на другую смену', now)
                    self._notice(db, after, 'Новая бронь сегодня · перенос', now)
                else:
                    heading = 'Новая бронь сегодня' if before is None else 'Бронь изменена'
                    if before:
                        heading += f" · ранее стол №{before['table_no']}, {before['start_time']}"
                    self._notice(db, after, heading, now)
            Store.audit(db, actor, operation, now, before, after, booking_id)
            response = {'booking': after}
            db.execute('INSERT INTO requests VALUES (?,?,?,?)', (actor, request_id, fingerprint, json.dumps(response)))
            return response

    def manual_digest(self, actor, date_string):
        self.require_owner(actor)
        d, now = day(date_string), self.now()
        with self.store.connect() as db:
            chat_id = Store.setting(db, 'staff_chat_id')
            if not chat_id:
                raise Problem('Сначала зарегистрируйте рабочий чат командой /register.')
            texts = digest_texts(d, Store.rows(db, d), manual=True)
            for text in texts:
                Store.enqueue(db, chat_id, text, now)
            if texts:
                db.execute('''INSERT INTO digests(chat_id,shift_date,announced) VALUES (?,?,1)
                    ON CONFLICT(chat_id,shift_date) DO UPDATE SET announced=1''', (chat_id, str(d)))
            Store.audit(db, actor, 'manual_digest', now, after={'date': str(d), 'messages': len(texts)})
            return {'queued': len(texts)}

    def change_time(self, actor, value):
        self.require_owner(actor)
        clock(value)
        with self.store.connect() as db:
            old = Store.setting(db, 'digest_time')
            Store.put_setting(db, 'digest_time', value)
            Store.audit(db, actor, 'digest_time', self.now(), before=old, after=value)

    def block(self, actor, user_id, blocked):
        self.require_owner(actor)
        if user_id == OWNER_ID:
            raise Problem('Владельцу нельзя отключить доступ.')
        if type(user_id) is not int or user_id <= 0 or type(blocked) is not bool:
            raise Problem('Некорректные данные доступа.')
        with self.store.connect() as db:
            db.execute('''INSERT INTO people(id,name,blocked,seen_at) VALUES (?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET blocked=excluded.blocked''',
                       (user_id, str(user_id), int(blocked), self.now().isoformat()))
            Store.audit(db, actor, 'block' if blocked else 'unblock', self.now(), after={'user_id': user_id})

    def visit(self, actor, session_id):
        try:
            UUID(session_id)
        except (ValueError, TypeError, AttributeError):
            raise Problem('Некорректная сессия.')
        with self.store.connect() as db:
            db.execute('INSERT OR IGNORE INTO visits VALUES (?,?,?)', (session_id, actor, self.now().isoformat()))

    def admin_data(self, actor):
        self.require_owner(actor)
        now = self.now()
        with self.store.connect() as db:
            metrics = []
            for days in (1, 7, 30):
                since = (now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days-1)).isoformat()
                row = db.execute('SELECT COUNT(*) opens,COUNT(DISTINCT user_id) people FROM visits WHERE opened_at>=?', (since,)).fetchone()
                metrics.append(dict(days=days, **dict(row)))
            people = [dict(r) for r in db.execute('SELECT * FROM people ORDER BY seen_at DESC')]
            pending = db.execute('SELECT COUNT(*) FROM outbox WHERE sent_at IS NULL').fetchone()[0]
            failed = [dict(r) for r in db.execute('SELECT id,attempts,last_error FROM outbox WHERE sent_at IS NULL AND attempts>0 ORDER BY id LIMIT 10')]
            audit = [dict(r) for r in db.execute('SELECT actor,action,booking_id,created_at FROM audit ORDER BY id DESC LIMIT 30')]
        return dict(metrics=metrics, people=people, pending=pending, delivery_errors=failed, audit=audit, **self.settings())

    async def deliver(self):
        async with self.delivery_lock:
            for _ in range(20):
                now = self.now()
                with self.store.connect() as db:
                    row = db.execute('SELECT * FROM outbox WHERE sent_at IS NULL ORDER BY id LIMIT 1').fetchone()
                if not row or row['next_attempt'] > now.timestamp():
                    return
                try:
                    await self.bot.send_message(chat_id=row['chat_id'], text=row['text'], parse_mode=None,
                                                disable_web_page_preview=True)
                except TelegramAPIError as exc:
                    delay = exc.retry_after + 1 if isinstance(exc, TelegramRetryAfter) else min(300, 5 * 2 ** min(row['attempts'], 6))
                    with self.store.connect() as db:
                        db.execute('UPDATE outbox SET attempts=attempts+1,next_attempt=?,last_error=? WHERE id=?',
                                   (now.timestamp() + delay, type(exc).__name__, row['id']))
                    logging.warning('Booking notification %s delayed: %s', row['id'], type(exc).__name__)
                    return
                with self.store.connect() as db:
                    db.execute('UPDATE outbox SET sent_at=?,last_error=NULL WHERE id=?', (self.now().isoformat(), row['id']))
                # A Telegram group normally accepts about 20 messages per minute.
                await asyncio.sleep(3.1)

    async def worker(self):
        while True:
            try:
                self.schedule()
                await self.deliver()
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.exception('Booking notification worker failed; will retry')
            await asyncio.sleep(2)
