import http.client
import base64
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from urllib.parse import urlencode

from core.database import DatabaseManager
from web.server import CustomHandler, verify_github_actions_oidc


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

    def request(self, method, path, body=None, cookie=None, extra_headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        headers = dict(extra_headers or {})
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

    def test_admin_can_update_own_login_account_and_password(self):
        admin = self.login("admin", "admin123")
        self.assertTrue(self.request("POST", "/api/password", {
            "current_password": "admin123", "new_password": "admin-secure-password"
        }, admin)[1]["success"])
        self.assertEqual(self.request("POST", "/api/admin-account", {
            "username": "renamed-admin", "current_password": "wrong-password",
            "new_password": "new-secure-password",
        }, admin)[0], 400)
        status, result, _ = self.request("POST", "/api/admin-account", {
            "username": "renamed-admin", "current_password": "admin-secure-password",
            "new_password": "new-secure-password",
        }, admin)
        self.assertEqual((status, result["success"], result["username"]), (200, True, "renamed-admin"))
        self.assertEqual(self.request("POST", "/api/login", {
            "username": "admin", "password": "admin-secure-password"
        })[0], 401)
        self.assertEqual(self.request("POST", "/api/login", {
            "username": "renamed-admin", "password": "new-secure-password"
        })[0], 200)
        rep = self.login("rep_test", "rep-secure-password")
        self.assertEqual(self.request("POST", "/api/admin-account", {
            "username": "should-fail", "current_password": "rep-secure-password",
        }, rep)[0], 403)

    def test_admin_account_validation_and_duplicate_username(self):
        admin = self.login("admin", "admin123")
        self.assertTrue(self.request("POST", "/api/password", {
            "current_password": "admin123", "new_password": "admin-secure-password"
        }, admin)[1]["success"])
        for body in (
            {"username": "ab", "current_password": "admin-secure-password"},
            {"username": "rep_test", "current_password": "admin-secure-password"},
            {"username": "valid-admin", "current_password": "admin-secure-password",
             "new_password": "short"},
        ):
            self.assertEqual(self.request("POST", "/api/admin-account", body, admin)[0], 400)
        status, result, _ = self.request("POST", "/api/admin-account", {
            "username": "admin-renamed", "current_password": "admin-secure-password",
            "new_password": "",
        }, admin)
        self.assertEqual((status, result["success"], result["password_changed"]),
                         (200, True, False))
        self.assertEqual(self.request("POST", "/api/login", {
            "username": "admin-renamed", "password": "admin-secure-password"
        })[0], 200)

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
        self.assertEqual(
            self.request("GET", "/api/pipeline?country=pending_verification", cookie=rep)[0],
            400,
        )
        self.assertEqual(
            self.request("GET", "/api/stats?country=pending_verification", cookie=rep)[0],
            400,
        )
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

    def mail_admin(self):
        admin = self.login("admin", "admin123")
        self.request("POST", "/api/password", {
            "current_password": "admin123", "new_password": "admin-secure-password"
        }, admin)
        return admin

    def test_feishu_mail_import_is_admin_only_and_fails_closed_without_credentials(self):
        self.assertEqual(self.request("GET", "/api/feishu-mail-import/status")[0], 401)
        observer = self.login("viewer_test", "viewer-secure-password")
        self.assertEqual(self.request(
            "GET", "/api/feishu-mail-import/status", cookie=observer
        )[0], 403)
        admin = self.mail_admin()
        with patch.dict("os.environ", {
            "FEISHU_APP_ID": "", "FEISHU_APP_SECRET": "",
        }):
            status, payload, _ = self.request(
                "GET", "/api/feishu-mail-import/status", cookie=admin
            )
            self.assertEqual((status, payload["configured"]), (200, False))
            status, payload, _ = self.request(
                "POST", "/api/feishu-mail-import/preview", {}, admin
            )
            self.assertEqual(status, 400)
            self.assertIn("尚未配置飞书应用凭证", payload["error"])
            self.assertEqual(self.request(
                "POST", "/api/feishu-mail-import/apply", {
                    "confirmation": "IMPORT FEISHU MAIL SHEET"
                }, observer
            )[0], 403)

    def test_feishu_mail_sync_requires_cron_secret_and_never_uses_user_session(self):
        self.assertEqual(self.request("GET", "/api/feishu-mail-sync/run")[0], 401)
        with patch.dict("os.environ", {"CRON_SECRET": "cron-test-secret"}):
            self.assertEqual(self.request("GET", "/api/feishu-mail-sync/run")[0], 401)
            with patch(
                "core.feishu_import.FeishuMailSheetImporter.apply",
                return_value={
                    "created_companies": 2,
                    "updated_companies": 1,
                    "created_contacts": 3,
                    "conflicts": 4,
                    "suppressed_existing_statuses": 1,
                },
            ) as apply, patch(
                "core.feishu_discord.FeishuDiscordImporter.apply",
                return_value={"create": 0},
            ) as discord_apply:
                with patch(
                    "core.feishu_import.FeishuXStatusSync.apply",
                    return_value={"created_outreach_records": 0},
                ) as x_apply:
                    status, payload, _ = self.request(
                        "GET",
                        "/api/feishu-mail-sync/run",
                        extra_headers={"Authorization": "Bearer cron-test-secret"},
                    )
        self.assertEqual((status, payload["success"]), (200, True))
        self.assertEqual(payload["created_contacts"], 3)
        apply.assert_called_once_with("Vercel Feishu Sync")
        discord_apply.assert_called_once_with("Vercel Feishu Sync")
        x_apply.assert_called_once_with("Vercel Feishu Sync")

    def test_feishu_mail_sync_accepts_only_verified_github_actions_identity(self):
        result = {
            "created_companies": 0,
            "updated_companies": 0,
            "created_contacts": 0,
            "conflicts": 0,
            "suppressed_existing_statuses": 0,
        }
        with patch.dict("os.environ", {"CRON_SECRET": ""}), patch(
            "web.server.verify_github_actions_oidc", return_value=True
        ) as verify, patch(
            "core.feishu_import.FeishuMailSheetImporter.apply", return_value=result
        ) as apply, patch(
            "core.feishu_discord.FeishuDiscordImporter.apply",
            return_value={"create": 0},
        ) as discord_apply, patch(
            "core.feishu_import.FeishuXStatusSync.apply",
            return_value={"created_outreach_records": 0},
        ) as x_apply:
            status, payload, _ = self.request(
                "GET",
                "/api/feishu-mail-sync/run",
                extra_headers={"Authorization": "Bearer signed-github-token"},
            )
        self.assertEqual((status, payload["success"]), (200, True))
        verify.assert_called_once_with("signed-github-token")
        apply.assert_called_once_with("Vercel Feishu Sync")
        discord_apply.assert_called_once_with("Vercel Feishu Sync")
        x_apply.assert_called_once_with("Vercel Feishu Sync")

    def test_email_reply_monitor_requires_verified_scheduler_and_scopes_reads(self):
        self.assertEqual(self.request("GET", "/api/email-reply-monitor/run")[0], 401)
        observer = self.login("viewer_test", "viewer-secure-password")
        self.assertEqual(self.request("GET", "/api/email-replies", cookie=observer)[0], 403)
        with patch.dict("os.environ", {"CRON_SECRET": ""}), patch(
            "web.server.verify_github_actions_oidc", return_value=True
        ) as verify, patch(
            "core.email_reply_monitor.EmailReplyMonitor.poll_all",
            return_value={"senders": 1, "scanned": 3, "matched": 1, "created": 1, "results": []},
        ):
            status, payload, _ = self.request(
                "GET", "/api/email-reply-monitor/run",
                extra_headers={"Authorization": "Bearer reply-monitor-token"},
            )
        self.assertEqual((status, payload["created"]), (200, 1))
        verify.assert_called_once_with(
            "reply-monitor-token",
            audience="aibubu-email-reply-monitor",
            workflow_ref=(
                "wumemngmeng/-1/.github/workflows/"
                "email-reply-monitor.yml@refs/heads/main"
            ),
        )

    def test_github_actions_oidc_verifier_checks_signature_and_workflow_identity(self):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding, rsa

        def encode(value):
            raw = json.dumps(value, separators=(",", ":")).encode()
            return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

        def integer(value):
            length = (value.bit_length() + 7) // 8
            return base64.urlsafe_b64encode(value.to_bytes(length, "big")).rstrip(b"=").decode()

        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        numbers = private_key.public_key().public_numbers()
        timestamp = int(time.time())
        header = {"alg": "RS256", "kid": "test-key"}
        claims = {
            "iss": "https://token.actions.githubusercontent.com",
            "aud": "aibubu-feishu-sync",
            "repository": "wumemngmeng/-1",
            "ref": "refs/heads/main",
            "workflow_ref": "wumemngmeng/-1/.github/workflows/feishu-sync.yml@refs/heads/main",
            "event_name": "schedule",
            "iat": timestamp - 10,
            "nbf": timestamp - 10,
            "exp": timestamp + 300,
        }

        def token(payload):
            encoded = f"{encode(header)}.{encode(payload)}"
            signature = private_key.sign(
                encoded.encode(), padding.PKCS1v15(), hashes.SHA256()
            )
            return f"{encoded}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode()}"

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self):
                return json.dumps({"keys": [{
                    "kid": "test-key", "kty": "RSA",
                    "e": integer(numbers.e), "n": integer(numbers.n),
                }]}).encode()

        opener = lambda *_args, **_kwargs: Response()
        self.assertTrue(verify_github_actions_oidc(token(claims), opener, timestamp))
        self.assertFalse(verify_github_actions_oidc(
            token(dict(claims, repository="attacker/repo")), opener, timestamp
        ))

    def test_email_queue_scope_settings_and_uncertain_send(self):
        from cryptography.fernet import Fernet
        admin = self.mail_admin()
        rep = self.login("rep_test", "rep-secure-password")
        observer = self.login("viewer_test", "viewer-secure-password")
        owned = self.db.insert_lead({"company_id": self.owned_id, "contact_name": "Owned", "email": "owned@example.com"})
        other = self.db.insert_lead({"company_id": self.other_id, "contact_name": "Other", "email": "other@example.com"})
        self.assertEqual(self.request("GET", "/api/email-queue")[0], 401)
        self.assertEqual(self.request("GET", "/api/email-queue", cookie=observer)[0], 403)
        self.assertEqual(self.request("POST", "/api/email-queue/add", {"lead_ids": [other]}, rep)[0], 403)
        self.request("POST", "/api/email-queue/add", {"lead_ids": [owned, other]}, admin)
        own_rows = self.request("GET", "/api/email-queue", cookie=rep)[1]["rows"]
        self.assertEqual([row["lead_id"] for row in own_rows], [owned])
        other_row = next(row for row in self.request("GET", "/api/email-queue", cookie=admin)[1]["rows"] if row["lead_id"] == other)
        self.assertEqual(self.request("POST", "/api/email-queue/preview", {
            "ids": [other_row["id"]], "subject": "Hi", "body": "Body"
        }, rep)[0], 400)
        self.assertEqual(self.request("POST", "/api/email-settings", {"from_email": "sales@example.com"}, rep)[0], 403)
        with patch.dict("os.environ", {"MAIL_CONFIG_KEY": Fernet.generate_key().decode()}), patch("web.server.send_resend_batch") as send:
            status, payload, _ = self.request("POST", "/api/email-settings", {
                "from_email": "sales@example.com", "api_key": "do-not-echo",
                "batch_limit": 1, "reply_to": "reply@example.com",
            }, admin)
            self.assertEqual(status, 200)
            self.assertNotIn("do-not-echo", json.dumps(payload))
            send.assert_not_called()
            send.side_effect = TimeoutError("unknown")
            status, payload, _ = self.request("POST", "/api/email-queue/send", {
                "ids": [own_rows[0]["id"]], "subject": "Hi", "body": "Body", "confirmation": "SEND 1"
            }, rep)
            self.assertEqual(status, 502)
            self.assertEqual(self.request("GET", "/api/email-queue", cookie=rep)[1]["rows"][0]["status"], "sending")
            self.assertEqual(self.request("POST", "/api/email-queue/send", {
                "ids": [own_rows[0]["id"]], "subject": "Hi", "body": "Body", "confirmation": "SEND 1"
            }, rep)[0], 400)
            self.assertEqual(send.call_count, 1)

    def test_admin_can_test_smtp_login_without_sending(self):
        from cryptography.fernet import Fernet
        admin = self.mail_admin()
        rep = self.login("rep_test", "rep-secure-password")
        with patch.dict("os.environ", {"MAIL_CONFIG_KEY": Fernet.generate_key().decode()}):
            status, payload, _ = self.request("POST", "/api/email-senders", {
                "provider": "smtp", "sending_domain": "example.com",
                "from_email": "sales@example.com", "smtp_host": "smtp.example.com",
                "smtp_port": 465, "smtp_security": "ssl",
                "smtp_username": "sales@example.com", "smtp_password": "secret",
            }, admin)
            self.assertEqual(status, 200)
            sender_id = payload["settings"]["id"]
            self.assertEqual(self.request("POST", "/api/email-senders/test", {
                "id": sender_id
            }, rep)[0], 403)
            with patch("web.server.test_smtp_connection") as test:
                status, payload, _ = self.request("POST", "/api/email-senders/test", {
                    "id": sender_id
                }, admin)
                self.assertEqual((status, payload["success"]), (200, True))
                test.assert_called_once()

    def test_daily_email_page_recipients_sender_selection_and_independent_history(self):
        from cryptography.fernet import Fernet
        admin = self.mail_admin()
        rep = self.login("rep_test", "rep-secure-password")
        observer = self.login("viewer_test", "viewer-secure-password")
        lead = self.db.insert_lead({
            "company_id": self.owned_id, "contact_name": "Daily", "email": "daily@example.com"
        })
        other = self.db.insert_lead({"company_id": self.other_id, "email": "other@example.com"})
        self.assertEqual(self.request("GET", "/api/email-recipients", cookie=observer)[0], 403)
        payload = self.request("GET", "/api/email-recipients", cookie=rep)[1]
        self.assertEqual([row["lead_id"] for row in payload["rows"]], [lead])
        self.assertEqual(self.request("GET", "/api/email-queue", cookie=rep)[1]["rows"], [])
        self.assertEqual(self.request("POST", "/api/email-senders", {
            "from_email": "denied@example.com"
        }, rep)[0], 403)
        message = {"lead_ids": [lead], "subject": "Hi {{company}}", "body": "Hello {{name}}"}
        self.assertEqual(self.request("POST", "/api/email-queue/preview", dict(
            message, lead_ids=[other]
        ), rep)[0], 400)
        with patch.dict("os.environ", {"MAIL_CONFIG_KEY": Fernet.generate_key().decode()}), patch(
            "web.server.send_resend_batch", return_value=["daily_msg"]
        ) as send:
            status, payload, _ = self.request("POST", "/api/email-senders", {
                "sending_domain": "other.com", "from_email": "selected@other.com",
                "api_key": "selected-secret", "batch_limit": 5,
            }, admin)
            self.assertEqual(status, 200)
            sender_id = payload["settings"]["id"]
            send.assert_not_called()
            self.assertNotIn("selected-secret", json.dumps(self.request(
                "GET", "/api/email-recipients", cookie=rep
            )[1]))
            message["sender_id"] = sender_id
            status, preview, _ = self.request("POST", "/api/email-queue/preview", message, rep)
            self.assertEqual(status, 200)
            self.assertEqual(preview["sender"], "selected@other.com")
            self.assertEqual(preview["previews"][0]["body"], "Hello Daily")
            status, result, _ = self.request("POST", "/api/email-queue/send", dict(
                message, confirmation="SEND 1"
            ), rep)
            self.assertEqual((status, result["sent"]), (200, 1))
            self.assertEqual(send.call_args.kwargs["settings"]["api_key"], "selected-secret")
            self.assertEqual(send.call_args.kwargs["settings"]["from_email"], "selected@other.com")
            # Changing the sender never permits a second delivery to the same recipient.
            self.assertEqual(self.request("POST", "/api/email-queue/send", dict(
                message, sender_id=1, confirmation="SEND 1"
            ), rep)[0], 400)
            self.assertEqual(send.call_count, 1)
        data = self.request("GET", "/api/email-recipients", cookie=rep)[1]
        self.assertEqual(data["rows"][0]["status"], "sent")
        self.assertEqual(data["history"][0]["sender"], "selected@other.com")
        crm = next(row for row in self.db.query_pipeline() if row["lead_id"] == lead)
        self.assertIsNone(crm["channel"])
        self.assertEqual(crm["status"], "pending")

    def test_email_queue_permissions_preview_and_safe_send(self):
        observer = self.login("viewer_test", "viewer-secure-password")
        self.assertEqual(self.request("POST", "/api/email-prospects/import", {
            "records": [{"email": "blocked@example.com"}]
        }, observer)[0], 403)

        admin = self.login("admin", "admin123")
        self.request("POST", "/api/password", {
            "current_password": "admin123", "new_password": "admin-secure-password"
        }, admin)
        self.assertEqual(self.request("POST", "/api/email-prospects/import", {
            "records": [{"email": "old@example.com"}]
        }, admin)[0], 410)
        lead = self.db.insert_lead({
            "company_id": self.owned_id, "contact_name": "Alex", "email": "alex@example.com"
        })
        status, payload, _ = self.request("POST", "/api/email-queue/add", {"lead_ids": [lead]}, admin)
        self.assertEqual((status, payload["added"]), (200, 1))
        self.assertEqual(self.request("POST", "/api/email-queue/add", {"lead_ids": [lead]}, admin)[1]["added"], 0)
        rows = self.request("GET", "/api/email-queue", cookie=admin)[1]["rows"]
        self.assertEqual(len(rows), 1)
        prospect_id = rows[0]["id"]
        status, payload, _ = self.request("POST", "/api/email-queue/preview", {
            "ids": [prospect_id], "subject": "Hi {{company}}",
            "body": "Hello {{name}} at {{email}}",
        }, admin)
        self.assertEqual(status, 200)
        self.assertEqual(payload["previews"][0]["subject"], "Hi 销售名下客户")
        self.assertEqual(payload["previews"][0]["body"], "Hello Alex at alex@example.com")

        self.assertEqual(self.request("POST", "/api/email-queue/send", {
            "ids": [prospect_id], "subject": "Hi", "body": "Body",
            "confirmation": "wrong",
        }, admin)[0], 400)
        with patch.dict("os.environ", {
            "RESEND_API_KEY": "test-key", "RESEND_FROM_EMAIL": "sales@example.com"
        }), patch("web.server.send_resend_batch", return_value=["msg_123"]):
            status, payload, _ = self.request("POST", "/api/email-queue/send", {
                "ids": [prospect_id], "subject": "Hi {{company}}",
                "body": "Hello {{name}}", "confirmation": "SEND 1",
            }, admin)
        self.assertEqual((status, payload["sent"], payload["failed"]), (200, 1, 0))
        self.assertEqual(self.request("POST", "/api/email-queue/send", {
            "ids": [prospect_id], "subject": "Hi", "body": "Again",
            "confirmation": "SEND 1",
        }, admin)[0], 400)
        row = next(row for row in self.db.query_pipeline() if row["lead_id"] == lead)
        self.assertIsNone(row["channel"])
        self.assertEqual(row["status"], "pending")

    def test_email_package_creates_owned_draft_without_queue_or_send(self):
        from cryptography.fernet import Fernet

        lead = self.db.insert_lead({
            "company_id": self.owned_id, "contact_name": "Package lead",
            "email": "package@example.com",
        })
        other = self.db.insert_lead({
            "company_id": self.other_id, "contact_name": "Private lead",
            "email": "private@example.com",
        })
        admin = self.login("admin", "admin123")
        self.request("POST", "/api/password", {
            "current_password": "admin123", "new_password": "admin-secure-password"
        }, admin)
        with patch.dict("os.environ", {"MAIL_CONFIG_KEY": Fernet.generate_key().decode()}):
            status, configured, _ = self.request("POST", "/api/email-senders", {
                "provider": "smtp", "from_email": "sales@example.com",
                "smtp_host": "smtp.example.com", "smtp_username": "sales@example.com",
                "smtp_password": "test-password",
            }, admin)
            self.assertEqual(status, 200)
            rep = self.login("rep_test", "rep-secure-password")
            message = {
                "title": "Founder package", "sender_id": configured["settings"]["id"],
                "lead_ids": [lead], "subject": "Hi {{company}}",
                "body": "Hello {{name}}",
            }
            with patch("web.server.send_smtp_batch") as send:
                status, result, _ = self.request("POST", "/api/email-packages", message, rep)
                self.assertEqual((status, result["success"]), (200, True))
                self.assertEqual(result["package"]["recipient_count"], 1)
                self.assertEqual(result["package"]["status"], "draft")
                send.assert_not_called()
            self.assertEqual(self.request("POST", "/api/email-packages", dict(
                message, lead_ids=[other]
            ), rep)[0], 403)
            self.assertEqual(self.request("GET", "/api/email-queue", cookie=rep)[1]["rows"], [])
            packages = self.request("GET", "/api/email-recipients", cookie=rep)[1]["packages"]
            self.assertEqual([item["title"] for item in packages], ["Founder package"])
            with patch("web.server.send_smtp_batch", return_value=["smtp-package-msg"]) as send:
                status, sent, _ = self.request("POST", "/api/email-packages/send", {
                    "id": result["package"]["id"],
                    "confirmation": f"SEND PACKAGE {result['package']['id']}",
                }, rep)
            self.assertEqual((status, sent["success"], sent["sent"]), (200, True, 1))
            self.assertEqual(send.call_args.args[0][0]["email"], "package@example.com")
            self.assertEqual(send.call_args.args[0][0]["subject"], "Hi 销售名下客户")
            updated = self.request("GET", "/api/email-recipients", cookie=rep)[1]
            self.assertEqual(updated["packages"][0]["status"], "sent")
            self.assertEqual(updated["rows"][0]["status"], "sent")
            self.assertEqual(updated["batches"][0]["package_id"], result["package"]["id"])
            self.assertEqual(updated["batches"][0]["title"], "Founder package")
            self.assertEqual(updated["batches"][0]["recipients"][0]["company_name"], "销售名下客户")
            self.assertEqual(updated["batches"][0]["recipients"][0]["email"], "package@example.com")


if __name__ == "__main__":
    unittest.main()
