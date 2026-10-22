import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from urllib.parse import urlencode
from src.store.discovery import DiscoveryStore
from src.ingest.collect_incremental import collect, parameters

START = '2026-10-22T00:00:00+00:00'
END = '2026-10-22T00:15:00+00:00'
ROW = {'id': 1, 'date_modified': START, 'docket': 'example'}

class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.store = DiscoveryStore()
        self.addCleanup(self.store.close)
        self.store.initialize_streams(START)
        self.t = 1800000000.0
    def run_collect(self, pages, **kwargs):
        quota = {'current_usage': [{'scope': 'user', 'remaining': 100, 'window_seconds': 86400}]}
        session = MagicMock()
        session.get.side_effect = [MagicMock(status_code=200, json=lambda: quota)] + [MagicMock(status_code=200, json=lambda p=p: p) for p in pages]
        def sleep(n): self.t += n
        result = collect(self.store, session, clock=lambda: self.t, sleep=sleep,
                         now=lambda: datetime(2026,10,22,1,tzinfo=timezone.utc), **kwargs)
        return result, session
    def state(self):
        return dict(self.store.execute("SELECT * FROM discovery_state WHERE stream='docket-entries'").fetchone())
    def test_partial_and_resume(self):
        nxt = 'https://www.courtlistener.com/api/rest/v4/docket-entries/?'+urlencode(parameters('docket-entries',START,END))+'&cursor=x'
        report, _ = self.run_collect([{'results':[ROW], 'next':nxt}], max_requests=2)
        self.assertEqual(report['status'], 'partial')
        self.assertEqual(self.state()['start_at'], START)
        self.assertEqual(self.state()['next_url'], nxt)
        self.t += 20
        report, session = self.run_collect([{'results':[], 'next':None}, {'results':[], 'next':None}], max_requests=3)
        self.assertEqual(report['pages'], 2)
        self.assertEqual(self.state()['start_at'], END)
        self.assertEqual(session.get.call_args_list[2].args[0], nxt)
    def test_atomic_rollback(self):
        state = self.state()
        original = self.store.execute
        def broken(sql, params=()):
            if sql.startswith('INSERT INTO discovery_records'): raise ValueError('simulated')
            return original(sql,params)
        with patch.object(self.store,'execute',side_effect=broken):
            with self.assertRaises(ValueError): self.store.save_page(state,[ROW],END,None,None)
        self.assertEqual(self.state(),state)
    def test_dedup(self):
        self.store.save_page(self.state(),[ROW],END,None,None)
        self.assertEqual(self.store.save_page(self.state(),[ROW],END,None,None),0)
        self.assertEqual(self.store.execute('SELECT COUNT(*) AS n FROM discovery_records').fetchone()['n'],1)
    def test_failed_page_retains_progress(self):
        report,_ = self.run_collect([{'results':[dict(ROW,date_modified='2000-01-01T00:00:00Z')],'next':None}])
        self.assertEqual(report['status'],'failed')
        self.assertEqual(self.state()['revision'],0)
    def test_budget_reserved_before_request(self):
        report,session = self.run_collect([],daily_limit=1)
        self.assertEqual(report['status'],'budget_or_pacing_pause')
        self.assertEqual(session.get.call_count,1)
    def test_stale_checkpoint_conflict(self):
        state=self.state()
        self.store.save_page(state,[],END,None,None)
        with self.assertRaises(ValueError): self.store.save_page(state,[ROW],END,None,None)
    def test_initialize_preserves_progress(self):
        self.store.save_page(self.state(),[],END,None,None)
        self.store.initialize_streams('2030-01-01T00:00:00+00:00')
        self.assertEqual(self.state()['start_at'],END)
