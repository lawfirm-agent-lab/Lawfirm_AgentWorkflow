"""Write/commit/reconnect/read probe using an isolated table, then remove it."""
import os
import uuid

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict


def verify_storage(connect):
    """connect must return a context-managed connection with autocommit enabled.

    Production entry point enforces TLS. The callback also permits local tests.
    Never accesses application tables. No CASCADE cleanup is used.
    """
    table_name = '_storage_probe_' + uuid.uuid4().hex
    table = sql.Identifier('public', table_name)
    value = 'synthetic-storage-check-' + uuid.uuid4().hex
    attempted = False
    verified = False
    cleaned = True
    stage = 'connect for write'
    try:
        with connect() as writer:
            with writer.transaction():
                stage = 'create isolated probe table and write'
                attempted = True
                writer.execute(sql.SQL('CREATE TABLE {} (id INTEGER PRIMARY KEY, value TEXT NOT NULL)').format(table))
                writer.execute(sql.SQL('INSERT INTO {} (id, value) VALUES (%s, %s)').format(table), (1, value))
                stage = 'commit'
            # Exit transaction before opening another connection: verify committed data.
        stage = 'reconnect and read'
        with connect() as reader:
            with reader.transaction():
                reader.execute('SET TRANSACTION READ ONLY')
                row = reader.execute(sql.SQL('SELECT id, value FROM {}').format(table)).fetchall()
                if row != [(1, value)]:
                    raise ValueError('Probe mismatch')
        verified = True
    except Exception:
        print(f'FAIL: Storage verification failed during {stage}; connection details are hidden.')
    finally:
        if attempted:
            try:
                with connect() as cleaner:
                    cleaner.execute(sql.SQL('DROP TABLE IF EXISTS {}').format(table))
                    exists = cleaner.execute('SELECT to_regclass(%s)', ('public.' + table_name,)).fetchone()
                    if exists != (None,):
                        raise ValueError('Probe cleanup not confirmed')
            except Exception:
                cleaned = False
                print(f'FAIL: Cleanup could not be confirmed. Inspect only public.{table_name}.')
    if verified and cleaned:
        print('PASS: Synthetic data committed, read through a new connection, and probe table removed.')
        return 0
    return 1


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
        mode = config.get('sslmode', 'require')
        if mode not in ('require', 'verify-ca', 'verify-full'):
            print('FAIL: TLS is required for cloud storage verification.')
            return 1
    except Exception:
        print('FAIL: Invalid database connection configuration; details are hidden.')
        return 1

    def connect():
        conn = psycopg.connect(url, sslmode=mode, connect_timeout=10, autocommit=True,
                               prepare_threshold=None,
                               options='-c statement_timeout=10000 -c lock_timeout=5000')
        if not conn.pgconn.ssl_in_use:
            conn.close()
            raise RuntimeError('TLS required')
        return conn

    return verify_storage(connect)


if __name__ == '__main__':
    raise SystemExit(main())
