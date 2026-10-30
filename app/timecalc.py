import csv
import io
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo


class RuleError(Exception):
    pass


def local_datetime(text: str, zone: str) -> datetime:
    try:
        value = datetime.fromisoformat(text.strip())
    except ValueError:
        raise RuleError("Дата: ГГГГ-ММ-ДД ЧЧ:ММ, при необходимости с UTC-смещением.") from None
    if value.tzinfo:
        return value.astimezone(UTC)
    tz = ZoneInfo(zone)
    options = [value.replace(tzinfo=tz, fold=f) for f in (0, 1)]
    valid = [v for v in options if v.astimezone(UTC).astimezone(tz).replace(tzinfo=None) == value]
    if not valid or (len(valid) == 2 and valid[0].utcoffset() != valid[1].utcoffset()):
        raise RuleError("Несуществующее или неоднозначное местное время: укажите UTC-смещение.")
    return valid[0].astimezone(UTC)


def period(spec: str, now: datetime, zone: str) -> tuple[datetime, datetime]:
    today = now.astimezone(ZoneInfo(zone)).date()
    try:
        if spec in ("сегодня", "день"):
            first, last = today, today
        elif spec == "неделя":
            first = today - timedelta(days=today.weekday())
            last = first + timedelta(days=6)
        elif spec == "месяц":
            first = today.replace(day=1)
            next_month = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
            last = next_month - timedelta(days=1)
        else:
            a, b = spec.split()
            first, last = date.fromisoformat(a), date.fromisoformat(b)
        if last < first or (last - first).days > 366:
            raise ValueError
        start = datetime.combine(first, time.min, ZoneInfo(zone)).astimezone(UTC)
        end = datetime.combine(last + timedelta(days=1), time.min, ZoneInfo(zone)).astimezone(UTC)
        return start, end
    except (ValueError, OverflowError):
        raise RuleError("Период: сегодня, неделя, месяц или ГГГГ-ММ-ДД ГГГГ-ММ-ДД (до 367 дней).") from None


def intersection(a, b, start, end) -> float:
    return max(0, (min(b, end) - max(a, start)).total_seconds())


def totals(shifts, breaks, start, end):
    count, gross, paused = 0, 0.0, 0.0
    for s in shifts:
        if s["ended_at"] is None:
            continue
        seconds = intersection(s["started_at"], s["ended_at"], start, end)
        if seconds <= 0:
            if s["started_at"] == s["ended_at"] and start <= s["started_at"] < end:
                count += 1
            continue
        count += 1
        gross += seconds
        for b in breaks:
            if b["shift_id"] == s["id"] and b["ended_at"] is not None:
                paused += intersection(
                    b["started_at"], b["ended_at"], max(start, s["started_at"]), min(end, s["ended_at"])
                )
    return {"count": count, "gross": gross, "breaks": paused, "net": gross - paused}


def duration(seconds):
    seconds = int(seconds)
    return f"{seconds // 3600:02}:{seconds % 3600 // 60:02}:{seconds % 60:02}"


def excel_safe(value):
    value = str(value)
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value


def csv_report(rows):
    output = io.StringIO(newline="")
    writer = csv.writer(output, delimiter=";", lineterminator="\r\n")
    writer.writerow(["MAX ID", "ФИО", "Смен", "Длительность", "Перерывы", "Рабочее время", "Открытых смен"])
    for r in rows:
        writer.writerow(
            [
                r["max_id"],
                excel_safe(r["full_name"]),
                r["count"],
                duration(r["gross"]),
                duration(r["breaks"]),
                duration(r["net"]),
                r["open_count"],
            ]
        )
    return output.getvalue().encode("utf-8-sig")
