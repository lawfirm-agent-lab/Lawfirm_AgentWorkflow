"""Disposable localhost PostgreSQL checks. Never reads DATABASE_URL or dotenv."""
import os
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.test_collector import CollectorTests, START
from src.store.discovery import DiscoveryStore

port = os.environ.get('TEST_POSTGRES_PORT', '')
if not port.isascii() or not port.isdecimal() or not 1 <= int(port) <= 65535:
    raise SystemExit('Provide a disposable localhost TEST_POSTGRES_PORT')
URL = f'postgresql://postgres:local-test-only@127.0.0.1:{int(port)}/lawfirm_test'


class PostgreSQLCollectorTests(CollectorTests):
    def setUp(self):
        self.store = DiscoveryStore(database_url=URL)
        self.addCleanup(self.store.close)
        self.store.execute('TRUNCATE discovery_events, discovery_records, discovery_state, discovery_requests, discovery_runs')
        self.store.execute('UPDATE discovery_control SET blocked_until=0')
        self.store.initialize_streams(START)
        self.t = 1800000000.0

    def test_reconnect_preserves_checkpoint_and_budget(self):
        from tests.test_collector import ROW, END
        self.store.save_page(self.state(), [ROW], END, None, None)
        self.assertTrue(self.store.reserve(self.t, 'request-test', 1))
        other = DiscoveryStore(database_url=URL)
        try:
            self.assertEqual(other.execute("SELECT start_at FROM discovery_state WHERE stream='docket-entries'").fetchone()['start_at'], END)
            self.assertFalse(other.reserve(self.t + 20, 'second', 1))
            self.assertEqual(other.execute('SELECT count(*) AS n FROM discovery_records').fetchone()['n'], 1)
        finally:
            other.close()

    def test_session_lock_excludes_second_connection(self):
        other = DiscoveryStore(database_url=URL)
        try:
            self.assertTrue(self.store.execute('SELECT pg_try_advisory_lock(7313279901) AS locked').fetchone()['locked'])
            self.assertFalse(other.execute('SELECT pg_try_advisory_lock(7313279901) AS locked').fetchone()['locked'])
        finally:
            self.store.execute('SELECT pg_advisory_unlock(7313279901)')
            other.close()


from tests.test_discovery_events import EventTests

class PostgreSQLEventTests(EventTests):
    def setUp(self):
        self.store = DiscoveryStore(database_url=URL)
        self.addCleanup(self.store.close)
        self.store.execute('TRUNCATE discovery_events, discovery_records, discovery_state')
        self.store.initialize_streams(START)


if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(PostgreSQLCollectorTests)
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(PostgreSQLEventTests))
    raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())
