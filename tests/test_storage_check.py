import io
import os
import unittest
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch
from src.store.check_storage import main, verify_storage


class StorageCheckTests(unittest.TestCase):
    def setup_connections(self):
        connect = MagicMock()
        conns = [MagicMock() for _ in range(3)]
        for conn in conns:
            conn.__enter__.return_value = conn
        connect.side_effect = conns
        conns[0].execute.side_effect = lambda query, params=None: setattr(self, 'value', params[1]) if params else MagicMock()
        conns[1].execute.return_value.fetchall.side_effect = lambda: [(1, self.value)]
        conns[2].execute.return_value.fetchone.return_value = (None,)
        return connect, conns

    def run_probe(self, connect):
        out = io.StringIO()
        with redirect_stdout(out):
            result = verify_storage(connect)
        self.assertNotIn('private-password', out.getvalue())
        return result, out.getvalue()

    def test_success_uses_separate_connections_and_cleans_up(self):
        connect, conns = self.setup_connections()
        self.assertEqual(self.run_probe(connect)[0], 0)
        self.assertEqual(connect.call_count, 3)
        conns[0].transaction.return_value.__exit__.assert_called_once()
        conns[1].execute.assert_any_call('SET TRANSACTION READ ONLY')
        statements = [call.args[0].as_string() for conn in conns for call in conn.execute.call_args_list if not isinstance(call.args[0], str)]
        self.assertTrue(any(q.startswith('DROP TABLE IF EXISTS "public"."_storage_probe_') for q in statements))
        self.assertTrue(all('CASCADE' not in q for q in statements))

    def test_read_failure_still_cleans_up(self):
        connect, conns = self.setup_connections()
        conns[1].execute.side_effect = RuntimeError('private-password')
        result, output = self.run_probe(connect)
        self.assertEqual(result, 1)
        self.assertIn('reconnect and read', output)
        self.assertEqual(connect.call_count, 3)

    def test_cleanup_failure_is_failure(self):
        connect, conns = self.setup_connections()
        conns[2].execute.side_effect = RuntimeError('private-password')
        result, output = self.run_probe(connect)
        self.assertEqual(result, 1)
        self.assertIn('Cleanup could not be confirmed', output)
        self.assertNotIn('PASS:', output)

    def test_mismatch_is_failure(self):
        connect, conns = self.setup_connections()
        conns[1].execute.return_value.fetchall.side_effect = lambda: []
        self.assertEqual(self.run_probe(connect)[0], 1)
        self.assertEqual(connect.call_count, 3)

    def test_commit_failure_still_attempts_cleanup(self):
        connect, conns = self.setup_connections()
        conns[0].transaction.return_value.__exit__.side_effect = RuntimeError('private-password')
        connect.side_effect = [conns[0], conns[2]]
        result, output = self.run_probe(connect)
        self.assertEqual(result, 1)
        self.assertIn('commit', output)
        self.assertEqual(connect.call_count, 2)

    @patch('src.store.check_storage.psycopg.connect')
    def test_bad_configuration_never_connects(self, connect):
        for url in ('', 'https://localhost', 'postgresql://localhost/db?sslmode=disable'):
            with patch.dict(os.environ, {'DATABASE_URL': url}, clear=True), redirect_stdout(io.StringIO()):
                self.assertEqual(main(), 1)
        connect.assert_not_called()

    @patch('src.store.check_storage.verify_storage')
    @patch('src.store.check_storage.psycopg.connect')
    def test_main_requires_tls(self, connect, verify):
        verify.side_effect = lambda factory: (factory(), 0)[1]
        connect.return_value.pgconn.ssl_in_use = True
        with patch.dict(os.environ, {'DATABASE_URL': 'postgresql://localhost/db?sslmode=verify-full'}, clear=True):
            self.assertEqual(main(), 0)
        self.assertEqual(connect.call_args.kwargs['sslmode'], 'verify-full')
        self.assertTrue(connect.call_args.kwargs['autocommit'])
