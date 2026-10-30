import hashlib
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.config import Settings

# All business mutations serialize here, across processes/replicas. No network I/O under this lock.
BUSINESS_LOCK = 72641001
INBOX_LOCK = 72641002


def pool_for(config: Settings):
    return ConnectionPool(
        config.database_url.get_secret_value(),
        min_size=1,
        max_size=8,
        kwargs={"row_factory": dict_row},
        open=False,
    )


def migrate(url: str):
    with psycopg.connect(url, row_factory=dict_row) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (BUSINESS_LOCK,))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, checksum TEXT NOT NULL)"
        )
        for path in sorted((Path(__file__).parent.parent / "migrations").glob("*.sql")):
            data = path.read_text()
            checksum = hashlib.sha256(data.encode()).hexdigest()
            row = conn.execute(
                "SELECT checksum FROM schema_migrations WHERE name=%s", (path.name,)
            ).fetchone()
            if row:
                if row["checksum"] != checksum:
                    raise RuntimeError(f"Изменена примененная миграция: {path.name}")
                continue
            conn.execute(data)
            conn.execute("INSERT INTO schema_migrations VALUES (%s,%s)", (path.name, checksum))
