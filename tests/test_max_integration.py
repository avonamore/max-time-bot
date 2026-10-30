import asyncio

import httpx
import pytest

from app.events import BadEvent, ingest, normalize
from app.max_api import APIError, MaxAPI
from app.web import create_app
from app.worker import deliver_one, process_one
from tests.test_service import handle, query, setup_users


def message(uid=2, chat_type="dialog", mid="mid-1", text="/начать"):
    return {
        "update_type": "message_created",
        "timestamp": 1793289600000,
        "message": {
            "sender": {"user_id": uid, "is_bot": False},
            "recipient": {"chat_type": chat_type, "chat_id": 20},
            "timestamp": 1793289600000,
            "body": {"mid": mid, "text": text},
        },
    }


def test_identity_and_private_only():
    raw = message(text="/начать 999")
    assert normalize(raw)["user_id"] == 2
    assert normalize(message(chat_type="chat")) is None
    raw["message"]["sender"]["is_bot"] = True
    assert normalize(raw) is None


def test_missing_callback_context_rejected():
    assert normalize({"update_type": "message_callback", "callback": {}, "message": None}) is None
    with pytest.raises(BadEvent):
        normalize({"update_type": "message_created", "message": {}})


def test_callback_and_start_schemas():
    raw = {
        "update_type": "message_callback",
        "message": message()["message"],
        "callback": {
            "user": {"user_id": 9},
            "callback_id": "abc",
            "payload": "nonce:/начать",
            "timestamp": 1793289600000,
        },
    }
    event = normalize(raw)
    assert event["user_id"] == 9 and event["callback_id"] == "abc"
    start = normalize(
        {"update_type": "bot_started", "timestamp": 1793289600000, "chat_id": 20, "user": {"user_id": 9}}
    )
    assert start["text"] == "/start" and start["user_id"] == 9


def test_webhook_authentication_size_and_durable_dedupe(pool, cfg):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(cfg, pool)), base_url="http://test"
        ) as client:
            assert (await client.post("/webhook/max", json=message())).status_code == 403
            headers = {"X-Max-Bot-Api-Secret": cfg.max_webhook_secret.get_secret_value()}
            assert (await client.post("/webhook/max", headers=headers, json=message())).status_code == 200
            assert (await client.post("/webhook/max", headers=headers, json=message())).status_code == 200
            assert len(query(pool, "SELECT * FROM inbox")) == 1
            assert (
                await client.post("/webhook/max", headers=headers, content=b"a" * 65537)
            ).status_code == 413
            assert (await client.post("/webhook/max", headers=headers, content=b"broken")).status_code == 400
            assert (await client.get("/healthz")).status_code == 200

    asyncio.run(run())


def test_api_contract_and_file_authorization_boundary(cfg):
    calls = []

    def transport(request):
        calls.append(request)
        if request.url.path == "/uploads":
            return httpx.Response(200, json={"url": "https://upload.example/upload?signature=sensitive"})
        if request.url.host == "upload.example":
            return httpx.Response(200, json={"token": "file-token"})
        return httpx.Response(200, json={"success": True})

    async def run():
        api = MaxAPI(cfg, httpx.MockTransport(transport))
        await api.send_message(2, {"text": "Привет"})
        await api.answer({"callback_id": "abc", "notification": "Готово"})
        attachment = await api.upload_file(b"csv")
        await api.send_message(1, {"attachments": [attachment]})
        await api.close()

    asyncio.run(run())
    assert calls[0].headers["Authorization"] == "test-token"
    assert calls[0].url.params["user_id"] == "2"
    assert "access_token" not in str(calls[0].url)
    assert calls[1].url.path == "/answers" and calls[1].url.params["callback_id"] == "abc"
    assert calls[2].url.params["type"] == "file"
    assert "authorization" not in calls[3].headers
    assert b'name="data"' in calls[3].content
    assert b'"type":"file"' in calls[4].content


def test_upload_host_fail_closed(cfg):
    async def run():
        api = MaxAPI(
            cfg, httpx.MockTransport(lambda r: httpx.Response(200, json={"url": "http://evil.local/file"}))
        )
        with pytest.raises(APIError) as exc:
            await api.upload_file(b"private")
        assert exc.value.status == 460
        await api.close()

    asyncio.run(run())


def test_rate_limit_preserves_retry_after(cfg):
    async def run():
        api = MaxAPI(cfg, httpx.MockTransport(lambda r: httpx.Response(429, headers={"Retry-After": "37"})))
        with pytest.raises(APIError) as exc:
            await api.send_message(1, {"text": "test"})
        assert exc.value.status == 429 and exc.value.retry_after == 37
        await api.close()

    asyncio.run(run())


class FakeAPI:
    def __init__(self, error=None):
        self.error, self.messages = error, []

    async def send_message(self, uid, payload):
        if self.error:
            raise self.error
        self.messages.append((uid, payload))

    async def answer(self, payload):
        pass

    async def upload_file(self, data):
        self.data = data
        return {"type": "file", "payload": {"token": "file-token"}}


def clear_outbox(pool):
    with pool.connection() as c:
        c.execute("DELETE FROM outbox")


def test_worker_retry_and_durable_response(pool, cfg, now):
    setup_users(pool, cfg, now)
    clear_outbox(pool)
    event = normalize(message(mid="fresh", text="/статус"))
    with pool.connection() as c:
        ingest(c, event)
    assert process_one(pool, cfg)
    assert asyncio.run(deliver_one(pool, cfg, FakeAPI(APIError(429, 37))))
    r = query(pool, "SELECT * FROM outbox")[0]
    assert r["attempts"] == 1 and r["sent_at"] is None and r["failed_at"] is None
    assert query(pool, "SELECT processed_at FROM inbox")[0]["processed_at"] is not None
    with pool.connection() as c:
        c.execute("UPDATE outbox SET available_at=now()")
    api = FakeAPI()
    assert asyncio.run(deliver_one(pool, cfg, api))
    assert len(api.messages) == 1 and "_access" not in api.messages[0][1]
    assert not asyncio.run(deliver_one(pool, cfg, api))


def test_revoke_access_before_csv_dispatch(pool, cfg, now):
    setup_users(pool, cfg, now)
    clear_outbox(pool)
    handle(pool, cfg, now, 1, "/csv месяц")
    with pool.connection() as c:
        c.execute("UPDATE employees SET status='blocked' WHERE max_id=1")
    api = FakeAPI()
    while asyncio.run(deliver_one(pool, cfg, api)):
        pass
    assert not api.messages
    assert all(r["last_error"] == "access_revoked" for r in query(pool, "SELECT * FROM outbox"))


def test_csv_delivery_is_private_and_role_checked(pool, cfg, now):
    setup_users(pool, cfg, now)
    clear_outbox(pool)
    handle(pool, cfg, now, 1, "/csv месяц 2")
    api = FakeAPI()
    assert asyncio.run(deliver_one(pool, cfg, api))
    assert api.data.startswith(b"\xef\xbb\xbf")
    assert api.messages[0][0] == 1
    assert api.messages[0][1]["attachments"][0]["type"] == "file"
    clear_outbox(pool)
    handle(pool, cfg, now, 2, "/csv месяц 3")
    assert not query(pool, "SELECT * FROM outbox WHERE kind='csv'")


def test_inbox_transaction_rollback_after_failure(pool, cfg, now, monkeypatch):
    setup_users(pool, cfg, now)
    clear_outbox(pool)
    with pool.connection() as c:
        ingest(c, normalize(message(mid="rollback")))
    from app.service import Service

    original = Service.handle

    def fail(self, event):
        original(self, event)
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(Service, "handle", fail)
    with pytest.raises(RuntimeError):
        process_one(pool, cfg)
    assert not query(pool, "SELECT * FROM shifts")
    assert not query(pool, "SELECT * FROM outbox")
    assert query(pool, "SELECT processed_at FROM inbox")[0]["processed_at"] is None
    monkeypatch.setattr(Service, "handle", original)
    assert process_one(pool, cfg)
    assert len(query(pool, "SELECT * FROM shifts")) == 1


@pytest.mark.parametrize(
    "raw",
    [
        {"update_type": "message_created", "message": {"recipient": 5}},
        {"update_type": "message_created", "message": {"recipient": {"chat_type": "dialog"}, "sender": 5}},
    ],
)
def test_malformed_nested_event_rejected(raw):
    with pytest.raises(BadEvent):
        normalize(raw)


def test_csv_uses_consistent_database_snapshot(pool, cfg, now, monkeypatch):
    setup_users(pool, cfg, now)
    from datetime import timedelta

    handle(pool, cfg, now, 2, "/начать", now - timedelta(hours=2))
    handle(pool, cfg, now, 2, "/закончить", now)
    clear_outbox(pool)
    handle(pool, cfg, now, 1, "/csv месяц 2")
    from app.service import Service

    original = Service.report

    def concurrent_change(self, uid, start, end):
        # Delivery has already selected its outbox row (and thus established its snapshot).
        with pool.connection() as writer:
            writer.execute("UPDATE shifts SET ended_at=ended_at-interval '1 hour' WHERE employee_id=2")
        rows = original(self, uid, start, end)
        assert rows[0]["net"] == 2 * 3600
        return rows

    monkeypatch.setattr(Service, "report", concurrent_change)
    api = FakeAPI()
    assert asyncio.run(deliver_one(pool, cfg, api))
    assert "02:00:00" in api.data.decode("utf-8-sig")
