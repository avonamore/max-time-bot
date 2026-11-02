import os
from datetime import datetime, timezone
from types import SimpleNamespace
import pytest
from app.db import connect, migrate
from app.service import Service


@pytest.fixture(scope='session')
def cfg():
    url = os.environ.get('TEST_DATABASE_URL')
    if not url:
        pytest.fail('Укажите TEST_DATABASE_URL: тесты требуют отдельную PostgreSQL (таблицы очищаются).')
    config = SimpleNamespace(database_url=url, admin_max_id=1, org_timezone='Asia/Omsk',
                             max_webhook_secret='test_secret_32_characters_long_ok',
                             max_api_base='https://platform-api2.max.ru', max_bot_token='fake-test-token',
                             max_ca_bundle=None, max_upload_hosts='fu.oneme.ru,omu.okcdn.ru,omub.okcdn.ru')
    migrate(url)
    return config


@pytest.fixture(autouse=True)
def clean(cfg):
    with connect(cfg.database_url) as db:
        db.execute('TRUNCATE employees,shifts,breaks,corrections,audit,inbox,outbox RESTART IDENTITY CASCADE')


@pytest.fixture
def now():
    return datetime(2026, 11, 2, 12, tzinfo=timezone.utc)


def command(cfg, uid, text, now, button=False):
    with connect(cfg.database_url) as db:
        return Service(db, cfg, now).handle(uid, text, button)


@pytest.fixture
def approved(cfg, now):
    command(cfg, 1, '/menu', now)
    command(cfg, 2, 'Иванов Иван Иванович', now)
    command(cfg, 1, '/approve 2', now)
    return 2
