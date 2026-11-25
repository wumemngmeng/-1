import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.database import DatabaseManager, get_connection
from core.feishu_import import (
    FeishuImportError,
    FeishuMailSheetImporter,
    FeishuXStatusSync,
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
            {"code": 0, "data": {"sheets": [
                {"sheet_id": "U8RTY1", "grid_properties": {"row_count": 2}},
            ]}},
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
        self.assertIn("/sheets/v2/spreadsheets/", requests[2].full_url)
        self.assertEqual(requests[2].get_header("Authorization"), "Bearer test-token")

    def make_x_lead(self, name="Acme", handle="alex"):
        company_id = self.db.insert_company({
            "name": name, "country_type": "overseas",
            "website": f"https://{handle}.example",
        })
        lead_id = self.db.insert_lead({
            "company_id": company_id, "contact_name": "Alex",
            "job_title": "Founder", "x_url": f"https://x.com/{handle}",
        })
        return company_id, lead_id

    @staticmethod
    def x_record(**values):
        record = {
            "sheet_row": 2, "contact_name": "Alex", "company_name": "Acme",
            "job_title": "Founder", "x_url": "https://x.com/alex",
            "website": "https://alex.example", "x_account": "alex",
            "source_status": "已推送未回复", "source_date": "2026-11-02",
        }
        record.update(values)
        return record

    def test_x_sync_maps_statuses_to_x_dm_and_is_idempotent_without_sending(self):
        self.make_x_lead()
        sync = FeishuXStatusSync(self.db, environ={})
        records = [self.x_record()]
        with patch.object(sync, "fetch_records", return_value=records):
            first = sync.apply()
            second = sync.apply()
        conn = get_connection(self.db.db_path)
        outreach = conn.execute(
            "SELECT * FROM outreach_records WHERE channel='x_dm'"
        ).fetchone()
        queue_count = conn.execute("SELECT count(*) FROM mail_queue").fetchone()[0]
        conn.close()
        self.assertEqual(first["created_outreach_records"], 1)
        self.assertEqual(first["create_sample_sent"], 1)
        self.assertEqual(second["created_outreach_records"], 0)
        self.assertEqual(second["unchanged"], 1)
        self.assertEqual(outreach["status"], "sample_sent")
        self.assertIn("2026-11-02", outreach["interaction_notes"])
        self.assertIn("未触发任何私信或邮件发送", outreach["interaction_notes"])
        self.assertEqual(queue_count, 0)
        conn = get_connection(self.db.db_path)
        run_rows = conn.execute(
            "SELECT actor,result FROM feishu_sync_runs WHERE source_type='x' ORDER BY rowid"
        ).fetchall()
        conn.close()
        self.assertEqual(len(run_rows), 2)
        self.assertEqual(run_rows[0]["actor"], "Vercel Feishu Sync")
        self.assertEqual(json.loads(run_rows[0]["result"])["created_outreach_records"], 1)

    def test_x_sync_replies_advance_but_never_downgrade_advanced_stages(self):
        _, lead_id = self.make_x_lead()
        sync = FeishuXStatusSync(self.db, environ={})
        replied = [self.x_record(source_status="已回复")]
        with patch.object(sync, "fetch_records", return_value=replied):
            result = sync.apply()
        self.assertEqual(result["create_contacted"], 1)
        self.db.record_outreach({
            "lead_id": lead_id, "channel": "x_dm", "status": "negotiation",
        })
        with patch.object(sync, "fetch_records", return_value=[
            self.x_record(source_status="已推送未回复")
        ]):
            result = sync.apply()
        self.assertEqual(result["preserved"], 1)
        self.assertEqual(result["created_outreach_records"], 0)

    def test_x_sync_explicit_suppression_is_lost_and_conflicts_are_reported(self):
        self.make_x_lead()
        sync = FeishuXStatusSync(self.db, environ={})
        with patch.object(sync, "fetch_records", return_value=[
            self.x_record(source_status="无法触达(私信关闭)")
        ]):
            result = sync.apply()
        conn = get_connection(self.db.db_path)
        self.assertEqual(conn.execute(
            "SELECT status FROM outreach_records WHERE channel='x_dm'"
        ).fetchone()["status"], "lost")
        conn.close()
        self.assertEqual(result["create_lost"], 1)

        conflicts = sync.plan([
            self.x_record(source_status="待处理", sheet_row=4),
            self.x_record(source_status="已回复", sheet_row=5),
            self.x_record(x_url="https://x.com/unknown", x_account="unknown",
                          source_status="ByCrux", sheet_row=6),
        ])
        self.assertEqual(conflicts["conflict_count"], 1)
        self.assertEqual(conflicts["malformed"], 1)
        self.assertTrue(any("多个状态" in item["reason"]
                            for item in conflicts["conflicts"]))
        self.assertTrue(any("未知 X 推送状态" in item["reason"]
                            for item in conflicts["conflicts"]))

    def test_x_sync_preserves_other_channel_advanced_stage_and_detects_company_mismatch(self):
        _, lead_id = self.make_x_lead(name="Unrelated Company")
        self.db.record_outreach({
            "lead_id": lead_id, "channel": "cold_email", "status": "won",
        })
        sync = FeishuXStatusSync(self.db, environ={})
        with patch.object(sync, "fetch_records", return_value=[self.x_record()]):
            result = sync.apply()
        self.assertEqual(result["preserved"], 1)
        self.assertEqual(result["created_outreach_records"], 0)

        self.make_x_lead(name="Another", handle="other")
        bad = self.x_record(company_name="Wrong Company", x_url="https://x.com/other",
                            x_account="other", website="https://wrong.example")
        result = sync.plan([bad])
        self.assertEqual(result["conflict_count"], 1)
        self.assertIn("公司名/官网域名不一致", result["conflicts"][0]["reason"])

    def test_x_raw_non_touch_states_and_dates_are_preserved_without_fake_outreach(self):
        self.make_x_lead()
        sync = FeishuXStatusSync(self.db, environ={})
        records = [self.x_record(source_status="已起草")]
        with patch.object(sync, "fetch_records", return_value=records):
            preview = sync.preview()
            result = sync.apply(expected_fingerprint=preview["fingerprint"])
            again = sync.apply()
        self.assertEqual(result["source_updates"], 1)
        self.assertEqual(result["created_outreach_records"], 0)
        self.assertEqual(again["source_updates"], 0)
        conn = get_connection(self.db.db_path)
        state = conn.execute("SELECT * FROM feishu_x_states").fetchone()
        self.assertEqual(state["source_status"], "已起草")
        self.assertEqual(state["source_date"], "2026-11-02")
        self.assertEqual(conn.execute("SELECT count(*) FROM outreach_records").fetchone()[0], 0)
        conn.close()
        records[0]["source_status"] = "已回复"
        with patch.object(sync, "fetch_records", return_value=records):
            with self.assertRaisesRegex(FeishuImportError, "发生变化"):
                sync.apply(expected_fingerprint=preview["fingerprint"])

    def test_x_duplicate_crm_identity_mismatch_and_unknown_handle_fail_closed(self):
        company_id, _ = self.make_x_lead()
        self.db.insert_lead({
            "company_id": company_id, "contact_name": "Other",
            "x_url": "https://twitter.com/ALEX",
        })
        sync = FeishuXStatusSync(self.db, environ={})
        result = sync.plan([self.x_record(contact_name="")])
        self.assertEqual(result["conflict_count"], 1)
        self.assertEqual(result["actions"], [])
        result = sync.plan([self.x_record(x_account="someoneelse")])
        self.assertEqual(result["malformed"], 1)
        self.assertEqual(sync.normalize_x_identity("https://x.com/home"), "")
        self.assertEqual(sync.normalize_x_identity("https://x.com/alex/status/123"), "alex")
        self.assertEqual(sync.normalize_x_identity("twitter.com/AlEx?lang=en"), "alex")

    def test_x_status_changes_append_history_instead_of_overwriting_notes(self):
        _, lead_id = self.make_x_lead()
        self.db.record_outreach({
            "lead_id": lead_id, "channel": "x_dm", "status": "sample_sent",
            "interaction_notes": "Original sales note",
        })
        sync = FeishuXStatusSync(self.db, environ={})
        with patch.object(sync, "fetch_records", return_value=[
            self.x_record(source_status="已回复")
        ]):
            sync.apply()
        conn = get_connection(self.db.db_path)
        rows = conn.execute("SELECT * FROM outreach_records ORDER BY id").fetchall()
        conn.close()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["interaction_notes"], "Original sales note")
        self.assertEqual(rows[-1]["status"], "contacted")

    def test_x_sync_imports_identifiable_missing_contact_and_archives_all_source_rows(self):
        self.db.insert_company({
            "name": "Northwind Studio", "country_type": "overseas",
            "website": "https://northwind.example",
        })
        sync = FeishuXStatusSync(self.db, environ={})
        records = [
            self.x_record(
                company_name="Northwind Studio", contact_name="Casey",
                x_url="https://x.com/casey_northwind", x_account="casey_northwind",
                website="https://northwind.example",
                source_status="已回复", sheet_row=10,
            ),
            self.x_record(
                company_name="Anonymous DTC brand", contact_name="Casey",
                x_url="https://x.com/unknownbrand", x_account="unknownbrand",
                source_status="已推送未回复", sheet_row=11,
            ),
        ]
        with patch.object(sync, "fetch_records", return_value=records):
            plan = sync.preview()
            first = sync.apply(expected_fingerprint=plan["fingerprint"])
            second = sync.apply()
        conn = get_connection(self.db.db_path)
        lead = conn.execute(
            "SELECT * FROM leads WHERE x_url='https://x.com/casey_northwind'"
        ).fetchone()
        outreach = conn.execute(
            "SELECT * FROM outreach_records WHERE lead_id=? AND channel='x_dm'",
            (lead["id"],),
        ).fetchone()
        archived = conn.execute(
            "SELECT count(*) FROM feishu_x_source_rows"
        ).fetchone()[0]
        mail_queue = conn.execute("SELECT count(*) FROM mail_queue").fetchone()[0]
        conn.close()
        self.assertEqual(plan["importable_rows"], 1)
        self.assertEqual(first["created_contacts"], 1)
        self.assertEqual(first["created_outreach_records"], 1)
        self.assertEqual(first["archived_source_rows"], 2)
        self.assertEqual(second["created_contacts"], 0)
        self.assertEqual(second["created_outreach_records"], 0)
        self.assertEqual(second["archived_source_rows"], 2)
        self.assertEqual(lead["verification_status"], "pending_review")
        self.assertEqual(outreach["status"], "contacted")
        self.assertEqual(archived, 2)
        self.assertEqual(mail_queue, 0)

    def test_linkedin_channel_status_is_not_written_as_x_dm(self):
        company_id = self.db.insert_company({
            "name": "Linkedin Co", "country_type": "overseas",
            "website": "https://linkedinco.example",
        })
        self.db.insert_lead({
            "company_id": company_id, "contact_name": "Sam",
            "linkedin_url": "https://www.linkedin.com/in/sam-linkedin",
        })
        sync = FeishuXStatusSync(self.db, environ={})
        record = self.x_record(
            contact_name="Sam", company_name="Linkedin Co",
            x_url="https://www.linkedin.com/in/sam-linkedin",
            x_account="linkedin.com/in/sam-linkedin", channel="LinkedIn",
            website="https://linkedinco.example", source_status="已回复",
        )
        with patch.object(sync, "fetch_records", return_value=[record]):
            result = sync.apply()
        conn = get_connection(self.db.db_path)
        row = conn.execute("SELECT channel,status FROM outreach_records").fetchone()
        conn.close()
        self.assertEqual(result["create_contacted"], 1)
        self.assertEqual(row["channel"], "linkedin_inmail")
        self.assertEqual(row["status"], "contacted")


if __name__ == "__main__":
    unittest.main()
