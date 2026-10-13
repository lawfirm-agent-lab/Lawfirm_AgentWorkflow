import io
import os
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch
from urllib.parse import urlsplit, parse_qs
from src.store.init_database import main
from src.store.db import Store


class InitializeTests(unittest.TestCase):
    def run_init(self, url):
        output = io.StringIO()
        with patch.dict(os.environ, {'DATABASE_URL': url}, clear=True), redirect_stdout(output):
            result = main()
        self.assertNotIn('private-password', output.getvalue())
        return result

    @patch('src.store.init_database.Store')
    def test_missing_invalid_and_insecure_url(self, store):
        for url in ('', 'https://localhost', 'postgresql://localhost/db?sslmode=disable'):
            self.assertEqual(self.run_init(url), 1)
        store.assert_not_called()

    @patch('src.store.init_database.Store')
    def test_tls_and_close_without_business_calls(self, store):
        self.assertEqual(self.run_init('postgresql://u:private-password@localhost/db'), 0)
        query = parse_qs(urlsplit(store.call_args.kwargs['database_url']).query)
        self.assertEqual(query['sslmode'], ['require'])
        self.assertIn('search_path=public', query['options'][0])
        self.assertEqual([c[0] for c in store.return_value.method_calls], ['close'])

    @patch('src.store.init_database.Store')
    def test_stronger_tls_preserved(self, store):
        self.assertEqual(self.run_init('postgresql://localhost/db?sslmode=verify-full'), 0)
        self.assertIn('sslmode=verify-full', store.call_args.kwargs['database_url'])

    @patch('src.store.init_database.Store', side_effect=RuntimeError('private-password'))
    def test_failure_redacted(self, store):
        self.assertEqual(self.run_init('postgresql://u:private-password@localhost/db'), 1)

    def test_repeated_schema_initialization_preserves_existing_rows(self):
        store = Store(':memory:')
        self.addCleanup(store.close)
        with store.transaction():
            store.execute('INSERT INTO dockets VALUES(?,?)', (42, 'test baseline'))
        store._initialize()
        self.assertEqual(store.execute('SELECT baseline_at FROM dockets WHERE id=?', (42,)).fetchone()['baseline_at'], 'test baseline')
        self.assertEqual(store.execute('SELECT count(*) AS n FROM runs').fetchone()['n'], 0)
