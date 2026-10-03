from datetime import date, datetime, time, timedelta
from urllib.parse import urlparse
from zoneinfo import ZoneInfo
import re

MSK = ZoneInfo('Europe/Moscow')
OWNER_ID = 439275668


class Problem(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def now_msk():
    return datetime.now(MSK)


def day(value):
    try:
        if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            raise ValueError
        result = date.fromisoformat(value)
        if not 2020 <= result.year <= 2100:
            raise ValueError
        return result
    except (ValueError, TypeError):
        raise Problem('Укажите корректную дату смены.')


def clock(value):
    if not isinstance(value, str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', value):
        raise Problem('Укажите время в формате ЧЧ:ММ.')
    return time.fromisoformat(value)


def shift_end(d):
    return datetime.combine(d + timedelta(days=1), time(3 if d.weekday() in (4, 5) else 0), MSK)


def current_shift(now):
    now = now.astimezone(MSK)
    previous = now.date() - timedelta(days=1)
    return previous if now < shift_end(previous) else now.date()


def due_at(d, digest_time):
    return datetime.combine(d, clock(digest_time), MSK)


def in_notice_window(d, now, digest_time, announced=False):
    return (announced or now >= due_at(d, digest_time)) and datetime.combine(d, time(), MSK) <= now < shift_end(d)


def booking_datetime(d, value):
    t = clock(value)
    return datetime.combine(d + timedelta(days=int(t.hour < 18)), t, MSK)


def clean_text(data, key, label, limit, required=False):
    value = data.get(key, '')
    if not isinstance(value, str):
        raise Problem(f'Поле «{label}» должно быть текстом.')
    value = value.strip()
    if (required and not value) or len(value) > limit:
        raise Problem(f'Поле «{label}»: от {1 if required else 0} до {limit} символов.')
    return value


def positive_int(value, label, maximum):
    if type(value) is not int or not 1 <= value <= maximum:
        raise Problem(f'Поле «{label}»: целое число от 1 до {maximum}.')
    return value


def validate_booking(data):
    if not isinstance(data, dict):
        raise Problem('Некорректные данные брони.')
    d = day(data.get('shift_date'))
    name = clean_text(data, 'guest_name', 'ФИО', 160, True)
    link = clean_text(data, 'guest_link', 'Ссылка', 300)
    if link.startswith('@'):
        link = 'https://t.me/' + link[1:]
    if link:
        parsed = urlparse(link)
        if parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username or parsed.password or any(c.isspace() for c in link):
            raise Problem('Укажите ссылку http(s)://… или @username.')
    start = booking_datetime(d, data.get('start_time'))
    opening = datetime.combine(d, time(18), MSK)
    closing = shift_end(d)
    if not opening <= start < closing:
        raise Problem('Начало должно попадать в часы смены: 18:00–' + closing.strftime('%H:%M') + '.')
    end_value = data.get('end_time') or None
    if end_value is not None:
        end = booking_datetime(d, end_value)
        if not start < end <= closing:
            raise Problem('Окончание должно быть позже начала и не позже закрытия бара.')
    return dict(shift_date=d.isoformat(), guest_name=name, guest_link=link,
                table_no=positive_int(data.get('table_no'), 'Стол', 17),
                guests=positive_int(data.get('guests'), 'Количество гостей', 999),
                start_time=data['start_time'], end_time=end_value,
                comment=clean_text(data, 'comment', 'Комментарий', 1000))


def time_label(b):
    return b['start_time'] + ('–' + b['end_time'] if b['end_time'] else '')


def booking_text(b):
    text = f"Стол №{b['table_no']} · {time_label(b)}\n{b['guest_name']} · гостей: {b['guests']}"
    if b['guest_link']:
        text += '\n' + b['guest_link']
    if b['comment']:
        text += '\nКомментарий: ' + b['comment']
    return text


def digest_texts(d, bookings, manual=False):
    if not bookings:
        return []
    header = f"{'Сводка по запросу' if manual else 'Сегодня'}: броней — {len(bookings)}, гостей — {sum(b['guests'] for b in bookings)}.\nСмена {d.strftime('%d.%m.%Y')}"
    parts, text = [], header
    for b in bookings:
        block = '\n\n' + booking_text(b)
        # Telegram counts UTF-16 code units. Keep room even with emoji-heavy comments.
        if len((text + block).encode('utf-16-le')) // 2 > 3800:
            parts.append(text)
            text = f"Смена {d.strftime('%d.%m.%Y')} · продолжение"
        text += block
    parts.append(text)
    return parts
