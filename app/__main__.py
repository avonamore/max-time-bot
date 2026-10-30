import argparse
import asyncio
import logging
from urllib.parse import urlsplit

import uvicorn

from app.config import settings
from app.db import migrate, pool_for
from app.max_api import MaxAPI
from app.worker import run


async def command(name, cfg):
    if not cfg.max_bot_token.get_secret_value():
        raise SystemExit("Укажите MAX_BOT_TOKEN в .env")
    api = MaxAPI(cfg)
    try:
        if name == "subscribe":
            cfg.validate_webhook()
            if urlsplit(cfg.max_webhook_url).scheme != "https":
                raise SystemExit("MAX_WEBHOOK_URL должен использовать HTTPS и доверенный сертификат")
            await api.request(
                "POST",
                "/subscriptions",
                json={
                    "url": cfg.max_webhook_url,
                    "secret": cfg.max_webhook_secret.get_secret_value(),
                    "update_types": ["bot_started", "message_created", "message_callback"],
                },
            )
            print("Подписка MAX создана.")
        elif name == "check-max":
            await api.request("GET", "/me")
            print("Токен MAX работает.")
        elif name == "upload-host":
            endpoint = await api.request("POST", "/uploads", params={"type": "file"})
            # Print only the hostname, never the signed upload URL.
            print("Хост загрузки MAX:", urlsplit(endpoint["url"]).hostname)
        else:
            with pool_for(cfg) as pool:
                await run(pool, cfg, api)
    finally:
        await api.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=["migrate", "bootstrap-admin", "web", "worker", "subscribe", "check-max", "upload-host"],
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
    cfg = settings()
    if args.command == "migrate":
        migrate(cfg.database_url.get_secret_value())
        print("Миграции применены.")
    elif args.command == "bootstrap-admin":
        if cfg.initial_admin_max_id <= 0:
            raise SystemExit("Укажите INITIAL_ADMIN_MAX_ID > 0")
        from app.db import BUSINESS_LOCK

        with pool_for(cfg) as pool, pool.connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (BUSINESS_LOCK,))
            old = conn.execute(
                "SELECT role,status FROM employees WHERE max_id=%s", (cfg.initial_admin_max_id,)
            ).fetchone()
            conn.execute(
                """INSERT INTO employees(max_id,full_name,role,status) VALUES (%s,'Администратор','admin','approved')
                ON CONFLICT(max_id) DO UPDATE SET role='admin',status='approved',menu_token='',
                full_name=CASE WHEN employees.full_name='' THEN 'Администратор' ELSE employees.full_name END""",
                (cfg.initial_admin_max_id,),
            )
            from app.service import Service

            Service(conn, cfg).audit(
                cfg.initial_admin_max_id,
                "bootstrap_admin",
                cfg.initial_admin_max_id,
                "Доверенная команда администратора сервера",
                dict(old) if old else {},
                {"role": "admin", "status": "approved"},
            )
        print("Первый администратор назначен.")
    elif args.command == "web":
        uvicorn.run("app.web:create_app", factory=True, host="0.0.0.0", port=8080, access_log=False)
    else:
        asyncio.run(command(args.command, cfg))


if __name__ == "__main__":
    main()
