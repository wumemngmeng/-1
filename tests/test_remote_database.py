import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from core.database import RemoteConnection, RemoteCursor, SCHEMA_SQL, bootstrap_remote_from_sqlite


class RemoteDatabaseTests(unittest.TestCase):
    def test_remote_rows_support_dictionary_conversion_and_cursor_consumption(self):
        client = Mock()
        client.execute.return_value = types.SimpleNamespace(
            columns=("id", "name"), rows=[(1, "测试企业"), (2, None)],
            last_insert_rowid=2,
        )
        cursor = RemoteCursor(client).execute("SELECT id, name FROM companies")
        self.assertEqual(dict(cursor.fetchone()), {"id": 1, "name": "测试企业"})
        self.assertEqual(cursor.fetchall(), [{"id": 2, "name": None}])
        self.assertIsNone(cursor.fetchone())
        self.assertEqual(cursor.lastrowid, 2)

    def test_remote_schema_and_bulk_parameters(self):
        client = Mock()
        client.batch.return_value = [types.SimpleNamespace(last_insert_rowid=3)]
        cursor = RemoteCursor(client)
        cursor.executescript(SCHEMA_SQL)
        statements = client.batch.call_args.args[0]
        self.assertTrue(any("CREATE TABLE IF NOT EXISTS companies" in s for s in statements))
        cursor.executemany("INSERT INTO test VALUES (?, ?)", [(3, None)])
        client.batch.assert_called_with([("INSERT INTO test VALUES (?, ?)", (3, None))])
        self.assertEqual(cursor.lastrowid, 3)

    def test_remote_connection_uses_https(self):
        module = types.ModuleType("libsql_client")
        create_client = unittest.mock.Mock()
        module.create_client_sync = create_client
        with patch.dict(sys.modules, {"libsql_client": module}):
            connection = RemoteConnection("libsql://example.turso.io", "secret")
            create_client.assert_called_once_with(
                "https://example.turso.io", auth_token="secret"
            )
            connection.close()

    def test_bootstrap_remote_database_once(self):
        with tempfile.TemporaryDirectory() as directory:
            source_path = Path(directory) / "source.db"
            target_path = Path(directory) / "target.db"
            source = sqlite3.connect(source_path)
            source.executescript(SCHEMA_SQL)
            source.execute(
                "INSERT INTO companies "
                "(id, name, country_type, industry, source_channel) "
                "VALUES (1, '测试企业', 'domestic', '电商', '测试来源')"
            )
            source.execute(
                "INSERT INTO leads (id, company_id, contact_name, job_title) "
                "VALUES (1, 1, '测试负责人', '负责人')"
            )
            source.commit()
            source.close()

            target = sqlite3.connect(target_path)
            target.row_factory = sqlite3.Row
            target.executescript(SCHEMA_SQL)
            with patch.dict(os.environ, {"TURSO_DATABASE_URL": "libsql://test"}):
                remote = RemoteConnection.__new__(RemoteConnection)
                remote.client = Mock()
                remote.client.execute.return_value = types.SimpleNamespace(
                    columns=("count",), rows=[(0,)], last_insert_rowid=None
                )
                bootstrap_remote_from_sqlite(remote, str(source_path))
                remote.client.batch.assert_called_once()
                inserts = remote.client.batch.call_args.args[0]
                self.assertEqual(len(inserts), 2)
                self.assertIn("INTO companies", inserts[0][0])
                self.assertIn("INTO leads", inserts[1][0])
                bootstrap_remote_from_sqlite(target, str(source_path))
                bootstrap_remote_from_sqlite(target, str(source_path))
            self.assertEqual(
                target.execute("SELECT count(*) FROM companies").fetchone()[0], 1
            )
            self.assertEqual(
                target.execute("SELECT count(*) FROM leads").fetchone()[0], 1
            )
            target.close()


if __name__ == "__main__":
    unittest.main()
