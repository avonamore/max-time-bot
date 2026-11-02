import logging
import time
from datetime import timedelta
from psycopg.types.json import Jsonb
from .db import connect
from .events import parse
from .service import Service, utcnow
from .timecalc import RuleError
from .max_api import APIError, MaxAPI

log = logging.getLogger(__name__)


def process_one(cfg):
    with connect(cfg.database_url) as db:
        # One global inbox consumer preserves receipt order across worker processes.
        if not db.execute('SELECT pg_try_advisory_xact_lock(5100923) AS ok').fetchone()['ok']:
            return False
        row = db.execute('SELECT * FROM inbox WHERE processed_at IS NULL ORDER BY id LIMIT 1 FOR UPDATE').fetchone()
        if not row or row['next_at'] > utcnow():
            return False
        event = parse(row['payload'])
        service = Service(db, cfg, event.at)
        try:
            with db.transaction():
                user = service.employee(event.uid)
                text, csv_data = service.handle(event.uid, event.text, bool(event.callback_id))
                sensitive = user['status'] == 'approved'
                service.enqueue(event.uid, text, csv_data, sensitive=sensitive, menu=sensitive, callback_id=event.callback_id)
        except (RuleError, ValueError, TypeError, OverflowError) as error:
            # Roll back every partial business mutation before writing a safe reply.
            text = str(error) if isinstance(error, RuleError) else 'Некорректные параметры команды. Откройте /help.'
            user = service.employee(event.uid)
            allowed = user['status'] == 'approved'
            if allowed:
                text += f'\nВремя события: {service.stamp(event.at)}\n{service.status(event.uid)}'
            service.enqueue(event.uid, text, menu=allowed, sensitive=allowed, callback_id=event.callback_id)
        db.execute('UPDATE inbox SET processed_at=now() WHERE id=%s', (row['id'],))
    return True


def deliver_one(cfg, api):
    with connect(cfg.database_url) as db:
        if not db.execute('SELECT pg_try_advisory_xact_lock(5100924) AS ok').fetchone()['ok']:
            return False
        row = db.execute('SELECT o.* FROM outbox o WHERE o.sent_at IS NULL AND NOT o.failed AND o.next_at<=now() AND NOT EXISTS (SELECT 1 FROM outbox earlier WHERE earlier.recipient=o.recipient AND earlier.id<o.id AND earlier.sent_at IS NULL AND NOT earlier.failed) ORDER BY o.id LIMIT 1 FOR UPDATE').fetchone()
        if not row or row['next_at'] > utcnow():
            return False
        # Hold the same lock as role mutations while checking and sending private data.
        service = Service(db, cfg)
        user = service.employee(row['recipient'])
        if row['sensitive'] and user['status'] != 'approved':
            db.execute('UPDATE outbox SET failed=true WHERE id=%s', (row['id'],))
            return True
        try:
            body = row['body']
            if row['csv_data'] is not None:
                token = api.upload_csv(bytes(row['csv_data']))
                body.setdefault('attachments', []).append({'type': 'file', 'payload': {'token': token}})
                # Persist token: retries for attachment.not.ready must reuse the same file.
                db.execute('UPDATE outbox SET body=%s,csv_data=NULL WHERE id=%s', (Jsonb(body), row['id']))
            api.send(row['recipient'], body)
            if row['callback_id']:
                try:
                    api.answer(row['callback_id'])
                except APIError:
                    log.warning('callback_ack_failed outbox_id=%s', row['id'])
            db.execute('UPDATE outbox SET sent_at=now() WHERE id=%s', (row['id'],))
        except APIError as error:
            attempts = row['attempts'] + 1
            dead = not error.retryable or attempts >= 12
            db.execute('UPDATE outbox SET attempts=%s,next_at=%s,failed=%s WHERE id=%s',
                       (attempts, utcnow() + timedelta(seconds=max(error.retry_after, min(3600, 2 ** attempts))), dead, row['id']))
            log.warning('outbox_id=%s status=%s attempt=%s failed=%s', row['id'], error.status, attempts, dead)
    return True


def run(cfg):
    api = MaxAPI(cfg)
    log.info('worker_started')
    try:
        while True:
            try:
                busy = process_one(cfg)
                busy = deliver_one(cfg, api) or busy
                if not busy:
                    time.sleep(0.5)
            except Exception as error:
                # Intentionally omit exception strings/tracebacks: can contain SQL/PII/URLs.
                log.error('worker_iteration_failed kind=%s', type(error).__name__)
                time.sleep(2)
    finally:
        api.close()
