"""Platform-independent UTC interval accounting. Period end is exclusive."""
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc


class RuleError(Exception):
    pass


def aware(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None or value.utcoffset() is None:
        raise RuleError('Укажите часовой пояс в ISO-дате: например 2026-11-02T09:00:00+06:00.')
    return value.astimezone(UTC)


def overlap(start, end, left, right):
    return max(0, (min(end, right) - max(start, left)).total_seconds())


def totals(start, end, breaks, left, right):
    if end is None:
        return (0, 0, 0)
    gross = overlap(start, end, left, right)
    rest = sum(overlap(a, b, max(start, left), min(end, right)) for a, b in breaks)
    return (gross, rest, gross - rest)


def period(kind, now, tz, dates=()):
    zone = ZoneInfo(tz)
    today = now.astimezone(zone).date()
    if kind == 'сегодня':
        first, last = today, today + timedelta(days=1)
    elif kind == 'неделя':
        first = today - timedelta(days=today.weekday())
        last = first + timedelta(days=7)
    elif kind == 'месяц':
        first = today.replace(day=1)
        last = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    else:
        if len(dates) != 2:
            raise RuleError('Период: сегодня | неделя | месяц | ГГГГ-ММ-ДД ГГГГ-ММ-ДД (обе даты включены).')
        first, last = (datetime.strptime(x, '%Y-%m-%d').date() for x in dates)
        last += timedelta(days=1)
    if last <= first:
        raise RuleError('Конец периода раньше начала.')
    return tuple(datetime.combine(d, time.min, zone).astimezone(UTC) for d in (first, last))


def validate_intervals(start, end, breaks):
    if end <= start:
        raise RuleError('Окончание смены должно быть позже начала.')
    previous = start
    for a, b in sorted(breaks):
        if a < previous or b < a or b > end:
            raise RuleError('Перерывы должны находиться внутри смены и не пересекаться.')
        previous = b


def duration(seconds):
    seconds = int(seconds)
    return f'{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}'
