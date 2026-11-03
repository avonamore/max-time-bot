import json
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
import httpx
import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
from app.db import connect
from app.web import create_app
from app.worker import process_one, deliver_one
from app.max_api import MaxAPI, APIError
from app.events import parse
from app.service import Service
from conftest import command


def update(now, text='Начать работу', mid='m1', uid=2, kind='message_created', chat_type='dialog'):
    result = {'update_type': kind, 'timestamp': int(now.timestamp()*1000),
              'message': {'sender': {'user_id': uid, 'is_bot': False},
                          'recipient': {'chat_type': chat_type, 'user_id': 999},
                          'body': {'mid': mid, 'text': text}}}
    if kind == 'message_callback':
        result['callback'] = {'user': {'user_id': uid}, 'callback_id': mid, 'payload': text}
        result['message']['sender']['user_id'] = 999  # Bot, not acting employee!
    return result


def post(cfg, body, secret=True):
    with TestClient(create_app(cfg)) as client:
        headers = {'X-Max-Bot-Api-Secret': cfg.max_webhook_secret} if secret else {}
        return client.post('/webhook', json=body, headers=headers)


def test_webhook_auth_group_and_dedup(cfg, now, approved):
    assert post(cfg, update(now), False).status_code == 403
    assert post(cfg, update(now, chat_type='chat')).status_code == 200
    with connect(cfg.database_url) as db:
        assert db.execute('SELECT count(*) n FROM inbox').fetchone()['n'] == 0
    assert post(cfg, update(now)).status_code == 200
    assert post(cfg, update(now)).status_code == 200
    # No in-memory background task: webhook is acknowledged after durable insert.
    with connect(cfg.database_url) as db:
        assert db.execute('SELECT count(*) n FROM shifts').fetchone()['n'] == 0
        assert db.execute('SELECT count(*) n FROM inbox').fetchone()['n'] == 1
    assert process_one(cfg)
    assert not process_one(cfg)
    with connect(cfg.database_url) as db:
        assert db.execute('SELECT count(*) n FROM shifts').fetchone()['n'] == 1


def test_callback_actor_and_stale_repeated_events(cfg, now, approved):
    callback = update(now, text='start|0.0', kind='message_callback')
    assert parse(callback).uid == 2
    assert post(cfg, callback).status_code == 200
    assert process_one(cfg)
    assert post(cfg, callback).status_code == 200
    assert not process_one(cfg)
    assert post(cfg, update(now+timedelta(seconds=1), text='start|0.0', mid='m2', kind='message_callback')).status_code == 200
    assert process_one(cfg)
    with connect(cfg.database_url) as db:
        assert db.execute('SELECT count(*) n FROM shifts').fetchone()['n'] == 1
        assert 'устарела' in db.execute('SELECT body FROM outbox ORDER BY id DESC LIMIT 1').fetchone()['body']['text']


def test_null_callback_and_malformed(cfg, now):
    data = update(now, kind='message_callback')
    data['message'] = None
    assert post(cfg, data).status_code == 200
    assert post(cfg, {'update_type': 'message_created'}).status_code == 400
    assert post(cfg, []).status_code == 400
    with TestClient(create_app(cfg)) as client:
        assert client.get('/health').json()['status'] == 'ok'
        response = client.post('/webhook', content=b'x'*262145, headers={'X-Max-Bot-Api-Secret': cfg.max_webhook_secret})
        assert response.status_code == 413


def test_concurrent_delivery_same_event(cfg, now, approved):
    payload = update(now)
    with ThreadPoolExecutor(max_workers=6) as pool:
        codes = list(pool.map(lambda _: post(cfg, payload).status_code, range(12)))
    assert set(codes) == {200}
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda _: process_one(cfg), range(12)))
    with connect(cfg.database_url) as db:
        assert db.execute('SELECT count(*) n FROM shifts').fetchone()['n'] == 1
        assert db.execute('SELECT count(*) n FROM inbox WHERE processed_at IS NOT NULL').fetchone()['n'] == 1


def test_worker_handles_malformed_command_atomically(cfg, now, approved):
    post(cfg, update(now, text='/correct invalid'))
    assert process_one(cfg)
    with connect(cfg.database_url) as db:
        assert db.execute('SELECT count(*) n FROM corrections').fetchone()['n'] == 0
        assert db.execute('SELECT count(*) n FROM inbox WHERE processed_at IS NOT NULL').fetchone()['n'] == 1


def test_http_contract_upload_and_rate_retry(cfg):
    seen, delays = [], []
    calls = {'send': 0}
    def handler(request):
        seen.append(request)
        if request.url.path == '/uploads':
            assert request.url.params['type'] == 'file'
            return httpx.Response(200, json={'url': 'https://fu.oneme.ru/api/upload.do?sig=opaque'})
        if request.url.host == 'fu.oneme.ru':
            assert 'Authorization' not in request.headers
            assert b'name="data"' in request.content
            assert b'filename="report.csv"' in request.content
            return httpx.Response(200, json={'fileId': 11, 'token': 'uploaded-token'})
        if request.url.path == '/messages':
            calls['send'] += 1
            if calls['send'] == 1:
                return httpx.Response(429, headers={'Retry-After':'2'}, json={})
            if calls['send'] == 2:
                return httpx.Response(400, json={'code':'attachment.not.ready'})
            assert request.url.params['user_id'] == '2'
            assert json.loads(request.content)['attachments'][0]['payload']['token'] == 'uploaded-token'
            return httpx.Response(200, json={'message': {}})
        return httpx.Response(200, json={'success': True})
    api = MaxAPI(cfg, transport=httpx.MockTransport(handler), sleep=delays.append)
    token = api.upload_csv('ФИО'.encode('utf-8-sig'))
    api.send(2, {'attachments':[{'type':'file','payload':{'token':token}}]})
    api.subscribe('https://bot.example.ru/webhook')
    api.answer('cb1')
    api.close()
    assert calls['send'] == 3 and 2 in delays
    for req in seen:
        if req.url.host == 'platform-api2.max.ru':
            assert req.headers['Authorization'] == 'fake-test-token'
            assert 'access_token' not in req.url.params
    subscription = next(json.loads(x.content) for x in seen if x.url.path == '/subscriptions')
    assert subscription['secret'] == cfg.max_webhook_secret
    assert 'message_callback' in subscription['update_types']


def test_upload_rejects_untrusted_url(cfg):
    api = MaxAPI(cfg, transport=httpx.MockTransport(lambda _: httpx.Response(200, json={'url':'http://127.0.0.1/secrets'})), sleep=lambda _: None)
    with pytest.raises(APIError):
        api.upload_csv(b'csv')
    api.close()


class FakeAPI:
    def __init__(self, fail=False):
        self.messages, self.uploads, self.fail = [], [], fail
    def upload_csv(self, data):
        self.uploads.append(data)
        return 'token'
    def send(self, uid, body):
        if self.fail:
            raise APIError(503, True)
        self.messages.append((uid, body))
    def answer(self, callback_id):
        pass


def test_outbox_persists_token_and_retry(cfg, now, approved):
    with connect(cfg.database_url) as db:
        db.execute('DELETE FROM outbox')
        Service(db, cfg).enqueue(2, 'Отчет', b'csv', sensitive=True)
    api = FakeAPI(fail=True)
    assert deliver_one(cfg, api)
    with connect(cfg.database_url) as db:
        row = db.execute('SELECT * FROM outbox').fetchone()
        assert row['sent_at'] is None and row['attempts'] == 1
        assert row['csv_data'] is None
        assert row['body']['attachments'][0]['payload']['token'] == 'token'
        db.execute('UPDATE outbox SET next_at=now()')
    api.fail = False
    assert deliver_one(cfg, api)
    assert len(api.uploads) == 1 and len(api.messages) == 1
    assert not deliver_one(cfg, api)


def test_blocked_recipient_never_receives_queued_report(cfg, now, approved):
    with connect(cfg.database_url) as db:
        db.execute('DELETE FROM outbox')
        Service(db, cfg).enqueue(2, 'Персональные часы', b'csv', sensitive=True)
    command(cfg, 1, '/block 2', now)
    api = FakeAPI()
    deliver_one(cfg, api)
    assert api.messages == [] and api.uploads == []


def test_one_recipients_retry_does_not_block_others(cfg, now, approved):
    with connect(cfg.database_url) as db:
        db.execute('DELETE FROM outbox')
        service = Service(db, cfg)
        service.enqueue(2, 'Отчет 1', sensitive=True)
        db.execute("UPDATE outbox SET next_at=now()+interval '1 hour'")
        service.enqueue(2, 'Отчет 2', sensitive=True)
        service.enqueue(1, 'Для руководителя', sensitive=True)
    api = FakeAPI()
    assert deliver_one(cfg, api)
    assert api.messages[0][0] == 1
    assert not deliver_one(cfg, api)


def test_oversized_id_and_nontext_payload_rejected(cfg, now):
    assert post(cfg, update(now, uid=2**70)).status_code == 400
    item = update(now, kind='message_callback')
    item['callback']['payload'] = ['bad']
    assert post(cfg, item).status_code == 400


def test_callback_reused_id_different_press_not_lost(cfg, now, approved):
    post(cfg, update(now, text='status|0.0', mid='same-button', kind='message_callback'))
    post(cfg, update(now+timedelta(seconds=1), text='status|0.0', mid='same-button', kind='message_callback'))
    assert process_one(cfg)
    assert process_one(cfg)


def test_registration_webhook_redelivery_notifies_once(cfg, now):
    command(cfg, 1, '/menu', now)
    data = update(now, text='Новый Тестовый Сотрудник', mid='registration')
    assert post(cfg, data).status_code == 200
    assert process_one(cfg)
    assert post(cfg, data).status_code == 200
    assert not process_one(cfg)
    with connect(cfg.database_url) as db:
        rows = db.execute('SELECT * FROM outbox WHERE recipient=1').fetchall()
        assert len(rows) == 1
        assert '/approve 2' in rows[0]['body']['text']
