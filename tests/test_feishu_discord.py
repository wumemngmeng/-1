import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.database import DatabaseManager, get_connection
from core.feishu_discord import FeishuDiscordImporter, invite_identity
from core.feishu_sync import commit_statements
from core.feishu_import import FeishuMailSheetImporter
from core.email_queue import EmailQueue


class FeishuDiscordTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = DatabaseManager(str(Path(self.temp_dir.name) / "crm.db"))
        conn = get_connection(self.db.db_path)
        conn.execute("DELETE FROM discord_communities")
        conn.commit()
        conn.close()

    def tearDown(self):
        self.temp_dir.cleanup()

    @staticmethod
    def record(index):
        return {
            "sheet_row": index + 2,
            "name": f"Community {index}",
            "invite_url": f"https://discord.gg/Code{index}",
            "verification": f"核验状态-{index}",
            "description": f"完整社区说明-{index}",
            "notes": f"运营备注-{index}",
        }

    def test_imports_missing_69_with_approved_defaults_audit_and_idempotency(self):
        records = [self.record(index) for index in range(190)]
        conn = get_connection(self.db.db_path)
        for item in records[:121]:
            conn.execute(
                """INSERT INTO discord_communities
                   (name,invite_url,focus,language,fit_level,priority,join_status,account_type,notes,added_by)
                   VALUES (?,?,?,'中文','高',1,'已加入','个人号','人工运营信息','人工')""",
                (item["name"], item["invite_url"], "保留现有 focus"),
            )
        conn.commit()
        conn.close()

        importer = FeishuDiscordImporter(self.db, environ={
            "FEISHU_SPREADSHEET_TOKEN": "test-token",
        })
        with patch.object(importer, "fetch_records", return_value=records):
            preview = importer.preview()
            self.assertEqual(preview["total_rows"], 190)
            self.assertEqual(preview["create"], 69)
            self.assertEqual(preview["update_source"], 121)
            first = importer.apply("管理员", preview["fingerprint"])
            second = importer.apply("管理员")

        conn = get_connection(self.db.db_path)
        self.assertEqual(conn.execute("SELECT count(*) FROM discord_communities").fetchone()[0], 190)
        self.assertEqual(conn.execute("SELECT count(*) FROM feishu_sync_sources WHERE source_type='discord'").fetchone()[0], 190)
        self.assertEqual(conn.execute("SELECT count(*) FROM feishu_sync_runs WHERE source_type='discord'").fetchone()[0], 2)
        new_row = conn.execute("SELECT * FROM discord_communities WHERE name='Community 189'").fetchone()
        existing = conn.execute("SELECT * FROM discord_communities WHERE name='Community 0'").fetchone()
        source = conn.execute("SELECT payload FROM feishu_sync_sources WHERE source_key='Code189'").fetchone()
        conn.close()
        self.assertEqual(first["create"], 69)
        self.assertEqual(second["create"], 0)
        self.assertEqual(second["unchanged"], 190)
        self.assertEqual(new_row["language"], "英文")
        self.assertEqual(new_row["fit_level"], "中")
        self.assertEqual(new_row["join_status"], "待加入")
        self.assertEqual(new_row["account_type"], "官方号")
        self.assertIn("核验状态-189", new_row["notes"])
        self.assertIn("完整社区说明-189", json.loads(source["payload"])["description"])
        self.assertEqual(existing["language"], "中文")
        self.assertEqual(existing["join_status"], "已加入")
        self.assertEqual(existing["focus"], "保留现有 focus")
        self.assertEqual(existing["notes"], "人工运营信息")

    def test_duplicate_identity_is_case_sensitive_and_ambiguous_rows_conflict(self):
        self.assertNotEqual(invite_identity("https://discord.gg/Abc123"),
                            invite_identity("https://discord.gg/abc123"))
        records = [self.record(0), self.record(1)]
        records[1]["name"] = "Community 0"
        records[1]["invite_url"] = "https://discord.gg/Code1"
        importer = FeishuDiscordImporter(self.db, environ={})
        plan = importer.plan(records)
        self.assertEqual(plan["create"], 1)
        self.assertEqual(plan["conflict_count"], 1)

    def test_duplicate_source_row_cannot_overwrite_canonical_audit(self):
        first = self.record(0)
        duplicate = {**first, "sheet_row": 50, "notes": "duplicate row notes"}
        importer = FeishuDiscordImporter(self.db, environ={})
        with patch.object(importer, "fetch_records", return_value=[first, duplicate]):
            importer.apply("管理员")
        conn = get_connection(self.db.db_path)
        sources = {row["source_key"]: dict(row) for row in conn.execute(
            "SELECT * FROM feishu_sync_sources WHERE source_type='discord'"
        ).fetchall()}
        conn.close()
        self.assertEqual(set(sources), {"Code0", "row-50"})
        self.assertIsNotNone(sources["Code0"]["target_id"])
        self.assertEqual(json.loads(sources["Code0"]["payload"])["sheet_row"], 2)
        self.assertEqual(json.loads(sources["row-50"]["payload"])["notes"], "duplicate row notes")

    def test_mail_exact_company_match_does_not_merge_same_domain_company(self):
        first = self.db.insert_company({
            "name": "Typeface", "country_type": "overseas",
            "website": "https://www.typeface.ai",
        })
        self.db.insert_company({
            "name": "字体创意（Typeface）", "country_type": "overseas",
            "website": "https://www.typeface.ai",
        })
        importer = FeishuMailSheetImporter(self.db, environ={})
        record = {
            "sheet_row": 213, "contact_name": "Official team",
            "company_name": "Typeface（官方团队）", "job_title": "Official team",
            "email": "support@typeface.ai", "website": "https://www.typeface.ai",
            "notes": "Source details", "source": "Official website",
            "market": "pending_verification", "mail_status": "待联系",
        }
        plan = importer.plan([record])
        self.assertEqual(plan["eligible_contacts"], 0)
        self.assertEqual(plan["conflict_count"], 1)
        conn = get_connection(self.db.db_path)
        conn.execute(
            """INSERT INTO feishu_mail_company_resolutions
               (spreadsheet_token,sheet_id,email,company_id,source_name,source_domain,approved_by)
               VALUES (?,?,?,?,?,?,?)""",
            (importer.spreadsheet_token, importer.sheet_id, record["email"],
             first, "Typeface（官方团队）", "typeface.ai", "管理员"),
        )
        conn.commit()
        conn.close()
        plan = importer.plan([record])
        self.assertEqual(plan["entries"][0]["company_key"], ("id", first))
        with patch.object(importer, "fetch_records", return_value=[record]):
            first_result = importer.apply("管理员", plan["fingerprint"])
            second_result = importer.apply("管理员")
        self.assertEqual(first_result["created_contacts"], 1)
        self.assertEqual(second_result["created_contacts"], 0)
        self.assertEqual(second_result["duplicates"], 1)
        conn = get_connection(self.db.db_path)
        self.assertEqual(conn.execute("SELECT company_id FROM leads").fetchone()[0], first)
        self.assertEqual(conn.execute("SELECT count(*) FROM companies").fetchone()[0], 2)
        self.assertEqual(conn.execute("SELECT count(*) FROM feishu_sync_sources WHERE source_type='mail'").fetchone()[0], 1)
        conn.close()
    def test_pending_market_source_overrides_existing_lead_without_reclassifying_company(self):
        company_id = self.db.insert_company({
            "name": "Typeface", "country_type": "overseas",
            "website": "https://www.typeface.ai",
        })
        lead_id = self.db.insert_lead({
            "company_id": company_id, "contact_name": "Official team",
            "email": "support@typeface.ai",
        })
        importer = FeishuMailSheetImporter(self.db, environ={})
        record = {
            "sheet_row": 213, "contact_name": "Official team",
            "company_name": "Typeface（官方团队）", "job_title": "Official team",
            "email": "support@typeface.ai", "website": "https://www.typeface.ai",
            "market": "pending_verification", "mail_status": "待联系",
        }
        with patch.object(importer, "fetch_records", return_value=[record]):
            preview = importer.preview()
            self.assertEqual(preview["market_override_updates"][0]["lead_id"], lead_id)
            result = importer.apply("管理员", preview["fingerprint"])
        self.assertEqual(result["market_overrides"], 1)
        queue = EmailQueue(self.db)
        recipient = next(row for row in queue.recipients() if row["lead_id"] == lead_id)
        self.assertEqual(recipient["country_type"], "pending_verification")
        with self.assertRaisesRegex(ValueError, "待核验市场线索不可发送"):
            queue.prepare([lead_id], "管理员")
        with self.assertRaisesRegex(ValueError, "待核验市场线索不可加入推送包"):
            queue.create_package({
                "title": "Pending verification", "lead_ids": [lead_id],
                "subject": "Hi", "body": "Hello",
            }, "管理员")
        company = get_connection(self.db.db_path)
        self.assertEqual(company.execute(
            "SELECT country_type,market_classification FROM companies WHERE id=?",
            (company_id,),
        ).fetchone()["market_classification"], "verified")
        self.assertEqual(next(row for row in self.db.query_pipeline()
                             if row["lead_id"] == lead_id)["country_type"], "pending_verification")
        company.close()


    def test_shared_remote_commit_uses_single_batch(self):
        class FakeRemote:
            def __init__(self):
                self.client = type("Client", (), {"batch": lambda _, statements: calls.append(statements)})()
        calls = []
        with patch("core.feishu_sync.RemoteConnection", FakeRemote):
            commit_statements(FakeRemote(), [("INSERT INTO t VALUES (?)", (1,)),
                                             ("INSERT INTO t VALUES (?)", (2,))])
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(calls[0]), 2)

    def test_feishu_sheet_reader_pages_physical_rows(self):
        values = [
            [["公司", "邮箱"], ["A", "a@example.com"], []],
            [["B", "b@example.com"]],
        ]
        responses = [
            {"code": 0, "data": {"sheets": [{"sheet_id": "S", "grid_properties": {"row_count": 501}}]}},
            {"code": 0, "data": {"valueRange": {"values": values[0]}}},
            {"code": 0, "data": {"valueRange": {"values": values[1]}}},
        ]

        class Response:
            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self):
                return json.dumps(self.payload).encode()

        def opener(*_, **__):
            return Response(responses.pop(0))

        importer = FeishuMailSheetImporter(self.db, environ={}, opener=opener)
        rows = importer.read_sheet_values("token", "S", "N")
        self.assertEqual(len(rows), 501)
        self.assertEqual(rows[1][0], "A")
        self.assertEqual(rows[500][0], "B")


if __name__ == "__main__":
    unittest.main()
