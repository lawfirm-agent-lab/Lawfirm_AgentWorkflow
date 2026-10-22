"""Event identity, immutable snapshots, and atomic checkpoint behavior."""
import json
import unittest
from unittest.mock import patch
from src.store.discovery import DiscoveryStore

START='2026-10-22T00:00:00+00:00'
END='2026-10-22T00:15:00+00:00'
ROW={'id':1,'date_modified':START,'docket':'example'}

class EventTests(unittest.TestCase):
    def setUp(self):
        self.store=DiscoveryStore()
        self.addCleanup(self.store.close)
        self.store.initialize_streams(START)
    def state(self,stream='docket-entries'):
        return dict(self.store.execute('SELECT * FROM discovery_state WHERE stream=?',(stream,)).fetchone())
    def save(self,row,stream='docket-entries'):
        return self.store.save_page(self.state(stream),[row],END,None,None)
    def events(self):
        return [dict(r) for r in self.store.execute('SELECT * FROM discovery_events ORDER BY source_modified_at')]
    def test_repeat_version_is_deduplicated(self):
        self.save(ROW); self.save(ROW)
        events=self.events()
        self.assertEqual(len(events),1)
        self.assertEqual(events[0]['kind'],'first_seen')
        self.assertEqual(events[0]['status'],'pending_enrichment')
    def test_new_version_preserves_old_payload(self):
        self.save(ROW)
        updated=dict(ROW,date_modified='2026-10-22T00:02:00+00:00')
        self.save(updated)
        events=self.events()
        self.assertEqual(len(events),2)
        self.assertEqual(json.loads(events[0]['payload_json']),ROW)
        self.assertEqual(events[1]['kind'],'metadata_changed')
    def test_resource_identity_is_namespaced(self):
        self.save(ROW); self.save(ROW,'opinions')
        self.assertEqual(len(self.events()),2)
    def test_event_insert_failure_rolls_back_checkpoint(self):
        original=self.store.execute
        state=self.state()
        def fail(sql,params=()):
            if sql.startswith('INSERT INTO discovery_events'): raise ValueError('simulated failure')
            return original(sql,params)
        with patch.object(self.store,'execute',side_effect=fail):
            with self.assertRaises(ValueError): self.save(ROW)
        self.assertEqual(self.state(),state)
        self.assertEqual(len(self.events()),0)
        self.assertEqual(self.store.execute('SELECT count(*) AS n FROM discovery_records').fetchone()['n'],0)
    def test_record_failure_rolls_back_event(self):
        original=self.store.execute
        def fail(sql,params=()):
            if sql.startswith('INSERT INTO discovery_records'): raise ValueError('simulated failure')
            return original(sql,params)
        with patch.object(self.store,'execute',side_effect=fail):
            with self.assertRaises(ValueError): self.save(ROW)
        self.assertEqual(len(self.events()),0)
        self.assertEqual(self.state()['revision'],0)
