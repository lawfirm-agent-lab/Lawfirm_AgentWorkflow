"""Read-only connection/TLS check. Reads DATABASE_URL only, never .env."""
import os

import psycopg
from psycopg.conninfo import conninfo_to_dict


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
        # Require encryption even if the saved URI omitted sslmode.
        # Preserve certificate verification if explicitly configured.
        sslmode = config.get('sslmode', 'require')
        if sslmode not in ('require', 'verify-ca', 'verify-full'):
            print('FAIL: TLS is required; use sslmode=require or certificate verification.')
            return 1
        # Do not instantiate Store: its constructor creates tables.
        with psycopg.connect(url, sslmode=sslmode, connect_timeout=10,
                             autocommit=True, prepare_threshold=None) as conn:
            if not conn.pgconn.ssl_in_use:
                print('FAIL: The client connection is not using TLS.')
                return 1
            with conn.transaction():
                conn.execute('SET TRANSACTION READ ONLY')
                conn.execute("SET LOCAL statement_timeout = '10s'")
                if conn.execute('SELECT 1').fetchone() != (1,):
                    print('FAIL: Unexpected database probe result.')
                    return 1
        print('PASS: Database connection and read-only query succeeded; client TLS is active.')
        return 0
    except Exception:
        # Never expose driver errors: they can contain connection credentials.
        print('FAIL: Connection check failed. Verify the secret URI, password encoding, '
              'Session pooler endpoint, TLS settings and project availability.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
