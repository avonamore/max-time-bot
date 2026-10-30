import os
from datetime import UTC, datetime

import psycopg
import pytest
from pydantic import SecretStr

from app.config import Settings
from app.db import migrate, pool_for

TEST_URL = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture
def cfg():
    return Settings(
        _env_file=None,
        database_url=SecretStr(TEST_URL or "postgresql://unused"),
        initial_admin_max_id=1,
        max_bot_token=SecretStr("test-token"),
        max_webhook_secret=SecretStr("s" * 48),
        max_upload_allowed_hosts="upload.example",
    )


@pytest.fixture
def pool(cfg):
    if not TEST_URL:
        pytest.skip("Set TEST_DATABASE_URL to a dedicated disposable PostgreSQL database")
    migrate(TEST_URL)
    with psycopg.connect(TEST_URL) as conn:
        conn.execute(
            "TRUNCATE employees,shifts,breaks,corrections,audit_log,inbox,outbox RESTART IDENTITY CASCADE"
        )
    with pool_for(cfg) as p:
        yield p


@pytest.fixture
def now():
    return datetime(2026, 10, 29, 16, 0, tzinfo=UTC)
