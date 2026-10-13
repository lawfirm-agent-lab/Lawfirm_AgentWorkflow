"""Create missing PostgreSQL application tables; never load .env or seed data."""
import os

from psycopg.conninfo import conninfo_to_dict
from src.store.db import Store


def main():
    url = os.environ.get('DATABASE_URL', '').strip()
    if not url:
        print('FAIL: DATABASE_URL secret is missing or empty.')
        return 1
    if not url.startswith(('postgresql://', 'postgres://')):
        print('FAIL: DATABASE_URL must be a PostgreSQL URI.')
        return 1
    try:
        config = conninfo_to_dict(url)
        sslmode = config.get('sslmode', 'require')
        if sslmode not in ('require', 'verify-ca', 'verify-full'):
            print('FAIL: TLS is required for cloud initialization.')
            return 1
        # Add explicit connection settings without printing credentials.
        from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query['sslmode'] = sslmode
        query['options'] = '-c statement_timeout=30000 -c lock_timeout=10000 -c search_path=public'
        secured_url = urlunsplit(parts._replace(query=urlencode(query)))
        store = Store(database_url=secured_url)
        try:
            # Store creates missing tables transactionally. No rows are inserted.
            print('PASS: Application tables initialized: dockets, entries, runs, changes, email_outbox.')
            print('Existing tables and rows retained; no case baseline or email was created.')
        finally:
            store.close()
        return 0
    except Exception:
        print('FAIL: Database initialization failed. Check connection settings, TLS, '
              'database availability and CREATE permission; credentials are not logged.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
