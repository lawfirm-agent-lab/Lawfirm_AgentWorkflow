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
TABLES = ('matching_requests, matching_access_keys, matching_runs, matching_cvs, llm_calls, court_info, docket_info, notifications, analyses, events, subscriptions, recipients, docket_watch, api_requests, api_control, '
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
        self.store.execute('TRUNCATE matching_requests, matching_access_keys, matching_runs, matching_cvs, matching_feedback')
        self.cloud = CloudRuns(self.store)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = self.root = Path(directory.name)
        daily_run(root, '20261103-090000-aaaaaa', 'jane.pdf', datetime.now(timezone.utc).isoformat(timespec='seconds'),
                  ['https://cl/docket/1/', 'https://cl/docket/2/'])
        self.cloud.save_run(root / '20261103-090000-aaaaaa')
        self.key = self.cloud.new_key('test', admin=True)  # The fixture run has no owner: the admin's.

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

    def test_upload_queues_a_run_and_saves_the_cv(self):
        import base64
        pdf = base64.b64encode(b'%PDF-1.4 ' + b'x' * 200).decode()
        answer = self.call('SELECT matching_submit(?, ?, ?, ?)', self.key, '../My CV (2026).pdf', pdf, True)
        self.assertEqual(answer['cv_name'], 'My_CV_2026_.pdf')
        state = self.call('SELECT matching_request(?, ?)', self.key, answer['request_id'])
        self.assertEqual(state['state'], 'queued')
        self.assertIn('cannot start', state['error'])  # No pg_net or GitHub token in the test database.
        self.assertEqual([name for name, _ in self.cloud.cvs()], ['My_CV_2026_.pdf'])
        feed = self.call('SELECT matching_feed(?, ?)', self.key, 14)
        self.assertIs(feed[0]['daily'], True)

    def test_upload_checks_key_file_and_daily_limit(self):
        import base64
        from src.store.db import StoreError
        pdf = base64.b64encode(b'%PDF-1.4 ' + b'x' * 200).decode()
        with self.assertRaises(StoreError):
            self.call('SELECT matching_submit(?, ?, ?, ?)', 'wrong', 'cv.pdf', pdf, False)
        with self.assertRaises(StoreError):  # Not a PDF.
            self.call('SELECT matching_submit(?, ?, ?, ?)', self.key, 'cv.pdf', base64.b64encode(b'hello' * 50).decode(), False)
        for _ in range(10):
            self.call('SELECT matching_submit(?, ?, ?, ?)', self.key, 'cv.pdf', pdf, False)
        with self.assertRaises(StoreError):
            self.call('SELECT matching_submit(?, ?, ?, ?)', self.key, 'cv.pdf', pdf, False)
        self.assertEqual(self.cloud.cvs(), [])  # save=false: the CV is kept only with the request.

    def test_site_lists_stops_and_deletes_saved_cvs(self):
        from src.store.db import StoreError
        self.cloud.save_cv('jane.pdf', b'%PDF-1.4')
        listed = self.call('SELECT matching_cvs(?)', self.key)
        self.assertEqual([(c['name'], c['active'], c['runs']) for c in listed], [('jane.pdf', True, 1)])
        self.call('SELECT matching_cv_active(?, ?, ?)', self.key, 'jane.pdf', False)
        self.assertEqual(self.cloud.cvs(), [])
        with self.assertRaises(StoreError):
            self.call('SELECT matching_cv_delete(?, ?)', 'wrong', 'jane.pdf')
        done = self.call('SELECT matching_cv_delete(?, ?)', self.key, 'jane.pdf')
        self.assertEqual((done['cvs'], done['runs']), (1, 1))
        self.assertEqual(self.call('SELECT matching_feed(?, ?)', self.key, 14), [])
        with self.assertRaises(StoreError):
            self.call('SELECT matching_cv_delete(?, ?)', self.key, 'jane.pdf')

    def test_people_see_and_manage_only_their_own_cvs(self):
        import base64
        from src.store.db import StoreError
        pdf = base64.b64encode(b'%PDF-1.4 ' + b'x' * 200).decode()
        alice, bob = self.cloud.new_key('alice'), self.cloud.new_key('bob')
        self.assertEqual(self.call('SELECT matching_whoami(?)', alice), {'who': 'alice', 'admin': False})
        self.assertEqual(self.call('SELECT matching_feed(?, ?)', alice, 14), [])  # The admin's run is not hers.
        first = self.call('SELECT matching_submit(?, ?, ?, ?)', alice, 'cv.pdf', pdf, True)
        second = self.call('SELECT matching_submit(?, ?, ?, ?)', bob, 'cv.pdf', pdf, True)
        self.assertEqual((first['cv_name'], second['cv_name']), ('cv.pdf', 'cv-2.pdf'))  # Bob cannot replace hers.
        self.assertEqual([c['name'] for c in self.call('SELECT matching_cvs(?)', alice)], ['cv.pdf'])
        self.assertEqual([c['name'] for c in self.call('SELECT matching_cvs(?)', bob)], ['cv-2.pdf'])
        self.assertEqual(len(self.call('SELECT matching_cvs(?)', self.key)), 2)  # The admin sees both.
        self.assertIsNone(self.call('SELECT matching_request(?, ?)', bob, first['request_id']))
        with self.assertRaises(StoreError):
            self.call('SELECT matching_cv_active(?, ?, ?)', bob, 'cv.pdf', False)
        with self.assertRaises(StoreError):
            self.call('SELECT matching_cv_delete(?, ?)', bob, 'cv.pdf')
        self.assertIsNone(self.call('SELECT matching_run(?, ?)', alice, '20261103-090000-aaaaaa'))  # The admin's run.
        self.call('SELECT matching_cv_active(?, ?, ?)', alice, 'cv.pdf', False)
        self.call('SELECT matching_cv_delete(?, ?)', self.key, 'cv-2.pdf')  # The admin can manage anyone's.
        self.assertEqual([n for n, _ in self.cloud.cvs()], [])

    def test_one_feedback_per_case_and_admins_can_delete_it(self):
        from src.store.db import StoreError
        run = '20261103-090000-aaaaaa'
        given = self.call('SELECT matching_feedback_give(?, ?, ?, ?, ?, ?, ?, ?)', self.key, run, 0, 'down',
                          'not_relevant', 'low', False, '  Pro se suit, no real technical issue.  ')
        self.assertEqual((given['verdict'], given['reason'], given['should_be'], given['note']),
                         ('down', 'not_relevant', 'low', 'Pro se suit, no real technical issue.'))
        with self.assertRaisesRegex(StoreError, 'failed'):  # A second rating of the same case is refused.
            self.call('SELECT matching_feedback_give(?, ?, ?, ?)', self.key, run, 0, 'up')
        self.call('SELECT matching_feedback_give(?, ?, ?, ?)', self.key, run, 0, 'gmail')  # Not a rating: allowed.
        self.call('SELECT matching_feedback_give(?, ?, ?, ?)', self.key, run, 0, 'copy')
        self.assertEqual(self.call('SELECT matching_feedback_case(?, ?, ?)', self.key, run, 0)['verdict'], 'down')
        rows = self.call('SELECT matching_feedback_all(?)', self.key)
        self.assertEqual([(r['kind'], r['verdict']) for r in rows], [('email', 'copy'), ('email', 'gmail'), ('rating', 'down')])
        snap = rows[2]['snapshot']
        self.assertEqual((rows[2]['case_name'], rows[2]['cv'], rows[2]['who']), ('Case 1', 'jane.pdf', 'test'))
        self.assertEqual((snap['tier'], snap['source'], snap['expert'], snap['daily']), ('high', 'courtlistener', 'Jane Doe', True))
        self.assertIs(snap['screen']['relevant'], True)
        self.assertEqual(self.cloud.feedback(), rows)  # The feedback command reads the same rows.
        # The snapshot stays after the run is deleted.
        self.store.execute('DELETE FROM matching_runs')
        self.assertEqual(self.call('SELECT matching_feedback_all(?)', self.key)[2]['snapshot']['tier'], 'high')
        # Deleting: one row by an admin from the website, or everything one person gave from the command line.
        alice = self.cloud.new_key('alice')
        with self.assertRaises(StoreError):
            self.call('SELECT matching_feedback_delete(?, ?)', alice, rows[2]['id'])
        self.assertEqual(self.call('SELECT matching_feedback_delete(?, ?)', self.key, rows[2]['id']), 1)
        self.assertIsNone(self.call('SELECT matching_feedback_case(?, ?, ?)', self.key, run, 0))
        self.assertEqual(self.cloud.delete_feedback('test'), 2)
        self.assertEqual(self.call('SELECT matching_feedback_all(?)', self.key), [])

    def test_feedback_values_and_access_are_checked(self):
        from src.store.db import StoreError
        from tests.test_web_daily import daily_run
        run = '20261103-090000-aaaaaa'
        bad = [(run, 0, 'maybe', None, None, ''), (run, 0, 'down', 'bored', None, ''), (run, 0, 'down', None, 'top', ''),
               (run, 1, 'up', None, None, ''), (run, -1, 'up', None, None, ''), (run, 0, 'up', None, None, 'x' * 1001),
               ('no-such-run', 0, 'up', None, None, '')]
        for which, idx, verdict, reason, level, note in bad:
            with self.subTest(verdict=verdict, idx=idx), self.assertRaises(StoreError):
                self.call('SELECT matching_feedback_give(?, ?, ?, ?, ?, ?, ?, ?)', self.key, which, idx, verdict,
                          reason, level, False, note)
        alice = self.cloud.new_key('alice')
        with self.assertRaises(StoreError):  # The admin's run is not hers.
            self.call('SELECT matching_feedback_give(?, ?, ?, ?)', alice, run, 0, 'up')
        with self.assertRaises(StoreError):  # Only an admin reads everyone's feedback.
            self.call('SELECT matching_feedback_all(?)', alice)
        daily_run(self.root, '20261104-090000-bbbbbb', 'alice.pdf', '2026-11-04T09:00:00+00:00', ['https://cl/docket/9/'])
        self.cloud.save_run(self.root / '20261104-090000-bbbbbb', owner='alice')
        given = self.call('SELECT matching_feedback_give(?, ?, ?, ?, ?, ?, ?, ?)', alice, '20261104-090000-bbbbbb', 0,
                          'up', None, None, True, None)
        self.assertIs(given['would_contact'], True)
        self.assertIsNone(self.call('SELECT matching_feedback_case(?, ?, ?)', self.key, '20261104-090000-bbbbbb', 0))
        self.assertEqual([r['who'] for r in self.call('SELECT matching_feedback_all(?)', self.key)], ['alice'])

    def test_logs_are_for_admin_links_only(self):
        from src.matching import logs
        from src.store.db import StoreError
        self.store.execute("INSERT INTO matching_requests (request_id, cv_name, save, state, created_at, updated_at, error, owner) "
                           "VALUES ('q1', 'bad.pdf', 0, 'failed', ?, ?, 'InvalidOutput: defendants', 'alice')",
                           (datetime_now(), datetime_now()))
        data = self.call('SELECT matching_log_overview(?, ?)', self.key, 7)
        self.assertEqual(data['schedule'], [])  # No pg_cron in the test database.
        run = data['runs'][0]
        self.assertEqual((run['run_id'], run['cv'], run['daily'], run['cases'], run['candidates'], run['tiers']),
                         ('20261103-090000-aaaaaa', 'jane.pdf', True, 1, 2, {'high': 1}))
        self.assertEqual([q['error'] for q in data['requests']], ['InvalidOutput: defendants'])
        text = logs.report(data)
        self.assertIn('jane.pdf (admin) · daily · done', text)
        self.assertIn('bad.pdf (alice): InvalidOutput: defendants', text)
        detail = self.call('SELECT matching_log_run(?, ?)', self.key, '20261103-090000-aaaaaa')
        self.assertEqual((detail['status']['state'], detail['trace']), ('done', []))
        alice = self.cloud.new_key('alice')
        for sql, args in (('SELECT matching_log_overview(?, ?)', (alice, 7)), ('SELECT matching_log_run(?, ?)', (alice, 'x'))):
            with self.subTest(sql=sql), self.assertRaises(StoreError):
                self.call(sql, *args)
        if self.call("SELECT count(*) FROM pg_roles WHERE rolname = 'anon'"):
            self.assertFalse(self.call("SELECT has_function_privilege('anon', 'public.matching_log_data(integer)', 'EXECUTE')"))
            self.assertTrue(self.call("SELECT has_function_privilege('anon', 'public.matching_log_run(text, text)', 'EXECUTE')"))


def datetime_now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


if __name__ == '__main__':
    suite = unittest.TestSuite()
    for case in (PostgreSQLPipelineTests, PostgreSQLSourceTests, PostgreSQLBudgetTests,
                 PostgreSQLLLMAnalyzerTests, PostgreSQLShadowTests,
                 PostgreSQLDocumentAnalyzerTests, PostgreSQLCloudTests,
                 PostgreSQLSiteFunctionTests):
        suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(case))
    raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())
