from pathlib import Path
import csv
import tempfile
import unittest

from core.database import DatabaseManager, normalize_lead_evidence
from enrichers.lead_enricher import LeadEnricher


class LeadVerificationTests(unittest.TestCase):
    def verified_contact(self):
        return {
            "contact_name": "真实创始人",
            "job_title": "创始人兼 CEO",
            "identity_source_url": "https://example.com/about",
            "identity_evidence": "真实创始人于 2020 年创立公司。",
            "phone": "13800000000",
            "phone_source_url": "https://example.com/contact",
            "phone_evidence": "商务合作：13800000000",
            "phone_confidence": "A",
            "wechat": "founder_business",
            "wechat_source_url": "https://example.com/contact",
            "wechat_evidence": "商务微信：founder_business",
            "wechat_confidence": "A",
            "verified_at": "2026-10-29",
            "fit_score": 85,
        }

    def test_sales_ready_is_derived_from_independent_evidence(self):
        result = normalize_lead_evidence(self.verified_contact())
        self.assertEqual(result["verification_status"], "sales_ready")

    def test_phone_cannot_be_assumed_to_be_wechat(self):
        data = self.verified_contact()
        data["wechat"] = data["phone"]
        with self.assertRaisesRegex(ValueError, "不能自动视为微信号"):
            normalize_lead_evidence(data)
        data["wechat_same_as_phone_confirmed"] = True
        self.assertEqual(
            normalize_lead_evidence(data)["verification_status"], "sales_ready"
        )

    def test_domestic_contact_requires_field_level_provenance(self):
        data = self.verified_contact()
        data["phone_source_url"] = ""
        with self.assertRaisesRegex(ValueError, "手机号必须同时填写"):
            normalize_lead_evidence(data)

    def test_database_round_trip_includes_verification_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            db = DatabaseManager(str(Path(directory) / "crm.db"))
            company_id = db.insert_company({
                "name": "核验测试企业", "country_type": "domestic",
            })
            lead = self.verified_contact()
            lead["company_id"] = company_id
            db.insert_lead(lead)
            row = db.query_pipeline(country_type="domestic")[0]
            self.assertEqual(row["verification_status"], "sales_ready")
            self.assertEqual(row["phone_source_url"], "https://example.com/contact")
            self.assertEqual(row["wechat_confidence"], "A")

    def test_csv_import_does_not_copy_phone_into_wechat(self):
        with tempfile.TemporaryDirectory() as directory:
            db = DatabaseManager(str(Path(directory) / "crm.db"))
            db.insert_company({"name": "CSV 测试企业", "country_type": "domestic"})
            path = Path(directory) / "leads.csv"
            record = self.verified_contact()
            headers = {
                "公司名称": "CSV 测试企业", "姓名": record["contact_name"],
                "职位": record["job_title"], "手机号": record["phone"],
                "身份来源URL": record["identity_source_url"],
                "身份原文": record["identity_evidence"],
                "手机号来源URL": record["phone_source_url"],
                "手机号证据原文": record["phone_evidence"],
                "手机号可信等级": "A", "适配评分": "80",
            }
            with path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=headers)
                writer.writeheader()
                writer.writerow(headers)
            LeadEnricher(db).enrich_from_csv(str(path))
            row = db.query_pipeline(country_type="domestic")[0]
            self.assertEqual(row["wechat"], "")
            self.assertEqual(row["verification_status"], "contact_partial")

    def test_synthetic_seed_is_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            db = DatabaseManager(str(Path(directory) / "crm.db"))
            with self.assertRaisesRegex(RuntimeError, "已禁用"):
                LeadEnricher(db).seed_verified_leads()


if __name__ == "__main__":
    unittest.main()
