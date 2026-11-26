import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet

from core.database import DatabaseManager, get_connection
from core.email_queue import EmailQueue


class EmailQueueTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db = DatabaseManager(str(Path(self.directory.name) / "crm.db"))
        self.queue = EmailQueue(self.db)
        self.company = self.db.insert_company({"name": "Queue Test", "country_type": "overseas"})
        self.db.update_company(self.company, {"owner": "Rep"})
        self.lead = self.db.insert_lead({
            "company_id": self.company, "contact_name": "Alex", "email": "alex@example.com"
        })

    def test_encrypted_configuration_and_blank_key_preservation(self):
        key = Fernet.generate_key().decode()
        with patch.dict(os.environ, {"MAIL_CONFIG_KEY": key}, clear=False):
            public = self.queue.save_settings({
                "from_email": "sales@example.com", "from_name": "Sales",
                "reply_to": "reply@example.com", "api_key": "secret-test-key",
                "batch_limit": 5,
            })
            self.assertTrue(public["configured"])
            self.assertNotIn("secret-test-key", json.dumps(public))
            conn = get_connection(self.db.db_path)
            stored = conn.execute("SELECT * FROM mail_settings").fetchone()
            self.assertNotIn("secret-test-key", str(dict(stored)))
            conn.close()
            self.queue.save_settings({"from_email": "new@example.com", "batch_limit": 10})
            self.assertEqual(self.queue.settings(private=True)["api_key"], "secret-test-key")
            with patch.dict(os.environ, {"MAIL_CONFIG_KEY": Fernet.generate_key().decode()}):
                with self.assertRaisesRegex(ValueError, "无法解密"):
                    self.queue.settings(private=True)

    def test_key_requires_server_master_key(self):
        with patch.dict(os.environ, {"MAIL_CONFIG_KEY": ""}):
            with self.assertRaisesRegex(ValueError, "部署环境"):
                self.queue.save_settings({"from_email": "sales@example.com", "api_key": "test"})

    def test_multiple_senders_keep_separate_encrypted_keys(self):
        with patch.dict(os.environ, {"MAIL_CONFIG_KEY": Fernet.generate_key().decode()}):
            self.queue.save_settings({"from_email": "one@example.com", "api_key": "first-secret"})
            second = self.queue.save_settings({
                "from_email": "two@other.com", "api_key": "second-secret"
            }, sender_id=None)
            self.assertGreater(second["id"], 1)
            self.queue.save_settings({"from_email": "renamed@other.com"}, sender_id=second["id"])
            self.assertEqual(self.queue.settings(True)["api_key"], "first-secret")
            self.assertEqual(self.queue.settings(True, second["id"])["api_key"], "second-secret")
            self.assertNotIn("secret", json.dumps(self.queue.senders()))
            self.assertEqual(len(self.queue.senders()), 2)
            with self.assertRaisesRegex(ValueError, "不存在"):
                self.queue.settings(sender_id=999)

    def test_smtp_configuration_and_automation(self):
        with patch.dict(os.environ, {"MAIL_CONFIG_KEY": Fernet.generate_key().decode()}):
            public = self.queue.save_settings({
                "provider": "smtp", "from_email": "sales@example.com",
                "smtp_host": "smtp.example.com", "smtp_port": 465,
                "smtp_security": "ssl", "smtp_username": "sales@example.com",
                "smtp_password": "app-password", "batch_limit": 50,
            })
            self.assertTrue(public["configured"])
            self.assertNotIn("app-password", json.dumps(public))
            private = self.queue.settings(private=True)
            self.assertEqual(private["smtp_password"], "app-password")
            automation = self.queue.save_automation({
                "enabled": True, "sender_id": 1, "daily_limit": 50,
                "send_hour": 9, "send_minute": 0, "timezone": "Asia/Shanghai",
                "subject_template": "Hi {{company}}", "body_template": "Hello {{name}}",
            })
            self.assertEqual((automation["enabled"], automation["daily_limit"]), (1, 50))

    def test_create_package_saves_draft_without_queueing_or_sending(self):
        with patch.dict(os.environ, {"MAIL_CONFIG_KEY": Fernet.generate_key().decode()}):
            self.queue.save_settings({
                "provider": "smtp", "from_email": "sales@example.com",
                "smtp_host": "smtp.example.com", "smtp_username": "sales@example.com",
                "smtp_password": "app-password",
            })
            package = self.queue.create_package({
                "title": "Founder outreach", "sender_id": 1,
                "lead_ids": [self.lead], "subject": "Hi {{company}}",
                "body": "Hello {{name}}",
            }, "Admin")
        self.assertEqual(package["status"], "draft")
        self.assertEqual(package["recipient_count"], 1)
        self.assertEqual(package["recipients"][0]["email"], "alex@example.com")
        self.assertEqual(self.queue.rows(), [])
        self.assertEqual(self.queue.history(), [])
        self.assertEqual(self.queue.packages()[0]["id"], package["id"])

    def test_package_update_and_delivery_batch_details(self):
        with patch.dict(os.environ, {"MAIL_CONFIG_KEY": Fernet.generate_key().decode()}):
            self.queue.save_settings({
                "provider": "smtp", "from_email": "sales@example.com",
                "smtp_host": "smtp.example.com", "smtp_username": "sales@example.com",
                "smtp_password": "app-password",
            })
            package = self.queue.create_package({
                "title": "Draft batch", "lead_ids": [self.lead],
                "subject": "Placeholder", "body": "Placeholder",
            }, "Admin")
            package = self.queue.update_package(package["id"], {
                "title": "November founders", "sender_id": 1,
                "subject": "Hi {{company}}", "body": "Hello {{name}}",
            })
            self.assertEqual(package["subject"], "Hi {{company}}")
            _, settings, batch_id, messages = self.queue.send_package_rows(package["id"])
            self.assertEqual(self.queue.get_package(package["id"])["status"], "sending")
            self.queue.complete(
                messages[0], messages[0]["subject"], messages[0]["body"],
                settings["from_email"], "provider-message", "Admin",
            )
            self.queue.mark_package_sent(package["id"], batch_id)
        batches = self.queue.delivery_batches()
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0]["batch_id"], batch_id)
        self.assertEqual(batches[0]["title"], "November founders")
        self.assertEqual(batches[0]["recipient_count"], 1)
        self.assertEqual(batches[0]["recipients"][0]["company_name"], "Queue Test")
        self.assertEqual(batches[0]["recipients"][0]["email"], "alex@example.com")
        self.assertEqual(batches[0]["recipients"][0]["subject"], "Hi Queue Test")
        self.assertEqual(batches[0]["recipients"][0]["provider_message_id"], "provider-message")
        with self.assertRaisesRegex(ValueError, "只有草稿"):
            self.queue.update_package(package["id"], {
                "sender_id": 1, "subject": "Again", "body": "Again",
            })

    def test_batch_grouping_preserves_scope_and_uncertain_recipient_status(self):
        other_company = self.db.insert_company({"name": "Other Owner", "country_type": "domestic"})
        self.db.update_company(other_company, {"owner": "Other"})
        other_lead = self.db.insert_lead({
            "company_id": other_company, "email": "other-batch@example.com"
        })
        ids = self.queue.prepare([self.lead, other_lead], "Admin")
        batch_id = self.queue.claim(ids)
        row = next(item for item in self.queue.rows() if item["lead_id"] == self.lead)
        self.queue.complete(row, "Hi", "Body", "sales@example.com", "accepted", "Admin")
        batch = self.queue.delivery_batches()[0]
        self.assertEqual((batch["batch_id"], batch["recipient_count"], batch["sent_count"]),
                         (batch_id, 2, 1))
        self.assertEqual(batch["status"], "sending")
        uncertain = next(item for item in batch["recipients"]
                         if item["email"] == "other-batch@example.com")
        self.assertEqual(uncertain["status"], "sending")
        self.assertTrue(uncertain["created_at"])
        scoped = self.queue.delivery_batches("Rep")[0]
        self.assertEqual(scoped["recipient_count"], 1)
        self.assertEqual(scoped["recipients"][0]["email"], "alex@example.com")
        self.assertEqual(self.queue.delivery_batches("Missing"), [])
        # A legacy record without a known batch is never guessed into another batch.
        conn = get_connection(self.db.db_path)
        conn.execute("UPDATE mail_queue SET batch_id=NULL WHERE id=?", (row["id"],))
        conn.commit()
        conn.close()
        self.assertTrue(any(item["batch_id"].startswith("legacy-")
                            for item in self.queue.delivery_batches()))

    def test_draft_requires_neither_configuration_nor_automatic_sending(self):
        with patch.dict(os.environ, {"RESEND_API_KEY": "", "RESEND_FROM_EMAIL": ""}):
            draft = self.queue.create_package({
                "title": "Recipients only draft", "lead_ids": [self.lead],
                "subject": "Default subject", "body": "Default body",
            }, "Admin")
            self.assertEqual(draft["status"], "draft")
            self.assertEqual(self.queue.rows(), [])
            with self.assertRaisesRegex(ValueError, "尚未配置"):
                self.queue.send_package_rows(draft["id"])

    def test_changed_email_or_oversized_package_is_blocked_before_claim(self):
        second = self.db.insert_lead({
            "company_id": self.company, "email": "second@example.com"
        })
        with patch.dict(os.environ, {
            "RESEND_API_KEY": "unit-test-key", "RESEND_FROM_EMAIL": "sales@example.com"
        }):
            self.queue.save_settings({"from_email": "sales@example.com", "batch_limit": 1})
            package = self.queue.create_package({
                "title": "Too large", "lead_ids": [self.lead, second],
                "subject": "Hi", "body": "Body",
            }, "Admin")
            with self.assertRaisesRegex(ValueError, "单批最多发送"):
                self.queue.send_package_rows(package["id"])
            package = self.queue.create_package({
                "title": "Changed mailbox", "lead_ids": [self.lead],
                "subject": "Hi", "body": "Body",
            }, "Admin")
            self.db.update_lead(self.lead, {"email": "new@example.com"})
            with self.assertRaisesRegex(ValueError, "邮箱已变更"):
                self.queue.send_package_rows(package["id"])
        self.assertEqual(self.queue.rows(), [])

    def test_package_enforces_selected_leads_date_window(self):
        with patch.dict(os.environ, {"MAIL_CONFIG_KEY": Fernet.generate_key().decode()}):
            self.queue.save_settings({
                "provider": "smtp", "from_email": "sales@example.com",
                "smtp_host": "smtp.example.com", "smtp_username": "sales@example.com",
                "smtp_password": "app-password",
            })
            with self.assertRaisesRegex(ValueError, "日期范围"):
                self.queue.create_package({
                    "title": "Out of range", "sender_id": 1,
                    "lead_ids": [self.lead], "subject": "Hi", "body": "Hello",
                    "date_from": "2099-01-01", "date_to": "2099-12-31",
                }, "Admin")
            with self.assertRaisesRegex(ValueError, "开始日期"):
                self.queue.create_package({
                    "title": "Bad dates", "sender_id": 1,
                    "lead_ids": [self.lead], "subject": "Hi", "body": "Hello",
                    "date_from": "2026-12-31", "date_to": "2026-01-01",
                }, "Admin")

    def test_package_rejects_inaccessible_or_non_pending_recipients(self):
        with patch.dict(os.environ, {"MAIL_CONFIG_KEY": Fernet.generate_key().decode()}):
            self.queue.save_settings({
                "provider": "smtp", "from_email": "sales@example.com",
                "smtp_host": "smtp.example.com", "smtp_username": "sales@example.com",
                "smtp_password": "app-password",
            })
            with self.assertRaisesRegex(ValueError, "无权访问"):
                self.queue.create_package({
                    "title": "Private", "lead_ids": [self.lead],
                    "subject": "Hi", "body": "Hello",
                }, "Rep", "Other")
            self.assertIsNone(self.queue.get_package(999))
            self.queue.add(self.lead, "Rep")
            queue_row = self.queue.rows()[0]
            self.queue.claim([queue_row["id"]])
            self.queue.complete(queue_row, "Hi", "Hello", "sales@example.com", "msg", "Rep")
            with self.assertRaisesRegex(ValueError, "未发送"):
                self.queue.create_package({
                    "title": "Already sent", "lead_ids": [self.lead],
                    "subject": "Hi", "body": "Hello",
                }, "Rep", "Rep")

    def test_readonly_recipients_prepare_and_cross_sender_suppression(self):
        conn = get_connection(self.db.db_path)
        before = {
            table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table}").fetchall()]
            for table in ("companies", "leads", "outreach_records")
        }
        conn.close()
        self.assertEqual(self.queue.rows(), [])
        contacts = self.queue.recipients("Rep")
        self.assertEqual([row["lead_id"] for row in contacts], [self.lead])
        self.assertEqual(self.queue.recipients("Other"), [])
        self.assertEqual(self.queue.rows(), [])  # Simply reading never enqueues contacts.
        ids = self.queue.prepare([self.lead], "Rep", "Rep")
        self.assertEqual(ids, self.queue.prepare([self.lead], "Rep", "Rep"))
        self.queue.claim(ids)
        row = self.queue.rows()[0]
        self.queue.complete(row, "Hi", "Body", "first@example.com", "msg", "Rep")
        self.assertEqual(self.queue.recipients("Rep")[0]["status"], "sent")
        with self.assertRaisesRegex(ValueError, "未推送"):
            self.queue.prepare([self.lead], "Rep", "Rep")
        duplicate = self.db.insert_lead({
            "company_id": self.company, "email": "Alex@Example.com"
        })
        self.assertEqual(next(r for r in self.queue.recipients() if r["lead_id"] == duplicate)["status"], "sent")
        # Added duplicate is test setup, not a mail side effect.
        conn = get_connection(self.db.db_path)
        conn.execute("DELETE FROM leads WHERE id=?", (duplicate,))
        conn.commit()
        for table, expected in before.items():
            self.assertEqual([tuple(row) for row in conn.execute(f"SELECT * FROM {table}").fetchall()], expected)
        conn.close()
        self.assertEqual(self.queue.history("Other"), [])

    def test_duplicate_emails_blocked_before_batch_preparation(self):
        duplicate = self.db.insert_lead({"company_id": self.company, "email": "Alex@Example.com"})
        with self.assertRaisesRegex(ValueError, "重复邮箱"):
            self.queue.prepare([self.lead, duplicate], "Rep", "Rep")
        self.assertEqual(self.queue.rows(), [])

    def test_pending_market_cannot_be_reintroduced(self):
        with self.assertRaisesRegex(ValueError, "国内或海外"):
            self.db.insert_company({
                "name": "Pending Verification", "country_type": "pending_verification"
            })
        with self.assertRaisesRegex(ValueError, "国内或海外"):
            self.db.update_company(self.company, {"country_type": "pending_verification"})

    def test_sending_domain_round_trip_validation_and_legacy_defaults(self):
        config = {"from_email": "sales@mail.example.com", "sending_domain": "MAIL.Example.com"}
        self.assertEqual(self.queue.save_settings(config)["sending_domain"], "mail.example.com")
        self.assertEqual(self.queue.settings()["sending_domain"], "mail.example.com")
        for domain in ("", "https://mail.example.com", "example.com/path", "-bad.com", "other.com"):
            with self.assertRaises(ValueError):
                self.queue.save_settings(dict(config, sending_domain=domain))
        legacy = self.queue.save_settings({"from_email": "sales@example.com"})
        self.assertEqual(legacy["sending_domain"], "example.com")
        conn = get_connection(self.db.db_path)
        conn.execute("UPDATE mail_settings SET settings_json=?", (json.dumps({"from_email": "old@example.com"}),))
        conn.commit()
        conn.close()
        self.assertEqual(self.queue.settings()["sending_domain"], "example.com")

    def test_validation_and_duplicate_mailboxes(self):
        for config in [
            {"from_email": "invalid"}, {"from_email": "sales@example.com", "batch_limit": 51},
            {"from_email": "sales@example.com", "reply_to": "invalid"},
            {"from_email": "sales@example.com", "from_name": "x\ninjected"},
        ]:
            with self.assertRaises(ValueError):
                self.queue.save_settings(config)
        self.queue.add(self.lead, "Rep")
        second = self.db.insert_lead({
            "company_id": self.company, "contact_name": "Other", "email": "Alex@Example.com"
        })
        with self.assertRaisesRegex(ValueError, "重复"):
            self.queue.add(second, "Rep")

    def test_legacy_suppression_and_no_email(self):
        self.db.import_email_prospects([{"email": "alex@example.com"}], "Admin")
        legacy = self.db.list_email_prospects()[0]
        self.db.update_email_prospect_status(legacy["id"], "unsubscribed")
        with self.assertRaisesRegex(ValueError, "退订"):
            self.queue.add(self.lead, "Rep")
        blank = self.db.insert_lead({"company_id": self.company, "contact_name": "No Email"})
        with self.assertRaisesRegex(ValueError, "有效邮箱"):
            self.queue.add(blank, "Rep")

    def test_owner_scope_email_changes_and_no_resubscribe(self):
        self.queue.add(self.lead, "Rep")
        row = self.queue.rows()[0]
        self.assertEqual(self.queue.rows("Other"), [])
        with self.assertRaises(ValueError):
            self.queue.select([row["id"]], "Other")
        self.db.update_lead(self.lead, {"email": "new@example.com"})
        with self.assertRaisesRegex(ValueError, "邮箱已变更"):
            self.queue.select([row["id"]], "Rep", sendable=True)
        self.db.update_lead(self.lead, {"email": "alex@example.com"})
        self.queue.status(row["id"], "unsubscribed", "Rep")
        with self.assertRaises(ValueError):
            self.queue.status(row["id"], "pending", "Rep")
        with self.assertRaises(ValueError):
            self.queue.select([row["id"]], sendable=True)

    def test_atomic_batch_claim_and_independent_history_preserves_crm(self):
        self.queue.add(self.lead, "Rep")
        row = self.queue.rows()[0]
        self.db.record_outreach({"lead_id": self.lead, "channel": "cold_email", "status": "negotiation"})
        self.queue.claim([row["id"]])
        with self.assertRaisesRegex(ValueError, "重复发送"):
            self.queue.claim([row["id"]])
        with self.assertRaises(ValueError):
            self.queue.status(row["id"], "removed")
        self.queue.recover_unsent(row["id"])
        self.assertEqual(self.queue.rows()[0]["status"], "pending")
        self.queue.claim([row["id"]])
        self.queue.complete(row, "Subject", "Body", "sales@example.com", "msg_test", "Rep")
        with self.assertRaisesRegex(ValueError, "仅发送中"):
            self.queue.recover_unsent(row["id"])
        self.assertEqual(self.queue.rows()[0]["status"], "sent")
        latest = self.db.query_pipeline()[0]
        self.assertEqual(latest["channel"], "cold_email")
        self.assertEqual(latest["status"], "negotiation")
        conn = get_connection(self.db.db_path)
        self.assertEqual(conn.execute("SELECT count(*) FROM outreach_records").fetchone()[0], 1)
        conn.close()
        self.assertEqual(self.queue.history()[0]["sender"], "sales@example.com")
        other = self.db.insert_lead({
            "company_id": self.company, "contact_name": "Other", "email": "other@example.com"
        })
        self.queue.add(other, "Rep")
        pending = self.queue.rows()[0]
        with self.assertRaises(ValueError):
            self.queue.claim([row["id"], pending["id"]])
        self.assertEqual(self.queue.rows()[0]["status"], "pending")


if __name__ == "__main__":
    unittest.main()
