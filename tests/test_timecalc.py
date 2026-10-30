from datetime import UTC, datetime

import pytest

from app.timecalc import RuleError, csv_report, local_datetime, period, totals


def dt(v):
    return datetime.fromisoformat(v).replace(tzinfo=UTC)


def test_midnight_clipping_breaks_and_open_shifts():
    shifts = [
        {"id": 1, "started_at": dt("2026-10-28T17:00"), "ended_at": dt("2026-10-29T03:00")},
        {"id": 2, "started_at": dt("2026-10-29T04:00"), "ended_at": None},
    ]
    breaks = [{"shift_id": 1, "started_at": dt("2026-10-28T17:30"), "ended_at": dt("2026-10-28T18:30")}]
    start, end = period("2026-10-29 2026-10-29", dt("2026-10-29T10:00"), "Asia/Omsk")
    assert start == dt("2026-10-28T18:00")
    assert totals(shifts, breaks, start, end) == {
        "count": 1,
        "gross": 9 * 3600,
        "breaks": 1800,
        "net": 8.5 * 3600,
    }


def test_interval_end_exclusive():
    shifts = [{"id": 1, "started_at": dt("2026-10-28T17:00"), "ended_at": dt("2026-10-28T18:00")}]
    assert totals(shifts, [], dt("2026-10-28T18:00"), dt("2026-10-29T18:00"))["count"] == 0


def test_week_month_year_boundary():
    now = dt("2027-01-01T12:00")
    assert period("неделя", now, "UTC") == (dt("2026-12-28T00:00"), dt("2027-01-04T00:00"))
    assert period("месяц", now, "UTC") == (dt("2027-01-01T00:00"), dt("2027-02-01T00:00"))


@pytest.mark.parametrize("text", ["2026-10-30 2026-10-29", "2020-01-01 2026-01-01", "wrong"])
def test_invalid_period(text, now):
    with pytest.raises(RuleError):
        period(text, now, "Asia/Omsk")


def test_dst_days_are_not_fixed_24h(now):
    a, b = period("2026-03-29 2026-03-29", now, "Europe/Berlin")
    assert (b - a).total_seconds() == 23 * 3600
    a, b = period("2026-10-25 2026-10-25", now, "Europe/Berlin")
    assert (b - a).total_seconds() == 25 * 3600


@pytest.mark.parametrize("text", ["2026-03-29 02:30", "2026-10-25 02:30"])
def test_dst_ambiguous_nonexistent_rejected(text):
    with pytest.raises(RuleError):
        local_datetime(text, "Europe/Berlin")


def test_explicit_offset_resolves_dst():
    assert local_datetime("2026-10-25 02:30+02:00", "Europe/Berlin") == dt("2026-10-25T00:30")


def test_excel_csv_bom_russian_and_formula_injection():
    data = csv_report(
        [dict(max_id=3, full_name="=Иван Иванов", count=1, gross=3600, breaks=60, net=3540, open_count=0)]
    )
    assert data.startswith(b"\xef\xbb\xbf")
    decoded = data.decode("utf-8-sig")
    assert "ФИО" in decoded and "'=Иван Иванов" in decoded and "00:59:00" in decoded
