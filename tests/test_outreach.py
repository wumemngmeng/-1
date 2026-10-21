from pathlib import Path
import tempfile
import unittest

from test_notes import ROOT
from core.database import DatabaseManager, get_connection
from outreach.outreach_engine import OutreachEngine, select_profile


class OutreachTests(unittest.TestCase):
    def test_yesterday_outreach_stats_use_created_at_and_owner_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            db = DatabaseManager(str(Path(directory) / "crm.db"))
            company_a = db.insert_company({"name": "Yesterday A", "country_type": "overseas"})
            company_b = db.insert_company({"name": "Yesterday B", "country_type": "overseas"})
            db.update_company(company_a, {"owner": "Alice"})
            db.update_company(company_b, {"owner": "Bob"})
            lead_a = db.insert_lead({"company_id": company_a, "contact_name": "A"})
            lead_b = db.insert_lead({"company_id": company_b, "contact_name": "B"})
            record_a = db.record_outreach({"lead_id": lead_a, "status": "sample_sent"})
            record_b = db.record_outreach({"lead_id": lead_b, "status": "contacted"})
            conn = get_connection(str(Path(directory) / "crm.db"))
            conn.execute("UPDATE outreach_records SET created_at = ? WHERE id = ?", ("2026-10-20 17:00:00", record_a))
            conn.execute("UPDATE outreach_records SET created_at = ? WHERE id = ?", ("2026-10-20 17:00:00", record_b))
            conn.commit()
            conn.close()
            start = "2026-10-20 16:00:00"
            end = "2026-10-21 16:00:00"
            self.assertEqual(db.get_yesterday_outreach_stats(start, end), {"sample_sent": 1, "contacted": 1})
            self.assertEqual(db.get_yesterday_outreach_stats(start, end, owner="Alice"), {"sample_sent": 1})
            self.assertEqual(db.get_yesterday_outreach_stats(start, end, owner="Nobody"), {})

    def test_type_priority_and_fallback(self):
        self.assertEqual(select_profile("API / AI 应用客户", "电商")[0], "api")
        self.assertEqual(select_profile("广告 / 代运营机构", "AI")[0], "agency")
        self.assertEqual(select_profile("电商商拍客户", "AI")[0], "commerce")
        self.assertEqual(select_profile("工具→API", "")[0], "api")
        self.assertEqual(select_profile(None, "DTC Performance Agency")[0], "agency")
        self.assertEqual(select_profile(None, "未知")[0], "unknown")
        self.assertEqual(select_profile("未识别分类", "电商")[0], "unknown")

    def test_pitches_for_each_type_and_market(self):
        with tempfile.TemporaryDirectory() as directory:
            db = DatabaseManager(str(Path(directory) / "crm.db"))
            engine = OutreachEngine(db)
            for market in ("domestic", "overseas"):
                texts = []
                for kind, token in (
                    ("电商商拍客户", "SKU"),
                    ("API / AI 应用客户", "接口协议"),
                    ("广告 / 代运营机构", "brief"),
                    ("待分类", "确认客户类型"),
                ):
                    company = db.insert_company({"name": market + kind, "country_type": market})
                    db.update_company(company, {
                        "customer_type": kind, "qualification_evidence": "测试需求记录",
                        "discovery_url": "https://example.com/evidence", "models_used": "待确认模型",
                    })
                    lead = db.insert_lead({"company_id": company, "contact_name": "测试联系人"})
                    pitch = engine.generate_pitch(lead)
                    self.assertIn(token, pitch["strategy"])
                    self.assertIn("测试需求记录", pitch["research_context"])
                    self.assertIn("https://example.com/evidence", pitch["research_context"])
                    self.assertIn("待确认模型", pitch["research_context"])
                    self.assertIn("发送前", pitch["strategy"])
                    text = pitch.get("phone_script") or pitch["email_body"]
                    texts.append(text)
                    self.assertNotIn("75%", text)
                    self.assertNotIn("$3,500", text)
                    self.assertEqual(pitch.get("is_overseas", False), market == "overseas")
                self.assertEqual(len(set(texts)), 4)
            with self.assertRaises(ValueError):
                engine.generate_pitch(-1)


if __name__ == "__main__":
    unittest.main()
