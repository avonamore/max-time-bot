"""Detailed report: one row per recorded start/end interval, never per payroll shift."""
from collections import defaultdict
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
from .timecalc import totals, duration

HEADERS = ['ФИО', 'Дата', 'Начало работы', 'Окончание работы', 'Перерывы',
           'Рабочее время', 'Статус', '№ интервала', 'Примечание', 'Итого за день']


def interval_row(name, shift, breaks, left, right, tz):
    zone = ZoneInfo(tz)
    start, end = shift['started_at'], shift['ended_at']
    local = start.astimezone(zone)
    notes = [f'Часовой пояс: {tz}']
    if end is None:
        rest = net = ''
        status = 'Не завершен'
        notes.append('Не включен в итоги завершенных интервалов')
    else:
        _, rest_seconds, net_seconds = totals(start, end, breaks, left, right)
        rest, net = duration(rest_seconds), duration(net_seconds)
        status = 'Завершен'
        if start < left or end > right:
            notes.append('Время учтено только за выбранный период: '
                         f'{max(start, left).astimezone(zone):%d.%m.%Y %H:%M:%S} — '
                         f'{min(end, right).astimezone(zone):%d.%m.%Y %H:%M:%S}')
    # Full dates in both endpoint columns make overnight intervals unambiguous.
    return [name, local.strftime('%d.%m.%Y'), local.strftime('%d.%m.%Y %H:%M:%S'),
            end.astimezone(zone).strftime('%d.%m.%Y %H:%M:%S') if end else '',
            rest, net, status, shift['id'], '; '.join(notes), '']


def interval_text(row):
    name, date, start, end, rest, net, status, sid, note, daily = row
    if status == 'Итого за день':
        return f'{name} | {date} | Итого за день: {daily} (без перерывов, только завершенные интервалы).'
    if not end:
        return f'{name} | №{sid} | {start} — не завершен (в итоги не включен).'
    return (f'{name} | №{sid} | {start} — {end}\n'
            f'Перерывы: {rest}; рабочее время: {net}. {note}')


def with_daily_totals(name, rows, shifts, breaks_by_shift, left, right, tz):
    """Daily sums use raw UTC intervals, not formatted/truncated CSV durations."""
    zone = ZoneInfo(tz)
    grouped = defaultdict(list)
    for row in rows:
        grouped[datetime.strptime(row[1], '%d.%m.%Y').date()].append(row)
    daily = defaultdict(float)
    for shift in shifts:
        if shift['ended_at'] is None:
            continue
        cursor = max(shift['started_at'], left)
        limit = min(shift['ended_at'], right)
        while cursor < limit:
            day = cursor.astimezone(zone).date()
            next_day = datetime.combine(day + timedelta(days=1), time.min, zone).astimezone(timezone.utc)
            stop = min(limit, next_day)
            _, _, net = totals(shift['started_at'], shift['ended_at'],
                               breaks_by_shift[shift['id']], cursor, stop)
            daily[day] += net
            cursor = stop
    result = []
    for day in sorted(set(grouped) | set(daily)):
        result.extend(grouped[day])
        if day in daily:
            result.append([name, day.strftime('%d.%m.%Y'), '', '', '', '', 'Итого за день', '',
                           f'Без перерывов; только завершенные интервалы; часовой пояс: {tz}',
                           duration(daily[day])])
    return result
