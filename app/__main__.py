import argparse
import logging
from .config import settings
from .db import migrate, connect
from .max_api import MaxAPI


def main():
    parser = argparse.ArgumentParser(description='Бот учета времени MAX')
    parser.add_argument('command', choices=['migrate', 'web', 'worker', 'subscribe', 'check', 'queue', 'retry-failed'])
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)
    cfg = settings()
    if args.command == 'migrate':
        migrate(cfg.database_url)
    elif args.command == 'web':
        import uvicorn
        from .web import create_app
        uvicorn.run(create_app(cfg), host='0.0.0.0', port=8000, access_log=False)
    elif args.command == 'worker':
        from .worker import run
        run(cfg)
    elif args.command in ('queue', 'retry-failed'):
        with connect(cfg.database_url) as db:
            if args.command == 'retry-failed':
                db.execute('UPDATE outbox SET failed=false,attempts=0,next_at=now() WHERE failed=true')
            for table, predicate in [('inbox', 'processed_at IS NULL'), ('outbox', 'sent_at IS NULL')]:
                # Fixed identifiers, no user input.
                print(table, db.execute(f'SELECT count(*) n FROM {table} WHERE {predicate}').fetchone()['n'])
    else:
        if not cfg.max_bot_token:
            parser.error('Задайте MAX_BOT_TOKEN')
        client = MaxAPI(cfg)
        try:
            if args.command == 'subscribe':
                client.subscribe(cfg.public_base_url.rstrip('/') + '/webhook')
                print('Подписка зарегистрирована.')
            else:
                print('Соединение MAX проверено; bot_id:', client.request('GET', '/me')['user_id'])
        finally:
            client.close()


if __name__ == '__main__':
    main()
