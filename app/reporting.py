"""Detailed report: one row per recorded start/end interval, never per payroll shift."""
from zoneinfo import ZoneInfo
from .timecalc import totals, duration

HEADERS = ['ФИО', 'Дата', 'Начало работы', 'Окончание работы', 'Перерывы',
           'Рабочее время', 'Статус', '№ интервала', 'Примечание']


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
            rest, net, status, shift['id'], '; '.join(notes)]


def interval_text(row):
    name, date, start, end, rest, net, status, sid, note = row
    if not end:
        return f'{name} | №{sid} | {start} — не завершен (в итоги не включен).'
    return (f'{name} | №{sid} | {start} — {end}\n'
            f'Перерывы: {rest}; рабочее время: {net}. {note}')
