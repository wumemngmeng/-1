import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.database import DatabaseManager, get_connection
from core.feishu_import import (
    FeishuImportError,
    FeishuMailSheetImporter,
    normalize_company_name,
    normalize_domain,
    parse_sheet_rows,
)


class FeishuImportTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = DatabaseManager(str(Path(self.temp_dir.name) / "crm.db"))

    def tearDown(self):
        self.temp_dir.cleanup()

    @staticmethod
    def record(**values):
        result = {
            "sheet_row": 2,
            "contact_name": "Alex",
            "company_name": "Acme Inc.",
            "job_title": "Founder",
            "email": "alex@acme.example",
            "x_url": "https://x.com/alex",
            "website": "https://www.acme.example/about",
            "notes": "Public business contact",
            "source": "Company website",
            "market": "overseas",
            "mail_status": "待联系",
        }
        result.update(values)
        return result

    def test_normalizes_company_names_and_domains(self):
        self.assertEqual(normalize_company_name("Ａcme, Inc."), "acmeinc")
        self.assertEqual(normalize_domain("https://www.Acme.example/path"), "acme.example")

    def test_parses_sheet_headers_and_preserves_market_as_pending(self):
        rows = parse_sheet_rows([
            ["姓名", "公司", "职位", "邮箱", "X链接", "官网", "找客理由", "邮箱来源", "状态", "目标市场"],
            ["Jamie", "Northwind", "Founder", "JAMIE@EXAMPLE.COM", "", "https://northwind.example",
             "Public source", "Website", "待联系", "待核验"],
        ])
        self.assertEqual(rows[0]["email"], "jamie@example.com")
        self.assertEqual(rows[0]["market"], "pending_verification")

    def test_plan_matches_company_by_name_or_domain_and_flags_duplicate_email(self):
        existing_id = self.db.insert_company({
            "name": "Acme Incorporated", "country_type": "overseas",
            "website": "https://acme.example", "industry": "AI tools",
            "notes": "Existing CRM notes",
        })
        importer = FeishuMailSheetImporter(self.db, environ={})
        records = [
            self.record(company_name="Acme, Incorporated", email="new@acme.example"),
            self.record(sheet_row=3, company_name="Different Brand Name", email="new2@acme.example"),
            self.record(sheet_row=4, company_name="Acme Incorporated", email="new@acme.example"),
        ]
        plan = importer.plan(records)
        self.assertEqual(plan["eligible_contacts"], 2)
        self.assertEqual(plan["new_companies"], 0)
        self.assertEqual(plan["matched_companies"], 1)
        self.assertEqual(plan["conflict_count"], 1)
        self.assertEqual(plan["entries"][0]["company_key"], ("id", existing_id))
        self.assertIn("重复邮箱", plan["conflicts"][0]["reason"])

    def test_multiple_existing_companies_matching_one_domain_are_flagged(self):
        for name in ("Acme One", "Acme Two"):
            self.db.insert_company({
                "name": name, "country_type": "overseas",
                "website": "https://same.example",
            })
        plan = FeishuMailSheetImporter(self.db, environ={}).plan([
            self.record(website="https://same.example")
        ])
        self.assertEqual(plan["eligible_contacts"], 0)
        self.assertIn("命中多个CRM公司", plan["conflicts"][0]["reason"])

    def test_apply_creates_pending_company_and_preserves_already_pushed_status(self):
        importer = FeishuMailSheetImporter(self.db, environ={})
        with self.assertRaises(FeishuImportError):
            importer.apply("管理员")

    def test_apply_is_idempotent_and_imports_market_and_sent_suppression(self):
        records = [
            self.record(mail_status="已推送"),
            self.record(sheet_row=3, contact_name="Morgan", email="morgan@acme.example"),
        ]
        importer = FeishuMailSheetImporter(self.db, environ={})
        with patch.object(importer, "fetch_records", return_value=records):
            first = importer.apply("管理员")
            second = importer.apply("管理员")
        self.assertEqual(first["created_companies"], 1)
        self.assertEqual(first["created_contacts"], 2)
        self.assertEqual(first["suppressed_existing_statuses"], 1)
        self.assertEqual(second["created_contacts"], 0)
        conn = get_connection(self.db.db_path)
        company = conn.execute("SELECT * FROM companies WHERE name='Acme Inc.'").fetchone()
        leads = conn.execute("SELECT count(*) AS count FROM leads").fetchone()["count"]
        queue = conn.execute("SELECT status FROM mail_queue").fetchone()
        conn.close()
        self.assertEqual(company["market_classification"], "verified")
        self.assertEqual(company["country_type"], "overseas")
        self.assertEqual(leads, 2)
        self.assertEqual(queue["status"], "sent")

    def test_apply_fills_only_blank_company_fields_and_rejects_stale_preview(self):
        company_id = self.db.insert_company({
            "name": "Acme Incorporated", "country_type": "overseas",
            "website": "", "notes": "Keep these existing notes",
        })
        importer = FeishuMailSheetImporter(self.db, environ={})
        source = [self.record(company_name="Acme, Incorporated")]
        expected = importer.plan(source)["fingerprint"]
        with patch.object(importer, "fetch_records", return_value=source):
            result = importer.apply("管理员", expected_fingerprint=expected)
        self.assertEqual(result["updated_companies"], 1)
        conn = get_connection(self.db.db_path)
        company = conn.execute("SELECT website,notes FROM companies WHERE id=?", (company_id,)).fetchone()
        conn.close()
        self.assertEqual(company["website"], "https://www.acme.example/about")
        self.assertEqual(company["notes"], "Keep these existing notes")

        changed = [self.record(email="changed@acme.example")]
        with patch.object(importer, "fetch_records", return_value=changed):
            with self.assertRaisesRegex(FeishuImportError, "发生变化"):
                importer.apply("管理员", expected_fingerprint=expected)
        conn = get_connection(self.db.db_path)
        count = conn.execute("SELECT count(*) FROM leads").fetchone()[0]
        conn.close()
        self.assertEqual(count, 1)

    def test_apply_keeps_unknown_market_out_of_sendable_overseas(self):
        records = [self.record(market="pending_verification", mail_status="待联系")]
        importer = FeishuMailSheetImporter(self.db, environ={})
        with patch.object(importer, "fetch_records", return_value=records):
            importer.apply("管理员")
        conn = get_connection(self.db.db_path)
        company = conn.execute("SELECT country_type,market_classification FROM companies").fetchone()
        conn.close()
        self.assertEqual(company["country_type"], "overseas")
        self.assertEqual(company["market_classification"], "pending_verification")

    def test_status_sync_updates_existing_queue_without_creating_send_history(self):
        importer = FeishuMailSheetImporter(self.db, environ={})
        source = [self.record(mail_status="待联系")]
        with patch.object(importer, "fetch_records", return_value=source):
            importer.apply("管理员")
        conn = get_connection(self.db.db_path)
        lead_id = conn.execute("SELECT id FROM leads").fetchone()["id"]
        conn.execute(
            """INSERT INTO mail_queue(company_id,lead_id,email,status,created_by)
               SELECT company_id,id,email,'pending','管理员' FROM leads WHERE id=?""",
            (lead_id,),
        )
        conn.commit()
        conn.close()

        changed = [self.record(mail_status="已推送")]
        with patch.object(importer, "fetch_records", return_value=changed):
            result = importer.apply("Vercel Feishu Sync")
        conn = get_connection(self.db.db_path)
        queue = conn.execute("SELECT status FROM mail_queue").fetchone()
        sends = conn.execute("SELECT count(*) AS count FROM mail_queue_sends").fetchone()["count"]
        conn.close()
        self.assertEqual(result["updated_sent_statuses"], 1)
        self.assertEqual(queue["status"], "sent")
        self.assertEqual(sends, 0)

    def test_status_sync_never_downgrades_backend_sent_to_feishu_pending(self):
        importer = FeishuMailSheetImporter(self.db, environ={})
        source = [self.record(mail_status="已推送")]
        with patch.object(importer, "fetch_records", return_value=source):
            importer.apply("管理员")
        changed = [self.record(mail_status="待联系")]
        with patch.object(importer, "fetch_records", return_value=changed):
            result = importer.apply("Vercel Feishu Sync")
        conn = get_connection(self.db.db_path)
        queue = conn.execute("SELECT status FROM mail_queue").fetchone()
        conn.close()
        self.assertEqual(queue["status"], "sent")
        self.assertGreaterEqual(result["preserved_statuses"], 1)

    def test_existing_contact_status_also_updates_pipeline_idempotently(self):
        importer = FeishuMailSheetImporter(self.db, environ={})
        source = [self.record()]
        with patch.object(importer, "fetch_records", return_value=source):
            importer.apply("管理员")
        source[0]["mail_status"] = "已推送"
        with patch.object(importer, "fetch_records", return_value=source):
            preview = importer.preview()
            first = importer.apply("管理员", preview["fingerprint"])
            second = importer.apply("管理员")
        self.assertEqual(preview["eligible_contacts"], 0)
        self.assertEqual(preview["status_sync"]["create_sent"], 1)
        self.assertEqual(preview["status_sync"]["outreach_updates"], 1)
        self.assertEqual(first["synced_outreach_records"], 1)
        self.assertEqual(second["synced_outreach_records"], 0)
        self.assertEqual(self.db.query_pipeline()[0]["status"], "contacted")

    def test_protected_queue_and_advanced_pipeline_states_are_preserved(self):
        importer = FeishuMailSheetImporter(self.db, environ={})
        for index, status in enumerate(("sending", "unsubscribed", "removed"), 1):
            source = [self.record(email=f"person{index}@acme.example")]
            with patch.object(importer, "fetch_records", return_value=source):
                importer.apply("管理员")
            conn = get_connection(self.db.db_path)
            lead = conn.execute("SELECT id,company_id FROM leads WHERE email=?", (source[0]["email"],)).fetchone()
            conn.execute(
                "INSERT INTO mail_queue(company_id,lead_id,email,status,created_by) VALUES(?,?,?,?,?)",
                (lead["company_id"], lead["id"], source[0]["email"], status, "管理员"),
            )
            conn.commit()
            conn.close()
            self.db.record_outreach({"lead_id": lead["id"], "channel": "cold_email", "status": "negotiation"})
            source[0]["mail_status"] = "已推送"
            with patch.object(importer, "fetch_records", return_value=source):
                result = importer.apply("管理员")
            conn = get_connection(self.db.db_path)
            self.assertEqual(conn.execute("SELECT status FROM mail_queue WHERE lead_id=?", (lead["id"],)).fetchone()["status"], status)
            self.assertEqual(conn.execute("SELECT status FROM outreach_records WHERE lead_id=? ORDER BY id DESC LIMIT 1", (lead["id"],)).fetchone()["status"], "negotiation")
            conn.close()
            self.assertEqual(result["synced_outreach_records"], 0)

    def test_unsubscribe_overrides_sent_without_removing_send_history(self):
        importer = FeishuMailSheetImporter(self.db, environ={})
        source = [self.record(mail_status="已推送")]
        with patch.object(importer, "fetch_records", return_value=source):
            importer.apply("管理员")
        source[0]["mail_status"] = "已退订"
        with patch.object(importer, "fetch_records", return_value=source):
            result = importer.apply("管理员")
        conn = get_connection(self.db.db_path)
        self.assertEqual(conn.execute("SELECT status FROM mail_queue").fetchone()["status"], "unsubscribed")
        conn.close()
        self.assertEqual(result["updated_unsubscribed_statuses"], 1)

    def test_company_mismatch_and_status_only_stale_previews_fail_safe(self):
        importer = FeishuMailSheetImporter(self.db, environ={})
        source = [self.record()]
        with patch.object(importer, "fetch_records", return_value=source):
            importer.apply("管理员")
            fingerprint = importer.preview()["fingerprint"]
        source[0]["mail_status"] = "已推送"
        with patch.object(importer, "fetch_records", return_value=source):
            with self.assertRaisesRegex(FeishuImportError, "发生变化"):
                importer.apply("管理员", fingerprint)
        source[0].update(company_name="Unrelated Company", website="https://unrelated.example")
        with patch.object(importer, "fetch_records", return_value=source):
            result = importer.apply("管理员")
        conn = get_connection(self.db.db_path)
        self.assertEqual(conn.execute("SELECT count(*) FROM mail_queue").fetchone()[0], 0)
        conn.close()
        self.assertEqual(result["status_conflicts"], 1)

    def test_unconfigured_feishu_credentials_fail_closed(self):
        importer = FeishuMailSheetImporter(self.db, environ={})
        with self.assertRaises(FeishuImportError):
            importer.fetch_records()

    def test_feishu_client_uses_app_auth_then_reads_sheet_without_exposing_secret(self):
        responses = [
            {"code": 0, "tenant_access_token": "test-token"},
            {"code": 0, "data": {"valueRange": {"values": [
                ["姓名", "公司", "邮箱", "目标市场"],
                ["Jamie", "Northwind", "jamie@northwind.example", "海外"],
            ]}}},
        ]
        requests = []

        class Response:
            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self):
                return json.dumps(self.payload).encode()

        def opener(request, timeout):
            requests.append(request)
            return Response(responses.pop(0))

        importer = FeishuMailSheetImporter(
            self.db,
            environ={"FEISHU_APP_ID": "app-id", "FEISHU_APP_SECRET": "hidden"},
            opener=opener,
        )
        rows = importer.fetch_records()
        self.assertEqual(rows[0]["email"], "jamie@northwind.example")
        self.assertIn("/sheets/v2/spreadsheets/", requests[1].full_url)
        self.assertEqual(requests[1].get_header("Authorization"), "Bearer test-token")


if __name__ == "__main__":
    unittest.main()
