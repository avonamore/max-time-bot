from datetime import datetime, timezone
import pytest
from app.timecalc import aware, totals, period, validate_intervals, RuleError, duration


def d(value):
    return aware(value)


def test_midnight_clipped_break():
    start, end = d('2026-11-02T23:00+06:00'), d('2026-11-03T03:00+06:00')
    pauses = [(d('2026-11-02T23:30+06:00'), d('2026-11-03T00:30+06:00'))]
    a, b = period('2026-11-03', end, 'Asia/Omsk', ('2026-11-03','2026-11-03'))
    assert totals(start, end, pauses, a, b) == (10800, 1800, 9000)
    a, b = period('2026-11-02', end, 'Asia/Omsk', ('2026-11-02','2026-11-02'))
    assert totals(start, end, pauses, a, b) == (3600, 1800, 1800)


def test_open_excluded():
    now = d('2026-11-02T12:00Z')
    assert totals(now, None, [], now, now) == (0, 0, 0)


def test_week_monday_month_year_timezone():
    now = d('2026-12-31T23:00Z')  # Jan 1 in Omsk
    a,b = period('месяц', now, 'Asia/Omsk')
    assert a == d('2027-01-01T00:00+06:00') and b == d('2027-02-01T00:00+06:00')
    a,b = period('неделя', now, 'Asia/Omsk')
    assert a == d('2026-12-28T00:00+06:00') and (b-a).days == 7


def test_dst_calendar_day():
    a,b = period('2026-03-29', d('2026-03-29T10:00Z'), 'Europe/Berlin', ('2026-03-29','2026-03-29'))
    assert (b-a).total_seconds() == 23*3600


@pytest.mark.parametrize('value', ['2026-11-02T10:00', '2026-11-02'])
def test_naive_rejected(value):
    with pytest.raises(RuleError):
        aware(value)


def test_overlapping_breaks_rejected():
    a,b,c = [d('2026-11-02T'+x+'Z') for x in ('09:00','10:00','11:00')]
    with pytest.raises(RuleError):
        validate_intervals(a,c,[(a,b),(a,c)])
    assert duration(100*3600+61) == '100:01:01'
