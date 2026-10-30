from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from app.events import ingest
from app.service import Service
from app.worker import process_one


def handle(pool, cfg, now, uid, text, at=None, callback_id=None):
    with pool.connection() as c:
        Service(c, cfg, now).handle(
            {"user_id": uid, "text": text, "at": (at or now).isoformat(), "callback_id": callback_id}
        )


def query(pool, sql, params=()):
    with pool.connection() as c:
        return c.execute(sql, params).fetchall()


def setup_users(pool, cfg, now):
    handle(pool, cfg, now, 1, "/start")
    for uid in (2, 3):
        handle(pool, cfg, now, uid, "/start")
        handle(pool, cfg, now, uid, f"Иван Иванов {uid}")
        handle(pool, cfg, now, 1, f"/одобрить {uid}")


def messages(pool, uid):
    return "\n".join(
        r["payload"].get("text", "")
        for r in query(pool, "SELECT payload FROM outbox WHERE recipient_id=%s ORDER BY id", (uid,))
    )


def test_registration_and_server_role_checks(pool, cfg, now):
    handle(pool, cfg, now, 2, "/начать")
    assert not query(pool, "SELECT * FROM shifts")
    handle(pool, cfg, now, 2, "Иван Иванов")
    handle(pool, cfg, now, 2, "/одобрить 2")
    assert query(pool, "SELECT status FROM employees WHERE max_id=2")[0]["status"] == "pending"
    handle(pool, cfg, now, 1, "/start")
    handle(pool, cfg, now, 1, "/одобрить 2")
    handle(pool, cfg, now, 2, "/начать")
    assert len(query(pool, "SELECT * FROM shifts")) == 1
    handle(pool, cfg, now, 2, "/отчет месяц 1")
    assert "только администратору" in messages(pool, 2)


def test_transitions_repeat_clicks_and_close_during_break(pool, cfg, now):
    setup_users(pool, cfg, now)
    start = now - timedelta(hours=10)
    handle(pool, cfg, now, 2, "/перерыв", start)
    assert not query(pool, "SELECT * FROM breaks")
    handle(pool, cfg, now, 2, "/начать", start)
    handle(pool, cfg, now, 2, "/начать", start)
    handle(pool, cfg, now, 2, "/перерыв", start + timedelta(hours=2))
    handle(pool, cfg, now, 2, "/перерыв", start + timedelta(hours=2))
    handle(pool, cfg, now, 2, "/закончить", start + timedelta(hours=3))
    handle(pool, cfg, now, 2, "/закончить", start + timedelta(hours=3))
    assert len(query(pool, "SELECT * FROM shifts")) == 1
    parts = query(pool, "SELECT * FROM breaks")
    assert len(parts) == 1 and parts[0]["ended_at"] == start + timedelta(hours=3)
    assert "чистое время: 02:00:00" in messages(pool, 2)


def test_end_break_and_stale_event(pool, cfg, now):
    setup_users(pool, cfg, now)
    handle(pool, cfg, now, 2, "/начать", now - timedelta(hours=4))
    handle(pool, cfg, now, 2, "/перерыв", now - timedelta(hours=3))
    handle(pool, cfg, now, 2, "/вернуться", now - timedelta(hours=2))
    handle(pool, cfg, now, 2, "/закончить", now - timedelta(hours=3))
    assert query(pool, "SELECT ended_at FROM shifts")[0]["ended_at"] is None
    handle(pool, cfg, now, 2, "/закончить", now)
    assert query(pool, "SELECT ended_at FROM shifts")[0]["ended_at"] == now


def test_duplicate_delivery_is_exactly_once(pool, cfg, now):
    setup_users(pool, cfg, now)
    event = {"key": "event-1", "user_id": 2, "text": "/начать", "at": now.isoformat(), "callback_id": None}
    with pool.connection() as c:
        assert ingest(c, event)
        assert not ingest(c, event)
    assert process_one(pool, cfg)
    before = len(query(pool, "SELECT * FROM outbox"))
    assert not process_one(pool, cfg)
    with pool.connection() as c:
        assert not ingest(c, event)
    assert len(query(pool, "SELECT * FROM outbox")) == before
    assert len(query(pool, "SELECT * FROM shifts")) == 1


def test_atomic_concurrent_starts(pool, cfg, now):
    setup_users(pool, cfg, now)
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = [executor.submit(handle, pool, cfg, now, 2, "/начать") for _ in range(12)]
        for f in futures:
            f.result()
    assert len(query(pool, "SELECT * FROM shifts")) == 1


def test_old_button_cannot_start_another_shift(pool, cfg, now):
    setup_users(pool, cfg, now)
    handle(pool, cfg, now, 2, "/статус")
    token = query(pool, "SELECT menu_token FROM employees WHERE max_id=2")[0]["menu_token"]
    handle(pool, cfg, now, 2, f"{token}:/начать", now - timedelta(hours=1), "a")
    handle(pool, cfg, now, 2, "/закончить", now)
    handle(pool, cfg, now, 2, f"{token}:/начать", now, "b")
    assert len(query(pool, "SELECT * FROM shifts")) == 1


def test_blocked_employee_and_personal_report_isolation(pool, cfg, now):
    setup_users(pool, cfg, now)
    handle(pool, cfg, now, 3, "/начать", now - timedelta(hours=5))
    handle(pool, cfg, now, 3, "/закончить", now)
    handle(pool, cfg, now, 2, "/часы месяц")
    assert "05:00:00" not in messages(pool, 2)
    handle(pool, cfg, now, 1, "/блокировать 2")
    handle(pool, cfg, now, 2, "/начать")
    assert not query(pool, "SELECT * FROM shifts WHERE employee_id=2")


def closed_shift(pool, cfg, now):
    setup_users(pool, cfg, now)
    handle(pool, cfg, now, 2, "/начать", now - timedelta(hours=5))
    handle(pool, cfg, now, 2, "/закончить", now - timedelta(hours=1))
    return query(pool, "SELECT * FROM shifts")[0]


def request(pool, cfg, now, sid=1, uid=2):
    a = (
        (now - timedelta(hours=6))
        .astimezone(__import__("zoneinfo").ZoneInfo(cfg.org_timezone))
        .strftime("%Y-%m-%d %H:%M")
    )
    b = (
        (now - timedelta(hours=1))
        .astimezone(__import__("zoneinfo").ZoneInfo(cfg.org_timezone))
        .strftime("%Y-%m-%d %H:%M")
    )
    handle(pool, cfg, now, uid, f"/исправить {sid} | {a} | {b} | - | Забыл отметить начало")


def test_correction_approval_audit_and_ownership(pool, cfg, now):
    s = closed_shift(pool, cfg, now)
    request(pool, cfg, now, s["id"], 3)
    assert not query(pool, "SELECT * FROM corrections")
    request(pool, cfg, now)
    assert query(pool, "SELECT * FROM shifts")[0]["started_at"] == s["started_at"]
    handle(pool, cfg, now, 2, "/принять 1")
    assert query(pool, "SELECT status FROM corrections")[0]["status"] == "pending"
    handle(pool, cfg, now, 1, "/принять 1")
    assert query(pool, "SELECT * FROM shifts")[0]["started_at"] == now - timedelta(hours=6)
    audit = query(pool, "SELECT * FROM audit_log WHERE action='correction_approved'")
    assert len(audit) == 1 and audit[0]["author_id"] == 1 and audit[0]["old_values"] != audit[0]["new_values"]
    handle(pool, cfg, now, 1, "/принять 1")
    assert len(query(pool, "SELECT * FROM audit_log WHERE action='correction_approved'")) == 1


def test_correction_rejection(pool, cfg, now):
    s = closed_shift(pool, cfg, now)
    request(pool, cfg, now)
    handle(pool, cfg, now, 1, "/отклонить 1")
    assert query(pool, "SELECT status FROM corrections")[0]["status"] == "rejected"
    assert query(pool, "SELECT * FROM shifts")[0]["started_at"] == s["started_at"]


def test_stale_correction_and_overlapping_breaks(pool, cfg, now):
    closed_shift(pool, cfg, now)
    request(pool, cfg, now)
    request(pool, cfg, now)
    handle(pool, cfg, now, 1, "/принять 1")
    handle(pool, cfg, now, 1, "/принять 2")
    assert query(pool, "SELECT status FROM corrections WHERE id=2")[0]["status"] == "pending"
    with pool.connection() as c:
        service = Service(c, cfg, now)
        import pytest

        from app.timecalc import RuleError

        bad = {
            "start": (now - timedelta(hours=6)).isoformat(),
            "end": now.isoformat(),
            "breaks": [
                [(now - timedelta(hours=5)).isoformat(), (now - timedelta(hours=3)).isoformat()],
                [(now - timedelta(hours=4)).isoformat(), (now - timedelta(hours=2)).isoformat()],
            ],
        }
        with pytest.raises(RuleError):
            service.validate_correction(2, 1, bad)


def test_restart_preserves_open_shift_and_break(pool, cfg, now):
    setup_users(pool, cfg, now)
    handle(pool, cfg, now, 2, "/начать", now - timedelta(hours=2))
    handle(pool, cfg, now, 2, "/перерыв", now - timedelta(hours=1))
    # New database connection and service instance, no process-local accounting state.
    with pool.connection() as c:
        service = Service(c, cfg, now)
        assert "на перерыве" in service.status(2)
        rows = service.report(2, now - timedelta(days=1), now + timedelta(days=1))
        assert rows[0]["net"] == 0 and rows[0]["open_count"] == 1


def test_future_action_and_shift_overlap_rejected(pool, cfg, now):
    closed_shift(pool, cfg, now)
    handle(pool, cfg, now, 2, "/начать", now - timedelta(hours=2))
    handle(pool, cfg, now, 2, "/начать", now + timedelta(hours=1))
    assert len(query(pool, "SELECT * FROM shifts")) == 1


def test_correction_overlap_and_own_admin_request(pool, cfg, now):
    closed_shift(pool, cfg, now)
    handle(pool, cfg, now, 2, "/начать", now - timedelta(minutes=30))
    with pool.connection() as c:
        import pytest

        from app.timecalc import RuleError

        with pytest.raises(RuleError):
            Service(c, cfg, now).validate_correction(
                2, 1, {"start": (now - timedelta(hours=6)).isoformat(), "end": now.isoformat(), "breaks": []}
            )
    handle(pool, cfg, now, 1, "/назначить_админа 2")
    request(pool, cfg, now)
    handle(pool, cfg, now, 2, "/принять 1")
    assert query(pool, "SELECT status FROM corrections")[0]["status"] == "pending"


def test_concurrent_break_start_and_finish_are_atomic(pool, cfg, now):
    setup_users(pool, cfg, now)
    handle(pool, cfg, now, 2, "/начать", now - timedelta(hours=3))
    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = [
            ex.submit(handle, pool, cfg, now, 2, "/перерыв", now - timedelta(hours=2)) for _ in range(8)
        ]
        for f in futures:
            f.result()
    assert len(query(pool, "SELECT * FROM breaks")) == 1
    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = [ex.submit(handle, pool, cfg, now, 2, "/закончить", now) for _ in range(8)]
        for f in futures:
            f.result()
    assert query(pool, "SELECT ended_at FROM breaks")[0]["ended_at"] == now
    assert query(pool, "SELECT ended_at FROM shifts")[0]["ended_at"] == now


def test_overnight_correction_with_break_approval(pool, cfg, now):
    closed_shift(pool, cfg, now)
    # Omsk midnight at 18:00 UTC, entire new shift ends before now.
    handle(
        pool,
        cfg,
        now,
        2,
        "/исправить 1 | 2026-10-28 23:00 | 2026-10-29 03:00 | "
        "2026-10-28 23:30 / 2026-10-29 00:30 | Исправляю ночную смену",
    )
    handle(pool, cfg, now, 1, "/принять 1")
    with pool.connection() as c:
        from app.timecalc import period

        svc = Service(c, cfg, now)
        rows = svc.report(2, *period("2026-10-29 2026-10-29", now, cfg.org_timezone))
    assert rows[0]["count"] == 1
    assert rows[0]["gross"] == 3 * 3600 and rows[0]["breaks"] == 1800 and rows[0]["net"] == 2.5 * 3600


def test_closed_zero_duration_shift_counts(pool, cfg, now):
    setup_users(pool, cfg, now)
    handle(pool, cfg, now, 2, "/начать", now)
    handle(pool, cfg, now, 2, "/закончить", now)
    with pool.connection() as c:
        rows = Service(c, cfg, now).report(2, now, now + timedelta(days=1))
    assert rows[0]["count"] == 1 and rows[0]["net"] == 0


def test_zero_admin_id_never_grants_role(pool, cfg, now):
    cfg.initial_admin_max_id = 0
    handle(pool, cfg, now, 1, "/мой_id")
    row = query(pool, "SELECT * FROM employees WHERE max_id=1")[0]
    assert row["status"] == "pending" and row["role"] == "employee"
