import io
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from unittest.mock import patch
import requests
from src import main as app
from src.store.db import Store, StoreError


class CloudCheckTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.root = Path(directory)
        (self.root / 'config.yaml').write_text('dockets: [101]\n')
        self.stack.enter_context(patch.object(app, 'ROOT', self.root))
        self.stack.enter_context(patch('sys.argv', ['main', '--cloud-check']))
        self.env = self.stack.enter_context(patch.dict(os.environ, {
            'DATABASE_URL': 'postgresql://u:private-password@localhost/test',
            'COURTLISTENER_TOKEN': 'synthetic-token'}, clear=True))
        self.dotenv = self.stack.enter_context(patch.object(app, 'load_dotenv'))
        self.fetch = self.stack.enter_context(patch.object(app, 'fetch_entries'))
        self.deliver = self.stack.enter_context(patch.object(app, 'deliver'))
        self.digest = self.stack.enter_context(patch.object(app, 'build_digest'))
        self.store = Store(':memory:')
        self.addCleanup(self.store.conn.close)
        self.stack.enter_context(patch.object(self.store, 'close'))
        self.factory = self.stack.enter_context(patch.object(app, 'Store', return_value=self.store))
        self.row = {'id': 1, 'docket': 'https://www.courtlistener.com/api/rest/v4/dockets/101/',
                    'description': 'ORDER test', 'recap_documents': []}
        self.fetch.return_value = [self.row]

    def run_check(self):
        output = io.StringIO()
        with redirect_stdout(output):
            result = app.main()
        self.assertNotIn('private-password', output.getvalue())
        self.assertNotIn('synthetic-token', output.getvalue())
        self.dotenv.assert_not_called()
        self.deliver.assert_not_called()
        self.digest.assert_not_called()
        self.assertFalse((self.root / 'data').exists())
        return result

    def test_first_baseline_repeat_and_new_entry(self):
        self.assertEqual(self.run_check(), 0)
        self.assertTrue(self.store.initialized(101))
        self.assertEqual(self.store.list_changes(101), [])
        self.assertIn('sslmode=require', self.factory.call_args.kwargs['database_url'])
        self.assertEqual(self.run_check(), 0)
        self.assertEqual(self.store.list_changes(101), [])
        self.fetch.return_value = [self.row, {**self.row, 'id': 2}]
        self.assertEqual(self.run_check(), 0)
        self.assertEqual(len(self.store.list_changes(101)), 1)

    def test_missing_secrets_never_fetch_or_open_database(self):
        for missing in ('DATABASE_URL', 'COURTLISTENER_TOKEN'):
            with patch.dict(os.environ, {missing: ''}):
                self.assertEqual(self.run_check(), 1)
        self.fetch.assert_not_called()
        self.factory.assert_not_called()

    def test_insecure_database_never_connects(self):
        with patch.dict(os.environ, {'DATABASE_URL':'postgresql://localhost/test?sslmode=disable'}):
            self.assertEqual(self.run_check(), 1)
        self.factory.assert_not_called()

    def test_api_failure_preserves_baseline_and_hides_details(self):
        self.assertEqual(self.run_check(), 0)
        self.fetch.side_effect = requests.Timeout('private-password synthetic-token')
        self.assertEqual(self.run_check(), 1)
        self.assertTrue(self.store.initialized(101))
        error = self.store.execute('SELECT error FROM runs ORDER BY id DESC LIMIT 1').fetchone()['error']
        self.assertEqual(error, 'Timeout')
        self.assertEqual(self.store.list_changes(101), [])

    def test_database_failure_does_not_fall_back(self):
        self.factory.side_effect = StoreError('Database initialization failed')
        self.assertEqual(self.run_check(), 1)
        self.fetch.assert_not_called()

    def test_email_flag_rejected(self):
        with patch('sys.argv', ['main','--cloud-check','--send-email']), redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                app.main()
        self.assertEqual(error.exception.code, 2)
        self.factory.assert_not_called()
