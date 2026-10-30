import hmac
import json
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from app.config import settings
from app.db import pool_for
from app.events import BadEvent, ingest, normalize


def create_app(config=None, pool=None):
    config = config or settings()
    config.validate_webhook()
    database = pool or pool_for(config)

    @asynccontextmanager
    async def lifespan(app):
        if pool is None:
            await run_in_threadpool(database.open, wait=True)
        yield
        if pool is None:
            database.close()

    app = FastAPI(
        title="Учет рабочего времени MAX", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )

    @app.get("/healthz")
    def health():
        with database.connection() as conn:
            conn.execute("SELECT 1 FROM schema_migrations LIMIT 1")
        return {"status": "ok"}

    @app.post("/webhook/max")
    async def webhook(request: Request):
        supplied = request.headers.get("X-Max-Bot-Api-Secret", "")
        expected = config.max_webhook_secret.get_secret_value()
        if not hmac.compare_digest(supplied.encode(), expected.encode()):
            raise HTTPException(403, "Доступ запрещен")
        # Also bound body at the application layer; configure reverse proxy similarly.
        body = bytearray()
        async for part in request.stream():
            body.extend(part)
            if len(body) > 65536:
                raise HTTPException(413, "Слишком большое событие")
        try:
            event = normalize(json.loads(body))
        except (ValueError, BadEvent):
            raise HTTPException(400, "Некорректное событие") from None

        def save():
            with database.connection() as conn:
                ingest(conn, event)

        await run_in_threadpool(save)
        # MAX receives success only after a durable commit. No API calls inside webhook.
        return {"success": True}

    return app
