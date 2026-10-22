"""Boundary checks independent of real services and credentials."""
import unittest
from unittest.mock import MagicMock
from datetime import datetime, timezone
from src.store.discovery import DiscoveryStore
from src.ingest.collect_incremental import collect


class CollectorBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.store = DiscoveryStore()
        self.addCleanup(self.store.close)
        self.store.initialize_streams('2026-10-22T00:00:00+00:00')
        self.time = 1800000000.0

    def run_case(self, responses):
        session = MagicMock()
        session.get.side_effect = responses
        def sleep(n): self.time += n
        report = collect(self.store, session, clock=lambda:self.time, sleep=sleep,
                         now=lambda:datetime(2026,10,22,1,tzinfo=timezone.utc))
        return report, session

    def response(self, data, code=200):
        return MagicMock(status_code=code, json=lambda:data)

    def quota(self, remaining=100):
        return self.response({'current_usage':[{'scope':'user','remaining':remaining,'window_seconds':86400}]})

    def test_unsafe_next_rolls_back_entire_page(self):
        report,session=self.run_case([self.quota(),self.response({'results':[{'id':1,'date_modified':'2026-10-22T00:01:00Z'}],'next':'https://evil.example/'})])
        self.assertEqual(report['status'],'failed')
        self.assertEqual(session.get.call_count,2)
        self.assertEqual(self.store.execute('SELECT count(*) AS n FROM discovery_records').fetchone()['n'],0)
        self.assertEqual(self.store.execute('SELECT sum(revision) AS n FROM discovery_state').fetchone()['n'],0)

    def test_remote_headroom_stops_collection(self):
        report,session=self.run_case([self.quota(10)])
        self.assertEqual(report['status'],'remote_quota_pause')
        self.assertEqual(session.get.call_count,1)

    def test_429_persists_pause_across_runs(self):
        report,session=self.run_case([self.quota(), self.response({},429)])
        self.assertEqual(report['status'],'failed')
        self.time += 60
        second,session=self.run_case([])
        self.assertEqual(second['status'],'budget_or_pacing_pause')
        session.get.assert_not_called()

    def test_unknown_quota_fails_closed(self):
        report,session=self.run_case([self.response({'unexpected':[]})])
        self.assertEqual(report['status'],'failed')
        self.assertEqual(session.get.call_count,1)
