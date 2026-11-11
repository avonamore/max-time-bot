import csv
import io
from datetime import timedelta
from app.timecalc import aware
from conftest import command


def export(cfg, now, args='сегодня all'):
    text, data = command(cfg, 1, '/csv ' + args, now)
    assert data.startswith(b'\xef\xbb\xbf')
    return text, list(csv.DictReader(io.StringIO(data.decode('utf-8-sig')), delimiter=';'))


def test_two_intervals_same_employee_same_day(cfg, now, approved):
    start = aware('2026-11-02T08:00+06:00')
    command(cfg, 2, 'Начать работу', start)
    command(cfg, 2, 'Начать перерыв', start + timedelta(hours=2))
    command(cfg, 2, 'Закончить перерыв', start + timedelta(hours=3))
    command(cfg, 2, 'Завершить работу', start + timedelta(hours=4))
    command(cfg, 2, 'Начать работу', start + timedelta(hours=5))
    command(cfg, 2, 'Завершить работу', start + timedelta(hours=10))
    text, rows = export(cfg, now)
    assert len(rows) == 2
    assert [r['Начало работы'] for r in rows] == ['02.11.2026 08:00:00', '02.11.2026 13:00:00']
    assert [r['Окончание работы'] for r in rows] == ['02.11.2026 12:00:00', '02.11.2026 18:00:00']
    assert [r['Рабочее время'] for r in rows] == ['03:00:00', '05:00:00']
    assert rows[0]['Перерывы'] == '01:00:00'
    assert rows[0]['ФИО'] == rows[1]['ФИО'] == 'Иванов Иван Иванович'
    assert rows[0]['№ интервала'] != rows[1]['№ интервала']
    assert 'Смен' not in rows[0]
    assert '02.11.2026 08:00:00' in text and '02.11.2026 13:00:00' in text
    assert 'смен 2' not in text


def test_overnight_actual_endpoints_and_clipped_hours(cfg, now, approved):
    start = aware('2026-11-02T23:00+06:00')
    command(cfg, 2, 'Начать работу', start)
    command(cfg, 2, 'Начать перерыв', start + timedelta(minutes=30))
    command(cfg, 2, 'Закончить перерыв', start + timedelta(hours=1, minutes=30))
    command(cfg, 2, 'Завершить работу', start + timedelta(hours=4))
    _, rows = export(cfg, start+timedelta(hours=5), '2026-11-03 2026-11-03 all')
    assert len(rows) == 1
    row = rows[0]
    assert row['Дата'] == '02.11.2026'
    assert row['Начало работы'] == '02.11.2026 23:00:00'
    assert row['Окончание работы'] == '03.11.2026 03:00:00'
    assert row['Перерывы'] == '00:30:00' and row['Рабочее время'] == '02:30:00'
    assert 'только за выбранный период' in row['Примечание']


def test_open_interval_visible_but_not_tallied_or_closed(cfg, now, approved):
    command(cfg, 2, 'Начать работу', now)
    _, rows = export(cfg, now)
    assert len(rows) == 1
    assert rows[0]['Статус'] == 'Не завершен'
    assert rows[0]['Окончание работы'] == rows[0]['Рабочее время'] == rows[0]['Перерывы'] == ''
    assert 'Открытой смены нет' not in command(cfg, 2, '/status', now)[0]
    _, future = export(cfg, now, '2026-12-01 2026-12-02 all')
    assert future == []


def test_filter_employee_and_empty_period(cfg, now, approved):
    command(cfg, 2, 'Начать работу', now)
    command(cfg, 3, 'Тестовый Второй', now)
    command(cfg, 1, '/approve 3', now)
    command(cfg, 3, 'Начать работу', now)
    _, rows = export(cfg, now, 'сегодня 2')
    assert len(rows) == 1 and rows[0]['ФИО'] == 'Иванов Иван Иванович'
    text, rows = export(cfg, now, '2026-10-01 2026-10-31 all')
    assert rows == [] and 'интервалов работы нет' in text


def test_detailed_report_uses_approved_correction(cfg, now, approved):
    start = aware('2026-11-02T08:00+06:00')
    command(cfg, 2, 'Начать работу', start)
    command(cfg, 2, 'Завершить работу', start+timedelta(hours=4))
    command(cfg, 2, f'/correct 1 {start.isoformat()} {(start+timedelta(hours=5)).isoformat()} - | Уточнение окончания', now)
    _, rows = export(cfg, now)
    assert rows[0]['Окончание работы'] == '02.11.2026 12:00:00'
    command(cfg, 1, '/accept 1', now)
    _, rows = export(cfg, now)
    assert rows[0]['Окончание работы'] == '02.11.2026 13:00:00'
    assert rows[0]['Рабочее время'] == '05:00:00'
