import contextlib
import io
import os
import unittest
from unittest.mock import MagicMock, patch
from src.ingest.collect_incremental import main


class InitializationDiagnosticsTests(unittest.TestCase):
    def invoke(self, env, store):
        output = io.StringIO()
        with patch.dict(os.environ, env, clear=True), patch('sys.argv', ['collector', '--initialize']), patch('src.ingest.collect_incremental.DiscoveryStore', store), contextlib.redirect_stdout(output):
            result = main()
        self.assertNotIn('secret-value', output.getvalue())
        return result, output.getvalue()

    def test_missing_database_is_explicit(self):
        store = MagicMock()
        result, output = self.invoke({}, store)
        self.assertEqual(result, 1)
        self.assertIn('DATABASE_URL secret is missing', output)
        store.assert_not_called()

    def test_connection_failure_is_redacted_and_staged(self):
        result, output = self.invoke({'DATABASE_URL':'postgresql://secret-value@localhost/db'}, MagicMock(side_effect=RuntimeError('secret-value')))
        self.assertEqual(result, 1)
        self.assertIn('stage=database_connection_or_schema_creation', output)

    def test_initialize_does_not_require_api_token(self):
        factory = MagicMock()
        factory.return_value.execute.return_value.fetchone.return_value = {'locked': True}
        result, output = self.invoke({'DATABASE_URL':'postgresql://secret-value@localhost/db'}, factory)
        self.assertEqual(result, 0)
        factory.return_value.initialize_streams.assert_called_once()
        factory.return_value.close.assert_called_once()
