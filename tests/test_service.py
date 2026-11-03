from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import csv
import io
import pytest
from psycopg.errors import UniqueViolation
from app.db import connect, migrate
from app.service import Service
from app.timecalc import RuleError
from conftest import command


def test_registration_and_roles(cfg, now):
    command(cfg, 1, '/menu', now)
    assert 'ФИО' in command(cfg, 2, '/menu', now)[0]
    command(cfg, 2, 'Иванов Иван', now)
    assert 'Ожидайте' in command(cfg, 2, 'Начать работу', now)[0]
    with connect(cfg.database_url) as db:
        assert db.execute('SELECT count(*) n FROM shifts').fetchone()['n'] == 0
    command(cfg, 1, '/approve 2', now)
    with pytest.raises(RuleError):
        command(cfg, 2, '/approve 3', now)
    with pytest.raises(RuleError):
        command(cfg, 2, '/report сегодня all', now)
    with pytest.raises(RuleError):
        command(cfg, 2, '/csv сегодня all', now)
    with pytest.raises(RuleError):
        command(cfg, 2, '/hours сегодня 1', now)
    command(cfg, 1, '/block 2', now)
    with pytest.raises(RuleError):
        command(cfg, 2, '/status', now)
    with pytest.raises(RuleError):
        command(cfg, 1, '/block 1', now)


def test_transitions_auto_close_pause_and_restart(cfg, now, approved):
    with pytest.raises(RuleError):
        command(cfg, 2, 'Начать перерыв', now)
    command(cfg, 2, 'Начать работу', now)
    command(cfg, 2, 'Начать работу', now)
    command(cfg, 2, 'Начать перерыв', now+timedelta(hours=1))
    command(cfg, 2, 'Начать перерыв', now+timedelta(hours=1))
    # Reopen connection/service simulates restart: state is in PostgreSQL.
    assert 'На перерыве' in command(cfg, 2, '/status', now)[0]
    text = command(cfg, 2, 'Завершить работу', now+timedelta(hours=3))[0]
    assert '03:00:00' in text and '02:00:00' in text and '01:00:00' in text
    with connect(cfg.database_url) as db:
        assert db.execute('SELECT count(*) n FROM shifts').fetchone()['n'] == 1
        shift = db.execute('SELECT * FROM shifts').fetchone()
        rest = db.execute('SELECT * FROM breaks').fetchone()
        assert shift['ended_at'] == rest['ended_at']


def test_resume_and_invalid_order(cfg, now, approved):
    command(cfg, 2, 'Начать работу', now)
    command(cfg, 2, 'Начать перерыв', now+timedelta(hours=1))
    with pytest.raises(RuleError):
        command(cfg, 2, 'Закончить перерыв', now)
    command(cfg, 2, 'Закончить перерыв', now+timedelta(hours=2))
    command(cfg, 2, 'Закончить перерыв', now+timedelta(hours=2))
    text = command(cfg, 2, 'Завершить работу', now+timedelta(hours=4))[0]
    assert 'чистое время: 03:00:00' in text


def test_concurrent_start_unique_open(cfg, now, approved):
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: command(cfg, 2, 'Начать работу', now), range(16)))
    with connect(cfg.database_url) as db:
        assert db.execute('SELECT count(*) n FROM shifts').fetchone()['n'] == 1
    with pytest.raises(UniqueViolation):
        with connect(cfg.database_url) as db:
            db.execute('INSERT INTO shifts(employee_id,started_at) VALUES(2,%s)', (now,))


def test_stale_button_cannot_create_new_shift(cfg, now, approved):
    command(cfg, 2, 'start|0.0', now, True)
    command(cfg, 2, 'Завершить работу', now+timedelta(hours=1))
    with pytest.raises(RuleError):
        command(cfg, 2, 'start|0.0', now+timedelta(hours=2), True)


def test_personal_reports_and_open_excluded(cfg, now, approved):
    command(cfg, 2, 'Начать работу', now)
    assert 'смен 0' in command(cfg, 2, '/hours сегодня', now)[0]
    assert 'Незакрытых: 1' in command(cfg, 2, '/hours сегодня', now)[0]
    command(cfg, 2, 'Завершить работу', now+timedelta(hours=1))
    command(cfg, 3, '=Другой Сотрудник', now)
    command(cfg, 1, '/approve 3', now)
    assert 'Другой' not in command(cfg, 2, '/hours сегодня', now)[0]
    _, data = command(cfg, 1, '/csv сегодня all', now)
    assert data.startswith(b'\xef\xbb\xbf')
    rows = list(csv.reader(io.StringIO(data.decode('utf-8-sig')), delimiter=';'))
    assert rows[0][1] == 'ФИО'
    assert any(r[1].startswith("'=") for r in rows[1:])


def closed(cfg, now):
    command(cfg, 2, 'Начать работу', now)
    command(cfg, 2, 'Завершить работу', now+timedelta(hours=1))
    return 1


def proposal(now, shift=1, reason='Забыл отметить'):
    return f'/correct {shift} {now.isoformat()} {(now+timedelta(hours=2)).isoformat()} - | {reason}'


def test_correction_approval_audit_and_stale(cfg, now, approved):
    closed(cfg, now)
    command(cfg, 2, proposal(now), now+timedelta(hours=4))
    command(cfg, 2, proposal(now), now+timedelta(hours=4))
    with connect(cfg.database_url) as db:
        assert db.execute('SELECT ended_at-started_at d FROM shifts').fetchone()['d'] == timedelta(hours=1)
    with pytest.raises(RuleError):
        command(cfg, 2, '/accept 1', now)
    command(cfg, 1, '/accept 1', now+timedelta(hours=5))
    with pytest.raises(RuleError):
        command(cfg, 1, '/accept 2', now+timedelta(hours=5))
    with pytest.raises(RuleError):
        command(cfg, 1, '/accept 1', now+timedelta(hours=5))
    with connect(cfg.database_url) as db:
        audit = db.execute('SELECT * FROM audit').fetchone()
        assert audit['author_id'] == 2 and audit['reviewer_id'] == 1
        assert audit['old_values']['end'] != audit['new_values']['end']
        assert audit['reason'] == 'Забыл отметить'
        assert db.execute('SELECT ended_at-started_at d FROM shifts').fetchone()['d'] == timedelta(hours=2)


def test_correction_ownership_reject_and_overlap(cfg, now, approved):
    closed(cfg, now)
    command(cfg, 3, 'Петров Петр', now)
    command(cfg, 1, '/approve 3', now)
    with pytest.raises(RuleError):
        command(cfg, 3, proposal(now), now+timedelta(hours=5))
    command(cfg, 2, proposal(now), now+timedelta(hours=5))
    command(cfg, 1, '/reject 1', now)
    with connect(cfg.database_url) as db:
        assert db.execute('SELECT count(*) n FROM audit').fetchone()['n'] == 0
    command(cfg, 2, 'Начать работу', now+timedelta(hours=1, minutes=30))
    command(cfg, 2, proposal(now), now+timedelta(hours=5))
    with pytest.raises(RuleError):
        command(cfg, 1, '/accept 2', now+timedelta(hours=5))


def test_migrations_idempotent(cfg):
    migrate(cfg.database_url)
    migrate(cfg.database_url)
    with connect(cfg.database_url) as db:
        assert db.execute('SELECT count(*) n FROM schema_migrations').fetchone()['n'] >= 1


def test_actual_overnight_report_clipping(cfg, now, approved):
    from app.timecalc import aware
    start = aware('2026-11-02T23:00+06:00')
    command(cfg, 2, 'Начать работу', start)
    command(cfg, 2, 'Начать перерыв', start+timedelta(minutes=30))
    command(cfg, 2, 'Закончить перерыв', start+timedelta(hours=1, minutes=30))
    command(cfg, 2, 'Завершить работу', start+timedelta(hours=4))
    text = command(cfg, 2, '/hours 2026-11-03 2026-11-03', start+timedelta(hours=5))[0]
    assert 'длительность 03:00:00, перерывы 00:30:00, чистое 02:30:00' in text


def test_correction_replaces_breaks(cfg, now, approved):
    command(cfg, 2, 'Начать работу', now)
    command(cfg, 2, 'Завершить работу', now+timedelta(hours=4))
    rest = f'{(now+timedelta(hours=1)).isoformat()}/{(now+timedelta(hours=2)).isoformat()}'
    command(cfg, 2, f'/correct 1 {now.isoformat()} {(now+timedelta(hours=4)).isoformat()} {rest} | Добавить забытый перерыв', now+timedelta(hours=5))
    command(cfg, 1, '/accept 1', now+timedelta(hours=5))
    text = command(cfg, 2, '/hours сегодня', now)[0]
    # The shift crosses local midnight at 18:00 UTC; now=12 UTC, so all 4 hours are in this date.
    assert 'перерывы 01:00:00, чистое 03:00:00' in text


def test_id_before_approval_is_only_self(cfg, now):
    assert command(cfg, 98765, '/id', now)[0] == 'Ваш MAX user_id: 98765'


def test_registration_notifies_only_admin_once(cfg, now):
    command(cfg, 1, '/menu', now)
    command(cfg, 2, 'Новый Тестовый Сотрудник', now)
    command(cfg, 2, 'Новый Тестовый Сотрудник', now)
    command(cfg, 2, '/register Повторная Заявка', now)
    with connect(cfg.database_url) as db:
        rows = db.execute('SELECT * FROM outbox ORDER BY id').fetchall()
        assert len(rows) == 1
        assert rows[0]['recipient'] == 1 and rows[0]['sensitive']
        text = rows[0]['body']['text']
        assert 'Новый Тестовый Сотрудник' in text
        assert 'MAX ID: 2' in text and '/approve 2' in text
        assert db.execute('SELECT status FROM employees WHERE max_id=2').fetchone()['status'] == 'pending'


def test_registration_excludes_blocked_admin(cfg, now):
    command(cfg, 1, '/menu', now)
    with connect(cfg.database_url) as db:
        db.execute("INSERT INTO employees(max_id,full_name,role,status) VALUES(3,'Другой Администратор','admin','blocked')")
    command(cfg, 2, 'Новый Тестовый Сотрудник', now)
    with connect(cfg.database_url) as db:
        assert [r['recipient'] for r in db.execute('SELECT recipient FROM outbox').fetchall()] == [1]
