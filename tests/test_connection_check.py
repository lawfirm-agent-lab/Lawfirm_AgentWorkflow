import io
import os
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch
from src.store.check_connection import main


class ConnectionCheckTests(unittest.TestCase):
    def run_check(self, url, connect):
        output = io.StringIO()
        with patch.dict(os.environ, {'DATABASE_URL': url}, clear=True), redirect_stdout(output):
            result = main()
        self.assertNotIn('private-password', output.getvalue())
        self.assertNotIn('secret-host', output.getvalue())
        return result, output.getvalue()

    @patch('src.store.check_connection.psycopg.connect')
    def test_missing_and_invalid_uri(self, connect):
        for url in ('', 'https://secret-host/private-password'):
            self.assertEqual(self.run_check(url, connect)[0], 1)
        connect.assert_not_called()

    @patch('src.store.check_connection.psycopg.connect')
    def test_read_only_tls_success(self, connect):
        conn = connect.return_value.__enter__.return_value
        conn.pgconn.ssl_in_use = True
        conn.execute.return_value.fetchone.return_value = (1,)
        self.assertEqual(self.run_check('postgresql://user:private-password@secret-host/db', connect)[0], 0)
        self.assertEqual(connect.call_args.kwargs['sslmode'], 'require')
        self.assertEqual([c.args[0] for c in conn.execute.call_args_list], [
            'SET TRANSACTION READ ONLY', "SET LOCAL statement_timeout = '10s'", 'SELECT 1'])
        conn.transaction.assert_called_once()
        connect.return_value.__exit__.assert_called_once()

    @patch('src.store.check_connection.psycopg.connect')
    def test_keeps_stronger_verification(self, connect):
        conn = connect.return_value.__enter__.return_value
        conn.pgconn.ssl_in_use = True
        conn.execute.return_value.fetchone.return_value = (1,)
        self.assertEqual(self.run_check('postgresql://localhost/db?sslmode=verify-full', connect)[0], 0)
        self.assertEqual(connect.call_args.kwargs['sslmode'], 'verify-full')

    @patch('src.store.check_connection.psycopg.connect')
    def test_rejects_insecure_mode(self, connect):
        self.assertEqual(self.run_check('postgresql://localhost/db?sslmode=disable', connect)[0], 1)
        connect.assert_not_called()

    @patch('src.store.check_connection.psycopg.connect')
    def test_rejects_connection_without_tls(self, connect):
        conn = connect.return_value.__enter__.return_value
        conn.pgconn.ssl_in_use = False
        self.assertEqual(self.run_check('postgresql://localhost/db', connect)[0], 1)
        conn.execute.assert_not_called()

    @patch('src.store.check_connection.psycopg.connect')
    def test_driver_exception_is_redacted(self, connect):
        connect.side_effect = RuntimeError('private-password secret-host')
        code, output = self.run_check('postgresql://user:private-password@secret-host/db', connect)
        self.assertEqual(code, 1)
        self.assertIn('FAIL:', output)

    @patch('src.store.check_connection.psycopg.connect')
    def test_query_failure_closes_connection(self, connect):
        conn = connect.return_value.__enter__.return_value
        conn.pgconn.ssl_in_use = True
        conn.execute.side_effect = RuntimeError('private-password secret-host')
        self.assertEqual(self.run_check('postgresql://localhost/db', connect)[0], 1)
        connect.return_value.__exit__.assert_called_once()
