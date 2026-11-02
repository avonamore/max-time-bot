from pathlib import Path
import psycopg
from psycopg.rows import dict_row


def connect(url):
    return psycopg.connect(url, row_factory=dict_row, options='-c timezone=UTC')


def migrate(url):
    with connect(url) as db:
        db.execute('SELECT pg_advisory_xact_lock(5100921)')
        db.execute('CREATE TABLE IF NOT EXISTS schema_migrations (name text PRIMARY KEY, applied_at timestamptz DEFAULT now())')
        for path in sorted((Path(__file__).parent.parent / 'migrations').glob('*.sql')):
            if not db.execute('SELECT 1 FROM schema_migrations WHERE name=%s', (path.name,)).fetchone():
                db.execute(path.read_text())
                db.execute('INSERT INTO schema_migrations(name) VALUES(%s)', (path.name,))
