import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from core.database import SCHEMA_SQL, bootstrap_remote_from_sqlite


class RemoteDatabaseTests(unittest.TestCase):
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
