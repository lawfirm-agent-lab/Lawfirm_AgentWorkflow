"""Disposable local PostgreSQL integration checks. Never loads .env."""
import sys
import os
from pathlib import Path
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.store.db import Store, StoreError
from src.notify.email import deliver

# Only a localhost port is configurable: never use DATABASE_URL or load .env.
# This suite resets tables in the disposable lawfirm_test database before each test.
port = os.environ.get("TEST_POSTGRES_PORT", "")
if not port.isascii() or not port.isdecimal() or not 1 <= int(port) <= 65535:
    raise SystemExit("Set TEST_POSTGRES_PORT to the disposable local PostgreSQL port")
URL = f"postgresql://postgres:local-test-only@127.0.0.1:{int(port)}/lawfirm_test"

def entry(i, docket=101, description='ORDER test'):
    return {'id': i, 'docket': f'https://www.courtlistener.com/api/rest/v4/dockets/{docket}/',
            'description': description, 'date_filed': '2026-10-12', 'recap_documents': []}

class Integration(unittest.TestCase):
    def setUp(self):
        self.store = Store(database_url=URL)
        self.addCleanup(self.store.close)
        self.store.execute('TRUNCATE email_outbox, changes, entries, runs, dockets RESTART IDENTITY CASCADE')

    def apply(self, rows, docket=101):
        return self.store.apply(self.store.start(docket), rows)

    def test_baseline_changes_and_missing(self):
        self.assertTrue(self.apply([entry(1)])['baseline'])
        self.assertEqual(self.store.list_changes(101), [])
        result = self.apply([entry(1, description='Updated'), entry(2)])
        self.assertEqual((result['added'], result['modified']), (1, 1))
        self.assertEqual(len(self.store.list_changes(101)), 2)
        self.assertEqual(self.apply([entry(1, description='Updated'), entry(2)])['modified'], 0)
        self.assertEqual(self.apply([])['missing'], 2)
        self.assertEqual(self.apply([entry(1, description='Updated'), entry(2)])['added'], 0)

    def test_partial_write_rollback_and_recovery(self):
        self.apply([entry(1)])
        run = self.store.start(101)
        original = self.store.execute
        def fail(sql, params=()):
            if sql.startswith('INSERT INTO entries') and params[1] == 3:
                return original('INSERT INTO nonexistent_table VALUES(?)', (1,))
            return original(sql, params)
        with patch.object(self.store, 'execute', side_effect=fail):
            with self.assertRaises(StoreError):
                self.store.apply(run, [entry(1), entry(2), entry(3)])
        self.assertEqual(self.store.list_changes(101), [])
        self.assertEqual(self.store.execute('SELECT count(*) AS n FROM entries').fetchone()['n'], 1)
        self.store.fail(run, 'synthetic failure')
        self.assertEqual(self.apply([entry(1), entry(2)])['added'], 1)

    def test_reconnect_and_email_dedup(self):
        self.apply([entry(1)])
        cfg = {'EMAIL_FROM': 'sender@example.com', 'EMAIL_TO': 'recipient@example.com'}
        sent = []
        deliver(self.store, 101, ('Subject', 'Body'), 'change:1', cfg, sent.append)
        other = Store(database_url=URL)
        try:
            self.assertTrue(other.initialized(101))
            self.assertIn('accepted', deliver(other, 101, ('Subject', 'Body'), 'change:1', cfg, sent.append))
            self.assertEqual(len(sent), 1)
            def failed_transport(message):
                raise TimeoutError('synthetic')
            with self.assertRaises(RuntimeError):
                deliver(other, 101, ('Subject', 'Body'), 'change:2', cfg, failed_transport)
            self.assertIn('uncertain', deliver(other, 101, ('Subject', 'Body'), 'change:2', cfg, sent.append))
            self.assertEqual(len(sent), 1)
        finally:
            other.close()

    def test_parallel_baseline(self):
        stores = [Store(database_url=URL), Store(database_url=URL)]
        barrier = Barrier(2)
        def worker(store):
            run = store.start(202)
            barrier.wait(timeout=10)
            return store.apply(run, [entry(1, 202)])
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(worker, stores))
            self.assertEqual(sum(r['baseline'] for r in results), 1)
            self.assertEqual(self.store.list_changes(202), [])
        finally:
            for store in stores: store.close()

    def test_parallel_email_claim(self):
        stores = [Store(database_url=URL), Store(database_url=URL)]
        barrier = Barrier(2)
        def worker(store):
            barrier.wait(timeout=10)
            return store.claim_email('same-key', 101, 'a@example.com', 'subject', 'body', 'test-id')
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(worker, stores))
            self.assertEqual(sum(results), 1)
        finally:
            for store in stores: store.close()

    def test_invalid_snapshot_keeps_baseline(self):
        with self.assertRaises(ValueError): self.apply([])
        self.assertFalse(self.store.initialized(101))
        self.apply([entry(1)])
        with self.assertRaises(ValueError): self.apply([entry(2), entry(3, 999)])
        self.assertEqual(self.store.execute('SELECT count(*) AS n FROM entries').fetchone()['n'], 1)

    def test_big_ids_and_bound_text(self):
        big = 5000000000
        row = entry(big, big, "法院 ORDER ? %s ' quoted")
        self.assertTrue(self.apply([row], big)['baseline'])
        self.assertEqual(self.apply([row], big)['modified'], 0)

if __name__ == '__main__': unittest.main(verbosity=2)
