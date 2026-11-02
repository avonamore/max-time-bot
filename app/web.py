import hmac
import json
from fastapi import FastAPI, Request, HTTPException
from psycopg.types.json import Jsonb
from .config import settings
from .db import connect
from .events import parse


def create_app(cfg=None):
    cfg = cfg or settings()
    api = FastAPI(title='Учет времени MAX', docs_url=None, redoc_url=None, openapi_url=None)

    @api.get('/health')
    def health():
        with connect(cfg.database_url) as db:
            db.execute('SELECT 1')
        return {'status': 'ok'}

    @api.post('/webhook')
    async def webhook(request: Request):
        value = request.headers.get('X-Max-Bot-Api-Secret', '')
        if not hmac.compare_digest(value.encode(), cfg.max_webhook_secret.encode()):
            raise HTTPException(403, 'Неверный секрет')
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 262144:
                raise HTTPException(413, 'Слишком большое событие')
        try:
            update = json.loads(body)
            if not isinstance(update, dict):
                raise ValueError()
            event = parse(update)
            if event and (not isinstance(event.text, str) or not 0 < event.uid < 2**63 or '\x00' in event.text or '\x00' in event.key or len(event.key) > 1000 or len(event.text) > 10000):
                raise ValueError()
        except (ValueError, KeyError, TypeError, OverflowError, OSError):
            raise HTTPException(400, 'Некорректное событие') from None
        if event:
            with connect(cfg.database_url) as db:
                db.execute('INSERT INTO inbox(event_key,payload) VALUES(%s,%s) ON CONFLICT DO NOTHING', (event.key, Jsonb(update)))
        return {'ok': True}

    return api
