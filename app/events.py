import hashlib
from datetime import UTC, datetime

from psycopg.types.json import Jsonb

from app.db import INBOX_LOCK


class BadEvent(ValueError):
    pass


def normalize(raw):
    """Take identity exclusively from authenticated MAX event, never command arguments."""
    if not isinstance(raw, dict):
        raise BadEvent("Ожидался объект")
    kind = raw.get("update_type")
    try:
        callback_id = None
        if kind == "bot_started":
            user, text, event_id = raw["user"], "/start", f"start:{raw['user']['user_id']}:{raw['timestamp']}"
            at = raw["timestamp"]
        elif kind in ("message_created", "message_callback"):
            message = raw.get("message")
            # A removed message's callback cannot establish a private context: fail closed.
            if not message:
                if kind == "message_callback":
                    return None
                raise BadEvent("Отсутствует сообщение")
            if message["recipient"]["chat_type"] != "dialog":
                return None
            if kind == "message_created":
                user = message["sender"]
                text = message["body"].get("text") or ""
                event_id = "message:" + message["body"]["mid"]
                at = message["timestamp"]
            else:
                callback = raw["callback"]
                user = callback["user"]
                text = callback.get("payload") or ""
                callback_id = callback["callback_id"]
                event_id = "callback:" + callback_id
                at = callback["timestamp"]
        else:
            return None
        uid = user["user_id"]
        if user.get("is_bot"):
            return None
        if type(uid) is not int or not 0 < uid < 2**63 or not isinstance(text, str) or len(text) > 4000:
            raise BadEvent("Некорректное событие")
        if type(at) is not int or at < 0 or not isinstance(event_id, str) or len(event_id) > 2048:
            raise BadEvent("Некорректное событие")
        if callback_id is not None and (not isinstance(callback_id, str) or not callback_id):
            raise BadEvent("Некорректный callback")
        # MAX timestamps are milliseconds since Unix epoch.
        when = datetime.fromtimestamp(at / 1000, UTC)
        return {
            "key": hashlib.sha256(event_id.encode()).hexdigest(),
            "user_id": uid,
            "text": text,
            "at": when.isoformat(),
            "callback_id": callback_id,
        }
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError, OSError):
        raise BadEvent("Некорректное событие") from None


def ingest(conn, event):
    if event is None:
        return False
    # Lock before sequence allocation ensures queue order matches commit order among webhook requests.
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (INBOX_LOCK,))
    return (
        conn.execute(
            "INSERT INTO inbox(event_key,event) VALUES (%s,%s) ON CONFLICT DO NOTHING RETURNING id",
            (event["key"], Jsonb(event)),
        ).fetchone()
        is not None
    )
