import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

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


if __name__ == "__main__":
    unittest.main()
