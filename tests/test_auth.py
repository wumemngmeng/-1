import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from urllib.parse import urlencode

from core.database import DatabaseManager
from web.server import CustomHandler


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db = DatabaseManager(str(Path(self.directory.name) / "auth.db"))
        self.db.register_user("rep_test", "strongpass", "测试销售", "sales_rep")
        self.db.register_user("viewer_test", "strongpass", "测试观察员", "observer")
        rep_user = self.db.authenticate_user("rep_test", "strongpass")
        viewer_user = self.db.authenticate_user("viewer_test", "strongpass")
        self.db.change_password(rep_user["id"], "strongpass", "rep-secure-password")
        self.db.change_password(viewer_user["id"], "strongpass", "viewer-secure-password")
        owned = self.db.insert_company({"name": "销售名下客户"})
        other = self.db.insert_company({"name": "其他客户"})
        self.db.update_company(owned, {"owner": "测试销售"})
        self.db.update_company(other, {"owner": "其他销售"})
        self.owned_id, self.other_id = owned, other
        database = self.db

        class Handler(CustomHandler):
            def __init__(self, *args, **kwargs):
                self.db = database
                super(CustomHandler, self).__init__(*args, **kwargs)

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.directory.cleanup()

    def request(self, method, path, body=None, cookie=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        headers = {}
        if body is not None:
            body = json.dumps(body)
            headers["Content-Type"] = "application/json"
        if cookie:
            headers["Cookie"] = cookie
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        payload = json.loads(response.read()) if response.getheader("Content-Type", "").startswith("application/json") else None
        result = response.status, payload, response.getheader("Set-Cookie")
        connection.close()
        return result

    def login(self, username, password):
        status, payload, cookie = self.request(
            "POST", "/api/login", {"username": username, "password": password}
        )
        self.assertEqual(status, 200, payload)
        return cookie.split(";", 1)[0]

    def test_login_gate_admin_registration_and_role_enforcement(self):
        self.assertEqual(self.request("GET", "/api/pipeline")[0], 401)
        self.assertEqual(self.request("POST", "/api/register", {
            "username": "blocked", "password": "strongpass", "display_name": "Blocked"
        })[0], 401)

        admin = self.login("admin", "admin123")
        status, payload, _ = self.request("POST", "/api/register", {
            "username": "blocked_until_password_change", "password": "strongpass",
            "display_name": "Blocked"
        }, admin)
        self.assertEqual((status, payload["code"]), (403, "PASSWORD_CHANGE_REQUIRED"))
        self.assertTrue(self.request("POST", "/api/password", {
            "current_password": "admin123", "new_password": "admin-secure-password"
        }, admin)[1]["success"])
        status, payload, _ = self.request("POST", "/api/register", {
            "username": "created_by_admin", "password": "strongpass",
            "display_name": "新销售", "role": "sales_rep"
        }, admin)
        self.assertEqual((status, payload["success"]), (200, True))

        observer = self.login("viewer_test", "viewer-secure-password")
        self.assertEqual(self.request("POST", "/api/company/update", {
            "company_id": self.owned_id, "updates": {"notes": "blocked"}
        }, observer)[0], 403)

    def test_sales_rep_only_sees_and_edits_owned_customers(self):
        rep = self.login("rep_test", "rep-secure-password")
        status, rows, _ = self.request("GET", "/api/pipeline", cookie=rep)
        self.assertEqual(status, 200)
        self.assertEqual([row["company_id"] for row in rows], [self.owned_id])
        self.assertEqual(self.request("POST", "/api/company/update", {
            "company_id": self.other_id, "updates": {"notes": "blocked"}
        }, rep)[0], 403)
        status, payload, _ = self.request("POST", "/api/company/update", {
            "company_id": self.owned_id, "updates": {"notes": "allowed", "owner": ""}
        }, rep)
        self.assertEqual((status, payload["success"]), (200, True))
        self.assertEqual(self.db.company_owner(self.owned_id), "测试销售")

    def test_market_stats_and_pipeline_preserve_owner_scope(self):
        rep = self.login("rep_test", "rep-secure-password")
        overseas = self.db.insert_company({"name": "海外名下客户", "country_type": "overseas"})
        self.db.update_company(overseas, {"owner": "测试销售"})
        hidden = self.db.insert_company({"name": "其他海外客户", "country_type": "overseas"})
        self.db.update_company(hidden, {"owner": "其他销售"})
        for company_id in (self.owned_id, overseas, hidden):
            lead = self.db.insert_lead({"company_id": company_id, "contact_name": "联系人"})
            self.db.record_outreach({"lead_id": lead, "status": "sample_sent"})
        from core.database import get_connection
        conn = get_connection(self.db.db_path)
        conn.execute("UPDATE outreach_records SET created_at = datetime('now', '+8 hours', 'start of day', '-1 day', '-8 hours', '+1 hour')")
        conn.commit()
        conn.close()

        for market, company_id in (("domestic", self.owned_id), ("overseas", overseas)):
            status, rows, _ = self.request("GET", "/api/pipeline?country=" + market, cookie=rep)
            self.assertEqual(status, 200)
            self.assertEqual([row["company_id"] for row in rows], [company_id])
            status, stats, _ = self.request("GET", "/api/stats?country=" + market, cookie=rep)
            self.assertEqual(status, 200)
            self.assertEqual(stats["total_companies"], 1)
            self.assertEqual(stats["companies_by_tier"], {"B": 1})
            self.assertEqual(stats["yesterday_outreach"], {"sample_sent": 1})
            self.assertTrue(stats["data_last_updated"])
        self.assertEqual(self.request("GET", "/api/stats?country=invalid", cookie=rep)[0], 400)

    def test_pipeline_orders_by_latest_company_lead_or_outreach_update(self):
        older = self.db.insert_company({"name": "较早更新客户", "country_type": "overseas"})
        newer = self.db.insert_company({"name": "最新更新客户", "country_type": "overseas"})
        older_lead = self.db.insert_lead({"company_id": older, "contact_name": "较早联系人"})
        newer_lead = self.db.insert_lead({"company_id": newer, "contact_name": "最新联系人"})
        older_touch = self.db.record_outreach({"lead_id": older_lead, "status": "sample_sent"})
        newer_touch = self.db.record_outreach({"lead_id": newer_lead, "status": "sample_sent"})
        from core.database import get_connection
        conn = get_connection(self.db.db_path)
        for table in ("companies", "leads", "outreach_records"):
            conn.execute(f"UPDATE {table} SET updated_at = '2026-10-20 00:00:00'")
        conn.execute(
            "UPDATE outreach_records SET updated_at = '2026-10-27 08:00:00' WHERE id = ?",
            (newer_touch,),
        )
        conn.commit()
        conn.close()

        rows = self.db.query_pipeline(country_type="overseas")
        self.assertEqual([row["company_id"] for row in rows[:2]], [newer, older])
        self.assertEqual(rows[0]["data_updated_at"], "2026-10-27 08:00:00")

    def test_logout_clears_session(self):
        admin = self.login("admin", "admin123")
        status, payload, _ = self.request("POST", "/api/password", {
            "current_password": "admin123", "new_password": "new-secure-password"
        }, admin)
        self.assertEqual((status, payload["success"]), (200, True))
        self.login("admin", "new-secure-password")
        status, _, cookie = self.request("POST", "/api/logout", cookie=admin)
        self.assertEqual(status, 200)
        self.assertIn("Max-Age=0", cookie)

    def test_session_is_persistent_and_refreshed(self):
        admin = self.login("admin", "admin123")
        status, payload, cookie = self.request("GET", "/api/session", cookie=admin)
        self.assertEqual((status, payload["success"]), (200, True))
        self.assertIn("Max-Age=2592000", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)

    def test_admin_can_upsert_prospect_without_duplicate_lead(self):
        admin = self.login("admin", "admin123")
        self.request("POST", "/api/password", {
            "current_password": "admin123", "new_password": "admin-secure-password"
        }, admin)
        prospect = {
            "company": {"name": "Prospect Co", "country_type": "overseas", "industry": "Apparel"},
            "lead": {"contact_name": "Founder", "x_url": "https://x.com/founder"},
            "touch": {"status": "sample_sent", "interaction_notes": "Sent on X"},
        }
        status, payload, _ = self.request("POST", "/api/prospect/upsert", prospect, admin)
        self.assertEqual((status, payload["success"]), (200, True))
        first_lead = payload["lead_id"]
        status, payload, _ = self.request("POST", "/api/prospect/upsert", prospect, admin)
        self.assertEqual((status, payload["lead_id"]), (200, first_lead))
        rows = self.db.query_pipeline()
        row = next(row for row in rows if row["company_name"] == "Prospect Co")
        self.assertEqual(row["status"], "sample_sent")

    def test_team_members_can_read_and_create_strategy_versions(self):
        self.assertEqual(self.request("GET", "/api/strategies")[0], 401)
        admin = self.login("admin", "admin123")
        self.request("POST", "/api/password", {
            "current_password": "admin123", "new_password": "admin-secure-password"
        }, admin)
        status, payload, _ = self.request("POST", "/api/strategies", {
            "title": "Filter strategy", "content": "Target S/A accounts"
        }, admin)
        self.assertEqual((status, payload["strategy"]["version"]), (200, 1))
        observer = self.login("viewer_test", "viewer-secure-password")
        self.assertTrue(self.request("GET", "/api/strategies", cookie=observer)[1][0]["content"])
        status, payload, _ = self.request("POST", "/api/strategies", {
            "title": "Filter strategy", "content": "Add evidence criteria"
        }, observer)
        self.assertEqual((status, payload["strategy"]["version"]), (200, 2))

    def test_team_members_can_read_and_sync_discord_communities(self):
        self.assertEqual(self.request("GET", "/api/discord-communities")[0], 401)
        observer = self.login("viewer_test", "viewer-secure-password")
        status, rows, _ = self.request(
            "GET", "/api/discord-communities?" + urlencode({"language": "中文"}),
            cookie=observer,
        )
        self.assertEqual((status, len(rows)), (200, 3))
        status, payload, _ = self.request("POST", "/api/discord-communities", {
            "name": "Test Community", "invite_url": "https://discord.gg/test",
            "focus": "AI 视频", "language": "中文", "fit_level": "高",
            "join_status": "已加入", "account_type": "个人号", "priority": 19,
        }, observer)
        self.assertEqual((status, payload["community"]["name"]), (200, "Test Community"))

    def test_email_pool_import_preview_permissions_and_safe_send(self):
        observer = self.login("viewer_test", "viewer-secure-password")
        self.assertEqual(self.request("POST", "/api/email-prospects/import", {
            "records": [{"email": "blocked@example.com"}]
        }, observer)[0], 403)

        admin = self.login("admin", "admin123")
        self.request("POST", "/api/password", {
            "current_password": "admin123", "new_password": "admin-secure-password"
        }, admin)
        status, payload, _ = self.request("POST", "/api/email-prospects/import", {
            "records": [
                {"email": "Alex@Example.com", "contact_name": "Alex", "company_name": "Example"},
                {"email": "alex@example.com", "notes": "deduplicated"},
                {"email": "invalid"},
            ]
        }, admin)
        self.assertEqual((status, payload["inserted"], payload["updated"], payload["invalid_count"]), (200, 1, 0, 1))
        rows = self.request("GET", "/api/email-prospects", cookie=admin)[1]["rows"]
        self.assertEqual(len(rows), 1)
        prospect_id = rows[0]["id"]
        status, payload, _ = self.request("POST", "/api/email-prospects/preview", {
            "ids": [prospect_id], "subject": "Hi {{company}}",
            "body": "Hello {{name}} at {{email}}",
        }, admin)
        self.assertEqual(status, 200)
        self.assertEqual(payload["previews"][0]["subject"], "Hi Example")
        self.assertEqual(payload["previews"][0]["body"], "Hello Alex at alex@example.com")

        self.assertEqual(self.request("POST", "/api/email-prospects/send", {
            "ids": [prospect_id], "subject": "Hi", "body": "Body",
            "confirmation": "wrong",
        }, admin)[0], 400)
        with patch.dict("os.environ", {
            "RESEND_API_KEY": "test-key", "RESEND_FROM_EMAIL": "sales@example.com"
        }), patch("web.server.send_resend_batch", return_value=["msg_123"]):
            status, payload, _ = self.request("POST", "/api/email-prospects/send", {
                "ids": [prospect_id], "subject": "Hi {{company}}",
                "body": "Hello {{name}}", "confirmation": "SEND 1",
            }, admin)
        self.assertEqual((status, payload["sent"], payload["failed"]), (200, 1, 0))
        self.assertEqual(self.request("POST", "/api/email-prospects/send", {
            "ids": [prospect_id], "subject": "Hi", "body": "Again",
            "confirmation": "SEND 1",
        }, admin)[0], 400)


if __name__ == "__main__":
    unittest.main()
