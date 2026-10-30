import asyncio
import logging
import random
from datetime import UTC, datetime, timedelta

from psycopg.types.json import Jsonb

from app.db import BUSINESS_LOCK
from app.max_api import APIError
from app.service import Service
from app.timecalc import RuleError, csv_report

log = logging.getLogger(__name__)


def process_one(pool, config):
    with pool.connection() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (BUSINESS_LOCK,))
        row = conn.execute(
            "SELECT * FROM inbox WHERE processed_at IS NULL ORDER BY id LIMIT 1 FOR UPDATE"
        ).fetchone()
        if not row:
            return False
        Service(conn, config).handle(row["event"])
        conn.execute("UPDATE inbox SET processed_at=now(),event='{}'::jsonb WHERE id=%s", (row["id"],))
        return True


async def deliver_one(pool, config, api):
    # Row remains locked until network attempt finishes. Crash => safe retry, potentially duplicate reply,
    # but accounting is already committed exactly once. MAX has no documented send idempotency key.
    with pool.connection() as conn:
        # Read all CSV tables and permissions from one consistent database snapshot.
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        row = conn.execute("""SELECT * FROM outbox WHERE sent_at IS NULL AND failed_at IS NULL AND available_at<=now()
            ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED""").fetchone()
        if not row:
            return False
        uid, payload = row["recipient_id"], dict(row["payload"])
        required = payload.pop("_access", None)
        user = conn.execute("SELECT * FROM employees WHERE max_id=%s", (uid,)).fetchone()
        if required and (user["status"] != required["status"] or user["role"] != required["role"]):
            conn.execute(
                "UPDATE outbox SET failed_at=now(),last_error='access_revoked',payload='{}' WHERE id=%s",
                (row["id"],),
            )
            return True
        try:
            if row["kind"] == "answer":
                await api.answer(payload)
            elif row["kind"] == "csv":
                service = Service(conn, config)
                service.admin(user)
                # A successful upload token survives retries; the actual CSV bytes never go in public storage here.
                if "attachment" not in payload:
                    rows = service.report(
                        payload["target"],
                        datetime.fromisoformat(payload["start"]),
                        datetime.fromisoformat(payload["end"]),
                    )
                    attachment = await api.upload_file(csv_report(rows))
                    payload["attachment"] = attachment
                    conn.execute(
                        "UPDATE outbox SET payload=%s WHERE id=%s",
                        (Jsonb({**payload, "_access": required}), row["id"]),
                    )
                await api.send_message(
                    uid,
                    {
                        "text": f"Отчет за {payload['spec']}. Часовой пояс: {config.org_timezone}.",
                        "attachments": [payload["attachment"]],
                    },
                )
            else:
                await api.send_message(uid, payload)
        except (APIError, RuleError) as exc:
            status = exc.status if isinstance(exc, APIError) else 403
            attempts = row["attempts"] + 1
            # The documented upload-processing 400 may be transient; retry it only for file messages.
            retryable = status in (0, 429, 500, 502, 503, 504) or (status == 400 and row["kind"] == "csv")
            terminal = not retryable or attempts >= 12
            retry_after = exc.retry_after if isinstance(exc, APIError) else None
            delay = retry_after or min(3600, 2 ** min(attempts, 11)) + random.random()
            conn.execute(
                """UPDATE outbox SET attempts=%s,last_error=%s,available_at=%s,failed_at=%s WHERE id=%s""",
                (
                    attempts,
                    f"HTTP_{status}",
                    datetime.now(UTC) + timedelta(seconds=delay),
                    datetime.now(UTC) if terminal else None,
                    row["id"],
                ),
            )
            # Never log requests, response bodies, URLs, employee names, file tokens or bot token.
            log.warning("MAX delivery failure: outbox_id=%s code=%s terminal=%s", row["id"], status, terminal)
            if terminal and row["kind"] == "csv":
                Service(conn, config).emit(
                    uid,
                    "Не удалось отправить CSV. Администратору сервера: проверьте "
                    "настройку хоста загрузки MAX и журнал ошибок. Затем повторите /csv.",
                )
            return True
        conn.execute("UPDATE outbox SET sent_at=now(),payload='{}'::jsonb WHERE id=%s", (row["id"],))
        return True


async def run(pool, config, api):
    while True:
        busy = False
        try:
            # Separate connection/network transaction from processing business commands.
            busy = await asyncio.to_thread(process_one, pool, config)
            busy = await deliver_one(pool, config, api) or busy
        except Exception as exc:
            # Exception messages can contain SQL parameters or URL secrets; log class only.
            log.error("Worker failure: %s", type(exc).__name__)
        if not busy:
            await asyncio.sleep(0.5)
