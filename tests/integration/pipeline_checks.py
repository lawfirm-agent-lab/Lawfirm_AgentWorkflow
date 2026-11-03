"""Pipeline tests rerun on disposable localhost PostgreSQL. Never reads DATABASE_URL or dotenv."""
import os
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.store.pipeline import PipelineStore
from tests.test_pipeline import PipelineTests
from tests.test_source_and_budget import BudgetTests, SourceTests
from tests.test_llm_analyzer import AnalyzerTests, ShadowAndRegistryTests
from tests.test_document import AnalyzerTests as DocumentAnalyzerTests
from tests.test_matching_cloud import CloudTests

port = os.environ.get('TEST_POSTGRES_PORT', '')
if not port.isascii() or not port.isdecimal() or not 1 <= int(port) <= 65535:
    raise SystemExit('Provide a disposable localhost TEST_POSTGRES_PORT')
URL = f'postgresql://postgres:local-test-only@127.0.0.1:{int(port)}/lawfirm_test'
TABLES = ('matching_access_keys, matching_runs, matching_cvs, llm_calls, court_info, docket_info, notifications, analyses, events, subscriptions, recipients, docket_watch, api_requests, api_control, '
          'pipeline_runs, email_outbox, changes, entries, runs, dockets')


def make_store(self):
    store = PipelineStore(database_url=URL)
    store.execute(f'TRUNCATE {TABLES} RESTART IDENTITY CASCADE')
    return store


class PostgreSQLPipelineTests(PipelineTests):
    make_store = make_store

    def test_rls_enabled_on_every_owned_public_table(self):
        rows = self.store.execute("SELECT tablename, rowsecurity FROM pg_tables WHERE schemaname='public' "
                                  "AND tableowner=current_user").fetchall()
        self.assertIn('events', {r['tablename'] for r in rows})
        self.assertEqual([r['tablename'] for r in rows if not r['rowsecurity']], [])

    def test_concurrent_open_applies_migrations_once(self):
        other = PipelineStore(database_url=URL)
        try:
            n = other.execute("SELECT count(*) AS n FROM schema_migrations WHERE version='001_pipeline'").fetchone()['n']
            self.assertEqual(n, 1)
        finally:
            other.close()


class PostgreSQLSourceTests(SourceTests):
    make_store = make_store


class PostgreSQLBudgetTests(BudgetTests):
    make_store = make_store


class PostgreSQLLLMAnalyzerTests(AnalyzerTests):
    make_store = make_store


class PostgreSQLShadowTests(ShadowAndRegistryTests):
    make_store = make_store


class PostgreSQLDocumentAnalyzerTests(DocumentAnalyzerTests):
    make_store = make_store


class PostgreSQLCloudTests(CloudTests):
    def open(self):
        return PipelineStore(database_url=URL)

    def setUp(self):
        super().setUp()
        self.store.execute('TRUNCATE matching_runs, matching_cvs')


class PostgreSQLSiteFunctionTests(unittest.TestCase):
    """The website's database functions return data only for an active access key."""

    def setUp(self):
        import tempfile
        from datetime import datetime, timezone
        from src.matching.cloud import CloudRuns
        from tests.test_web_daily import daily_run
        self.store = PipelineStore(database_url=URL)
        self.addCleanup(self.store.close)
        self.store.execute('TRUNCATE matching_access_keys, matching_runs, matching_cvs')
        self.cloud = CloudRuns(self.store)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        daily_run(root, '20261103-090000-aaaaaa', 'jane.pdf', datetime.now(timezone.utc).isoformat(timespec='seconds'),
                  ['https://cl/docket/1/', 'https://cl/docket/2/'])
        self.cloud.save_run(root / '20261103-090000-aaaaaa')
        self.key = self.cloud.new_key('test')

    def call(self, sql, *args):
        return next(iter(self.store.execute(sql, args).fetchone().values()))

    def test_feed_lists_recent_checks_for_a_valid_key(self):
        feed = self.call('SELECT matching_feed(?, ?)', self.key, 14)
        self.assertEqual([r['run_id'] for r in feed], ['20261103-090000-aaaaaa'])
        expert = feed[0]['experts'][0]
        self.assertEqual(expert['name'], 'Jane Doe')
        self.assertEqual([c['tier'] for c in expert['cases']], ['high'])
        self.assertEqual(len(expert['candidates']), 2)
        run = self.call('SELECT matching_run(?, ?)', self.key, '20261103-090000-aaaaaa')
        self.assertEqual(run['result'][0]['results'][0]['match']['summary'], 'Strong fit.')

    def test_wrong_or_revoked_key_reads_nothing(self):
        from src.store.db import StoreError
        for key in ('wrong', '', None):
            with self.assertRaises(StoreError):
                self.call('SELECT matching_feed(?, ?)', key, 14)
        self.cloud.revoke_keys('test')
        with self.assertRaises(StoreError):
            self.call('SELECT matching_run(?, ?)', self.key, '20261103-090000-aaaaaa')


if __name__ == '__main__':
    suite = unittest.TestSuite()
    for case in (PostgreSQLPipelineTests, PostgreSQLSourceTests, PostgreSQLBudgetTests,
                 PostgreSQLLLMAnalyzerTests, PostgreSQLShadowTests,
                 PostgreSQLDocumentAnalyzerTests, PostgreSQLCloudTests,
                 PostgreSQLSiteFunctionTests):
        suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(case))
    raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())
