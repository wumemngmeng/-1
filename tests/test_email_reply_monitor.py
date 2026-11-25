import os
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet

from core.database import DatabaseManager
from core.email_queue import EmailQueue
from core.email_reply_monitor import EmailReplyMonitor, default_imap_host


class FakeImap:
    messages = {}

    def __init__(self, host, port, security):
        self.host, self.port, self.security = host, port, security
        self.readonly = None
        self.logged_out = False

    def login(self, username, password):
        self.username, self.password = username, password
        return "OK", [b"logged in"]

    def select(self, mailbox, readonly=False):
        self.readonly = readonly
        return "OK", [b"2"]

    def uid(self, command, *_args):
        if command == "search":
            return "OK", [b" ".join(str(value).encode() for value in sorted(self.messages))]
        uid = int(_args[0])
        return "OK", [(b"BODY[]", self.messages[uid])]

    def logout(self):
        self.logged_out = True


class EmailReplyMonitorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db = DatabaseManager(str(Path(self.directory.name) / "crm.db"))
        self.queue = EmailQueue(self.db)
        company = self.db.insert_company({"name": "Reply Customer", "country_type": "overseas"})
        self.db.update_company(company, {"owner": "Rep"})
        self.lead = self.db.insert_lead({
            "company_id": company, "contact_name": "Alex",
            "email": "alex@example.com",
        })
        self.key = Fernet.generate_key().decode()
        with patch.dict(os.environ, {"MAIL_CONFIG_KEY": self.key}):
            self.queue.save_settings({
                "provider": "smtp", "from_email": "sales@example.com",
                "smtp_host": "smtp.qiye.aliyun.com", "smtp_port": 465,
                "smtp_security": "ssl", "smtp_username": "sales@example.com",
                "smtp_password": "app-password",
            })
        self.queue.add(self.lead, "Rep")
        row = self.queue.rows()[0]
        self.queue.claim([row["id"]])
        self.queue.complete(row, "Outbound", "Hello", "sales@example.com", "msg-1", "Rep")

    @staticmethod
    def message(auto=False):
        item = EmailMessage()
        item["From"] = "Alex <alex@example.com>"
        item["To"] = "sales@example.com"
        item["Date"] = "Fri, 25 Nov 2030 03:30:00 +0000"
        item["Subject"] = "Re: Outbound"
        item["Message-ID"] = "<reply@example.com>"
        item["In-Reply-To"] = "<msg-1@example.com>"
        if auto:
            item["Auto-Submitted"] = "auto-replied"
        item.set_content("Thanks, this looks interesting.\n\nOn Tuesday someone wrote:\nOld text")
        return item.as_bytes()

    def test_default_alibaba_imap_host(self):
        self.assertEqual(default_imap_host("smtp.qiye.aliyun.com"), "imap.qiye.aliyun.com")

    def test_poll_is_readonly_filters_automation_and_is_idempotent(self):
        FakeImap.messages = {101: self.message(), 102: self.message(auto=True)}
        clients = []

        def factory(*args):
            client = FakeImap(*args)
            clients.append(client)
            return client

        monitor = EmailReplyMonitor(self.db, factory)
        with patch.dict(os.environ, {"MAIL_CONFIG_KEY": self.key}):
            first = monitor.poll_sender()
            second = monitor.poll_sender()
        self.assertEqual(
            (first["scanned"], first["matched"], first["created"]),
            (2, 1, 1),
        )
        self.assertEqual(second["created"], 0)
        self.assertTrue(all(client.readonly for client in clients))
        self.assertTrue(all(client.logged_out for client in clients))
        reply = monitor.replies()[0]
        self.assertEqual(reply["from_email"], "alex@example.com")
        self.assertEqual(reply["subject"], "Re: Outbound")
        self.assertEqual(reply["snippet"], "Thanks, this looks interesting.")
        self.assertEqual(monitor.status()["unread"], 1)

    def test_owner_scope_and_internal_read_marker(self):
        FakeImap.messages = {101: self.message()}
        monitor = EmailReplyMonitor(self.db, FakeImap)
        with patch.dict(os.environ, {"MAIL_CONFIG_KEY": self.key}):
            monitor.poll_sender()
        reply_id = monitor.replies("Rep")[0]["id"]
        self.assertEqual(monitor.replies("Other"), [])
        with self.assertRaisesRegex(ValueError, "无权访问"):
            monitor.mark_read([reply_id], "Other")
        monitor.mark_read([reply_id], "Rep")
        self.assertEqual(monitor.replies("Rep")[0]["is_read"], 1)


if __name__ == "__main__":
    unittest.main()
