import unittest
from unittest.mock import patch
from src.store.db import Store
from src.notify.queue import send_pending_changes
from src.notify.digest import build_digest
from src.notify.email import settings


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.addCleanup(self.store.close)
        self.cfg = {'EMAIL_FROM':'sender@example.com','EMAIL_TO':'recipient@example.com'}
        self.row = {'id':1,'docket':'https://www.courtlistener.com/api/rest/v4/dockets/1/',
                    'description':'ORDER setting hearing.','date_filed':'2020-01-01','recap_documents':[]}
        self.store.apply(self.store.start(1),[self.row])

    def add_change(self):
        self.store.apply(self.store.start(1),[self.row,{**self.row,'id':2}])

    def test_baseline_silent_then_old_filing_delivered_once(self):
        sent=[]
        self.assertFalse(send_pending_changes(self.store,1,self.cfg,sent.append))
        self.assertEqual(sent,[])
        self.add_change()
        self.assertFalse(send_pending_changes(self.store,1,self.cfg,sent.append))
        self.assertFalse(send_pending_changes(self.store,1,self.cfg,sent.append))
        self.assertEqual(len(sent),1)
        text=sent[0].get_content()
        self.assertIn('2020-01-01',text)
        self.assertIn('No AI analysis',text)
        self.assertIn('docket-entries/2/',text)
        self.assertNotIn('filed today',text)

    def test_uncertain_never_retried_and_remains_failure(self):
        self.add_change()
        attempts=[]
        def fail(message):
            attempts.append(message)
            raise TimeoutError('synthetic')
        self.assertTrue(send_pending_changes(self.store,1,self.cfg,fail))
        self.assertTrue(send_pending_changes(self.store,1,self.cfg,fail))
        self.assertEqual(len(attempts),1)

    def test_rules_still_exclude_party_motion(self):
        self.assertIsNone(build_digest(1,[{**self.row,'description':'MOTION by plaintiff'}],detected_change=True))

    def test_bad_port_does_not_echo_private_value(self):
        import os
        cfg={**self.cfg,'SMTP_HOST':'localhost','SMTP_PORT':'private-value','SMTP_USERNAME':'test','SMTP_PASSWORD':'test'}
        with patch.dict(os.environ,cfg,clear=True):
            with self.assertRaises(ValueError) as error: settings()
        self.assertNotIn('private-value',str(error.exception))


class CloudEmailIntegrationTests(unittest.TestCase):
    def test_fetch_failure_still_drains_prior_changes(self):
        import tempfile
        import os
        from pathlib import Path
        import requests
        from src import main as app
        from contextlib import ExitStack
        with ExitStack() as stack:
            root=Path(stack.enter_context(tempfile.TemporaryDirectory()))
            (root/'config.yaml').write_text('dockets: [1]\n')
            store=Store(':memory:')
            stack.callback(store.conn.close)
            stack.enter_context(patch.object(store,'close'))
            stack.enter_context(patch.object(app,'ROOT',root))
            stack.enter_context(patch.object(app,'Store',return_value=store))
            dotenv=stack.enter_context(patch.object(app,'load_dotenv'))
            stack.enter_context(patch.object(app,'settings',return_value={'EMAIL_FROM':'a@example.com','EMAIL_TO':'b@example.com'}))
            stack.enter_context(patch.object(app,'fetch_entries',side_effect=requests.Timeout('test')))
            drain=stack.enter_context(patch.object(app,'send_pending_changes',return_value=False))
            stack.enter_context(patch.dict(os.environ,{'DATABASE_URL':'postgresql://localhost/test','COURTLISTENER_TOKEN':'fake'},clear=True))
            stack.enter_context(patch('sys.argv',['main','--cloud-email']))
            self.assertEqual(app.main(),1)
            drain.assert_called_once()
            dotenv.assert_not_called()
            self.assertFalse((root/'data').exists())
