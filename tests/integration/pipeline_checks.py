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
TABLES = ('matching_runs, matching_cvs, llm_calls, court_info, docket_info, notifications, analyses, events, subscriptions, recipients, docket_watch, api_requests, api_control, '
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


if __name__ == '__main__':
    suite = unittest.TestSuite()
    for case in (PostgreSQLPipelineTests, PostgreSQLSourceTests, PostgreSQLBudgetTests,
                 PostgreSQLLLMAnalyzerTests, PostgreSQLShadowTests,
                 PostgreSQLDocumentAnalyzerTests, PostgreSQLCloudTests):
        suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(case))
    raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())
