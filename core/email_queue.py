"""CRM-linked email queue. Legacy standalone lists remain read-only in storage."""
import json
import os
import re
import uuid

from core.database import get_connection

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
DOMAIN_RE = re.compile(r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")


def sender_domain(email):
    return email.rsplit("@", 1)[-1].lower() if EMAIL_RE.fullmatch(email) else ""


def default_imap_host(smtp_host):
    host = str(smtp_host or "").strip().lower()
    known = {
        "smtp.qiye.aliyun.com": "imap.qiye.aliyun.com",
        "smtp.exmail.qq.com": "imap.exmail.qq.com",
        "smtp.qq.com": "imap.qq.com",
        "smtp.163.com": "imap.163.com",
    }
    if host in known:
        return known[host]
    return f"imap.{host[5:]}" if host.startswith("smtp.") else ""


SCHEMA = """
CREATE TABLE IF NOT EXISTS mail_settings (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    settings_json TEXT NOT NULL DEFAULT '{}',
    encrypted_key TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS mail_senders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    settings_json TEXT NOT NULL DEFAULT '{}',
    encrypted_key TEXT NOT NULL DEFAULT ''
);
INSERT OR IGNORE INTO mail_senders(id) VALUES(1);
CREATE TABLE IF NOT EXISTS mail_automation (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    enabled INTEGER NOT NULL DEFAULT 0,
    sender_id INTEGER NOT NULL DEFAULT 1,
    daily_limit INTEGER NOT NULL DEFAULT 20,
    send_hour INTEGER NOT NULL DEFAULT 9,
    send_minute INTEGER NOT NULL DEFAULT 0,
    timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
    subject_template TEXT NOT NULL DEFAULT '{{company}} × Aibubu：更高效的 AI 图片与视频工作流',
    body_template TEXT NOT NULL DEFAULT 'Hi {{name}},\n\nI’m reaching out from Aibubu. We provide an AI workspace and API for creating product images and short videos at scale.\n\nBest,\nAibubu Team',
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
INSERT OR IGNORE INTO mail_automation(id) VALUES(1);
CREATE TABLE IF NOT EXISTS mail_packages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    sender_id INTEGER NOT NULL DEFAULT 1,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    recipients_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'draft',
    batch_id TEXT,
    sent_at TIMESTAMP,
    created_by TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS mail_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    lead_id INTEGER NOT NULL UNIQUE REFERENCES leads(id),
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','sending','sent','unsubscribed','removed')),
    batch_id TEXT,
    created_by TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS mail_queue_sends (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    queue_id INTEGER NOT NULL REFERENCES mail_queue(id),
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    sender TEXT NOT NULL,
    provider_message_id TEXT NOT NULL,
    sent_by TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS mail_replies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sender_id INTEGER NOT NULL DEFAULT 1,
    imap_uid INTEGER NOT NULL,
    queue_id INTEGER NOT NULL REFERENCES mail_queue(id),
    company_id INTEGER NOT NULL REFERENCES companies(id),
    lead_id INTEGER NOT NULL REFERENCES leads(id),
    from_email TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    snippet TEXT NOT NULL DEFAULT '',
    message_id TEXT NOT NULL DEFAULT '',
    in_reply_to TEXT NOT NULL DEFAULT '',
    received_at TIMESTAMP NOT NULL,
    is_read INTEGER NOT NULL DEFAULT 0 CHECK(is_read IN (0,1)),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(sender_id, imap_uid)
);
CREATE INDEX IF NOT EXISTS idx_mail_replies_received
ON mail_replies(received_at DESC);
CREATE TABLE IF NOT EXISTS mail_reply_monitor_state (
    sender_id INTEGER PRIMARY KEY,
    last_uid INTEGER NOT NULL DEFAULT 0,
    last_checked_at TIMESTAMP,
    last_success_at TIMESTAMP,
    last_error TEXT NOT NULL DEFAULT ''
);
"""


class EmailQueue:
    def __init__(self, db):
        self.db = db

    def connection(self):
        return get_connection(self.db.db_path)

    def settings(self, private=False, sender_id=1):
        conn = self.connection()
        try:
            table = "mail_settings" if sender_id == 1 else "mail_senders"
            row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (sender_id,)).fetchone()
            if not row and sender_id != 1:
                raise ValueError("发件邮箱不存在")
        finally:
            conn.close()
        values = json.loads(row["settings_json"]) if row else {}
        values.setdefault("from_email", os.environ.get("RESEND_FROM_EMAIL", "").strip())
        values.setdefault("sending_domain", sender_domain(values["from_email"]))
        values.setdefault("from_name", "Aibubu")
        values.setdefault("reply_to", "")
        values.setdefault("batch_limit", 20)
        values.setdefault("provider", "resend")
        values.setdefault("smtp_host", "")
        values.setdefault("smtp_port", 465)
        values.setdefault("smtp_security", "ssl")
        values.setdefault("smtp_username", "")
        values.setdefault("imap_host", default_imap_host(values["smtp_host"]))
        values.setdefault("imap_port", 993)
        values.setdefault("imap_security", "ssl")
        values.setdefault("imap_username", values["smtp_username"])
        key = os.environ.get("RESEND_API_KEY", "").strip() if sender_id == 1 else ""
        encrypted = row["encrypted_key"] if row else ""
        encryption_ready = bool(os.environ.get("MAIL_CONFIG_KEY"))
        key_readable = bool(key) if not encrypted else encryption_ready
        if private and encrypted:
            try:
                key = self.cipher().decrypt(encrypted.encode()).decode()
            except Exception:
                raise ValueError("无法解密邮件密钥，请管理员检查 MAIL_CONFIG_KEY 或重新保存 API Key") from None
        credential_configured = bool(key or encrypted)
        if values["provider"] == "smtp":
            credential_configured = bool(encrypted and values["smtp_host"] and values["smtp_username"])
        values.update(
            id=sender_id,
            provider=values["provider"],
            key_configured=bool(key or encrypted),
            credential_configured=credential_configured,
            encryption_ready=encryption_ready,
            configured=bool(credential_configured and key_readable and EMAIL_RE.fullmatch(values["from_email"])),
        )
        if private:
            values["api_key"] = key
            values["smtp_password"] = (
                self.cipher().decrypt(encrypted.encode()).decode()
                if encrypted and values["provider"] == "smtp" else ""
            )
        return values

    def automation(self):
        conn = self.connection()
        try:
            row = conn.execute("SELECT * FROM mail_automation WHERE id=1").fetchone()
            return dict(row) if row else {}
        finally:
            conn.close()

    def save_automation(self, data):
        enabled = 1 if data.get("enabled") else 0
        sender_id = int(data.get("sender_id", 1))
        daily_limit = int(data.get("daily_limit", 20))
        hour, minute = int(data.get("send_hour", 9)), int(data.get("send_minute", 0))
        timezone = str(data.get("timezone") or "Asia/Shanghai").strip()
        if not 1 <= daily_limit <= 50:
            raise ValueError("每日自动发送上限须为 1–50")
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError("发送时间无效")
        if timezone not in {"Asia/Shanghai", "UTC", "America/Los_Angeles", "America/New_York", "Europe/London"}:
            raise ValueError("不支持的时区")
        subject = str(data.get("subject_template") or "").strip()
        body = str(data.get("body_template") or "").strip()
        if not subject or not body or len(subject) > 300 or len(body) > 50000:
            raise ValueError("自动邮件标题和正文不能为空")
        conn = self.connection()
        try:
            conn.execute("""UPDATE mail_automation SET enabled=?,sender_id=?,daily_limit=?,
                send_hour=?,send_minute=?,timezone=?,subject_template=?,body_template=?,
                updated_at=CURRENT_TIMESTAMP WHERE id=1""",
                (enabled, sender_id, daily_limit, hour, minute, timezone, subject, body))
            conn.commit()
        finally:
            conn.close()
        return self.automation()

    def create_package(self, data, actor, owner=None):
        title = str(data.get("title") or "").strip()
        subject = str(data.get("subject") or "").strip()
        body = str(data.get("body") or "").strip()
        try:
            lead_ids = list(dict.fromkeys(int(value) for value in data.get("lead_ids", [])))
            sender_id = int(data.get("sender_id", 1))
        except (TypeError, ValueError):
            raise ValueError("推送包收件人或发件邮箱无效")
        if not title or len(title) > 120:
            raise ValueError("推送包名称不能为空且最多 120 字")
        if not 1 <= len(lead_ids) <= 200:
            raise ValueError("每个推送包须选择 1–200 位联系人")
        if not subject or not body or len(subject) > 300 or len(body) > 50000:
            raise ValueError("邮件标题和正文不能为空，且不能超过长度限制")
        visible = {row["lead_id"]: row for row in self.recipients(owner)}
        if any(value not in visible for value in lead_ids):
            raise ValueError("推送包包含无权访问或无效的邮箱联系人")
        recipients = [visible[value] for value in lead_ids]
        if any(row["status"] != "pending" for row in recipients):
            raise ValueError("仅可将未发送、未退订的联系人加入推送包")
        if len({row["email"].lower() for row in recipients}) != len(recipients):
            raise ValueError("推送包中存在重复邮箱")
        date_from = str(data.get("date_from") or "").strip()
        date_to = str(data.get("date_to") or "").strip()
        for date_value in (date_from, date_to):
            if date_value and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_value):
                raise ValueError("入库日期格式应为 YYYY-MM-DD")
        if date_from and date_to and date_from > date_to:
            raise ValueError("开始日期不能晚于结束日期")
        if any(
            (date_from and str(row.get("lead_created_at") or "")[:10] < date_from)
            or (date_to and str(row.get("lead_created_at") or "")[:10] > date_to)
            for row in recipients
        ):
            raise ValueError("所选线索不在指定的入库日期范围内")
        snapshot = [{
            key: row.get(key) for key in
            ("lead_id", "email", "contact_name", "company_name", "country_type", "lead_created_at")
        } for row in recipients]
        conn = self.connection()
        try:
            cur = conn.execute(
                """INSERT INTO mail_packages
                   (title,sender_id,subject,body,recipients_json,created_by)
                   VALUES(?,?,?,?,?,?)""",
                (title, sender_id, subject, body,
                 json.dumps(snapshot, ensure_ascii=False), actor),
            )
            package_id = cur.lastrowid
            conn.commit()
        finally:
            conn.close()
        return self.get_package(package_id, owner)

    def update_package(self, package_id, data, owner=None):
        package = self.get_package(package_id, owner)
        if not package:
            raise ValueError("推送包不存在或无权访问")
        if package["status"] != "draft":
            raise ValueError("只有草稿批次可以修改")
        title = str(data.get("title") or package["title"]).strip()
        subject = str(data.get("subject") or "").strip()
        body = str(data.get("body") or "").strip()
        sender_id = int(data.get("sender_id", package["sender_id"]))
        if not title or len(title) > 120:
            raise ValueError("批次名称不能为空且最多 120 字")
        if not subject or not body or len(subject) > 300 or len(body) > 50000:
            raise ValueError("邮件标题和正文不能为空，且不能超过长度限制")
        if not self.settings(sender_id=sender_id)["configured"]:
            raise ValueError("所选发件邮箱尚未配置完整")
        conn = self.connection()
        try:
            conn.execute(
                """UPDATE mail_packages SET title=?,sender_id=?,subject=?,body=?,
                updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='draft'""",
                (title, sender_id, subject, body, int(package_id)),
            )
            conn.commit()
        finally:
            conn.close()
        return self.get_package(package_id, owner)

    def send_package_rows(self, package_id, owner=None):
        package = self.get_package(package_id, owner)
        if not package:
            raise ValueError("推送包不存在或无权访问")
        if package["status"] != "draft":
            raise ValueError("该推送包已发送或正在处理")
        settings = self.settings(private=True, sender_id=int(package["sender_id"]))
        if not settings["configured"]:
            raise ValueError("所选发件邮箱尚未配置完整")
        if len(package["recipients"]) > int(settings["batch_limit"]):
            raise ValueError(f'本发件邮箱单批最多发送 {settings["batch_limit"]} 人，请缩小批次后再发送')
        current = {row["lead_id"]: row for row in self.recipients(owner)}
        if any(
            recipient["lead_id"] not in current
            or current[recipient["lead_id"]]["email"].lower() != recipient["email"].lower()
            for recipient in package["recipients"]
        ):
            raise ValueError("批次中的联系人邮箱已变更，请重新核对并创建批次")
        lead_ids = [int(item["lead_id"]) for item in package["recipients"]]
        ids = self.prepare(lead_ids, package["created_by"], owner)
        rows = self.select(ids, owner, sendable=True)
        batch = self.claim(ids)
        conn = self.connection()
        try:
            conn.execute(
                """UPDATE mail_packages SET status='sending',batch_id=?,
                updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='draft'""",
                (batch, int(package_id)),
            )
            conn.commit()
        finally:
            conn.close()
        messages = []
        for row in rows:
            fields = {
                "company": row.get("company_name") or "your team",
                "name": row.get("contact_name") or "there",
                "email": row.get("email") or "",
            }
            subject, body = package["subject"], package["body"]
            for key, value in fields.items():
                subject = subject.replace("{{" + key + "}}", str(value))
                body = body.replace("{{" + key + "}}", str(value))
            messages.append({**row, "subject": subject, "body": body})
        return package, settings, batch, messages

    def mark_package_sent(self, package_id, batch_id):
        conn = self.connection()
        try:
            conn.execute(
                """UPDATE mail_packages SET status='sent',batch_id=?,sent_at=CURRENT_TIMESTAMP,
                   updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='sending'""",
                (str(batch_id), int(package_id)),
            )
            conn.commit()
        finally:
            conn.close()

    def get_package(self, package_id, owner=None):
        conn = self.connection()
        try:
            row = conn.execute(
                "SELECT * FROM mail_packages WHERE id=?", (int(package_id),)
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        package = dict(row)
        package["recipients"] = json.loads(package.pop("recipients_json") or "[]")
        if owner is not None and any(
            recipient["lead_id"] not in {
                item["lead_id"] for item in self.recipients(owner)
            }
            for recipient in package["recipients"]
        ):
            return None
        package["recipient_count"] = len(package["recipients"])
        return package

    def packages(self, owner=None):
        conn = self.connection()
        try:
            rows = conn.execute(
                "SELECT id FROM mail_packages ORDER BY id DESC LIMIT 100"
            ).fetchall()
            ids = [row["id"] for row in rows]
        finally:
            conn.close()
        results = []
        for package_id in ids:
            package = self.get_package(package_id, owner)
            if package is not None:
                results.append(package)
        return results

    def delivery_batches(self, owner=None):
        """Group immutable send records by their queue claim batch."""
        conn = self.connection()
        try:
            rows = [dict(row) for row in conn.execute(
                """SELECT s.id,s.subject,s.sender,s.sent_by,s.provider_message_id,
                s.created_at,q.email,q.batch_id,q.status AS queue_status,
                q.updated_at AS attempted_at,c.name AS company_name,c.country_type,l.contact_name
                FROM mail_queue q LEFT JOIN mail_queue_sends s ON s.queue_id=q.id
                JOIN companies c ON c.id=q.company_id JOIN leads l ON l.id=q.lead_id
                WHERE (? IS NULL OR c.owner=?) AND (s.id IS NOT NULL OR q.batch_id IS NOT NULL)
                ORDER BY coalesce(s.created_at,q.updated_at) DESC,q.id DESC""",
                (owner, owner),
            ).fetchall()]
        finally:
            conn.close()
        packages = {
            package["batch_id"]: package
            for package in self.packages(owner)
            if package.get("batch_id")
        }
        batches = {}
        for row in rows:
            key = row.get("batch_id") or f'legacy-{row["id"]}'
            batch = batches.setdefault(key, {
                "batch_id": key,
                "package_id": packages.get(key, {}).get("id"),
                "title": packages.get(key, {}).get("title") or f'发送批次 {str(key)[:8]}',
                "status": "sent",
                "sender": row["sender"] or "",
                "subject": row["subject"] or packages.get(key, {}).get("subject", ""),
                "sent_by": row["sent_by"] or packages.get(key, {}).get("created_by", ""),
                "sent_at": row["created_at"] or row["attempted_at"],
                "recipients": [],
            })
            recipient = {
                field: row.get(field) for field in (
                    "email", "company_name", "country_type", "contact_name",
                    "provider_message_id", "created_at",
                )
            }
            recipient["status"] = "sent" if row["id"] is not None else "sending"
            if recipient["status"] != "sent":
                batch["status"] = "sending"
            if row["sender"]:
                batch["sender"] = row["sender"]
            batch["recipients"].append(recipient)
        results = list(batches.values())
        for batch in results:
            batch["recipient_count"] = len(batch["recipients"])
            batch["sent_count"] = sum(row["status"] == "sent" for row in batch["recipients"])
        return results[:100]

    @staticmethod
    def cipher():
        from cryptography.fernet import Fernet
        key = os.environ.get("MAIL_CONFIG_KEY", "").strip()
        if not key:
            raise ValueError("请先在部署环境设置 MAIL_CONFIG_KEY，或使用 RESEND_API_KEY 环境变量")
        try:
            return Fernet(key.encode())
        except ValueError:
            raise ValueError("MAIL_CONFIG_KEY 必须是有效的 Fernet 主密钥") from None

    def senders(self):
        conn = self.connection()
        try:
            ids = [row["id"] for row in conn.execute("SELECT id FROM mail_senders ORDER BY id").fetchall()]
        finally:
            conn.close()
        return [self.settings(sender_id=value) for value in ids]

    def save_settings(self, data, sender_id=1):
        values = {
            field: str(data.get(field) or "").strip()
            for field in ("from_email", "from_name", "reply_to", "smtp_host", "smtp_username", "smtp_security",
                          "imap_host", "imap_username", "imap_security")
        }
        if not EMAIL_RE.fullmatch(values["from_email"]):
            raise ValueError("请填写有效的发件邮箱")
        # Older clients omit the domain; derive it only for backward compatibility.
        domain = str(data.get("sending_domain", sender_domain(values["from_email"])) or "").strip().lower()
        if not DOMAIN_RE.fullmatch(domain):
            raise ValueError("请填写有效的发件域名，例如 mail.example.com，不要包含 https:// 或路径")
        if sender_domain(values["from_email"]) != domain:
            raise ValueError("发件邮箱的 @ 后域名必须与发件域名一致")
        values["sending_domain"] = domain
        if values["reply_to"] and not EMAIL_RE.fullmatch(values["reply_to"]):
            raise ValueError("回复邮箱格式无效")
        if any("\r" in value or "\n" in value for value in values.values()):
            raise ValueError("配置项不能包含换行")
        values["batch_limit"] = int(data.get("batch_limit", 20))
        if not 1 <= values["batch_limit"] <= 50:
            raise ValueError("单批数量须为 1–50")
        values["provider"] = str(data.get("provider") or "resend").strip().lower()
        if values["provider"] not in {"resend", "smtp"}:
            raise ValueError("不支持的邮件服务")
        values["smtp_port"] = int(data.get("smtp_port", 465))
        values["smtp_security"] = values["smtp_security"] or "ssl"
        values["imap_host"] = values["imap_host"] or default_imap_host(values["smtp_host"])
        values["imap_username"] = values["imap_username"] or values["smtp_username"]
        values["imap_security"] = values["imap_security"] or "ssl"
        values["imap_port"] = int(data.get("imap_port", 993))
        if not 1 <= values["smtp_port"] <= 65535:
            raise ValueError("SMTP端口无效")
        if values["smtp_security"] not in {"ssl", "starttls", "none"}:
            raise ValueError("SMTP加密方式无效")
        if not 1 <= values["imap_port"] <= 65535:
            raise ValueError("IMAP端口无效")
        if values["imap_security"] not in {"ssl", "starttls", "none"}:
            raise ValueError("IMAP加密方式无效")
        if values["provider"] == "smtp" and (not values["smtp_host"] or not values["smtp_username"]):
            raise ValueError("SMTP模式需要填写服务器和账号")
        secret = data.get("smtp_password") if values["provider"] == "smtp" else data.get("api_key")
        encrypted = None
        if secret:
            encrypted = self.cipher().encrypt(str(secret).strip().encode()).decode()
        conn = self.connection()
        try:
            if sender_id is None:
                cur = conn.execute(
                    "INSERT INTO mail_senders(settings_json,encrypted_key) VALUES(?,?)",
                    (json.dumps(values), encrypted or ""),
                )
                sender_id = cur.lastrowid
            else:
                if sender_id != 1 and not conn.execute("SELECT id FROM mail_senders WHERE id=?", (sender_id,)).fetchone():
                    raise ValueError("发件邮箱不存在")
                table = "mail_settings" if sender_id == 1 else "mail_senders"
                conn.execute(
                    f"""INSERT INTO {table}(id,settings_json,encrypted_key) VALUES(?,?,?)
                ON CONFLICT(id) DO UPDATE SET settings_json=excluded.settings_json,
                encrypted_key=CASE WHEN ? IS NULL THEN {table}.encrypted_key ELSE ? END""",
                    (sender_id, json.dumps(values), encrypted or "", encrypted, encrypted),
                )
            conn.commit()
        finally:
            conn.close()
        return self.settings(sender_id=sender_id)

    def recipients(self, owner=None):
        """Read CRM contacts without changing CRM data or sales stages."""
        conn = self.connection()
        try:
            leads = conn.execute(
                """SELECT l.id AS lead_id,l.email,l.contact_name,
                l.created_at AS lead_created_at,
                c.id AS company_id,c.name AS company_name,
                c.country_type
                FROM leads l JOIN companies c ON c.id=l.company_id
                WHERE (? IS NULL OR c.owner=?) ORDER BY l.id DESC""", (owner, owner)
            ).fetchall()
            queued = conn.execute(
                """SELECT q.*,
                (SELECT max(created_at) FROM mail_queue_sends s WHERE s.queue_id=q.id) AS last_sent_at,
                (SELECT sender FROM mail_queue_sends s WHERE s.queue_id=q.id ORDER BY s.id DESC LIMIT 1) AS last_sender
                FROM mail_queue q"""
            ).fetchall()
            legacy = {str(row["email"]).strip().lower(): row["status"] for row in conn.execute(
                "SELECT email,status FROM email_prospects WHERE status IN ('sent','unsubscribed')"
            ).fetchall()}
        finally:
            conn.close()
        by_email = {row["email"].lower(): dict(row) for row in queued}
        by_lead = {row["lead_id"]: dict(row) for row in queued}
        result = []
        for lead in leads:
            row = dict(lead)
            email = str(row["email"] or "").strip().lower()
            if not EMAIL_RE.fullmatch(email):
                continue
            previous = by_email.get(email) or by_lead.get(row["lead_id"])
            row.update(email=email, status="pending", queue_id=None, last_sent_at=None, last_sender=None)
            if previous:
                row.update(status=previous["status"], queue_id=previous["id"],
                           last_sent_at=previous["last_sent_at"], last_sender=previous["last_sender"])
                if previous["email"].lower() != email:
                    row["status"] = "email_changed"
                elif previous["last_sent_at"] and previous["status"] != "unsubscribed":
                    row["status"] = "sent"
            if email in legacy:
                row["status"] = legacy[email]
            result.append(row)
        return result

    def prepare(self, lead_ids, actor, owner=None):
        visible = {row["lead_id"]: row for row in self.recipients(owner)}
        if not lead_ids or any(value not in visible for value in lead_ids):
            raise ValueError("请选择有权访问的邮箱联系人")
        rows = [visible[value] for value in lead_ids]
        if any(row["status"] != "pending" for row in rows):
            raise ValueError("仅未推送联系人可发送，已推送、退订或待核实联系人已拦截")
        if len({row["email"] for row in rows}) != len(rows):
            raise ValueError("所选联系人包含重复邮箱，请每个邮箱只选一人")
        for row in rows:
            if row["queue_id"] is None:
                try:
                    self.add(row["lead_id"], actor)
                except ValueError:
                    # Another request may have prepared this address already.
                    pass
        current = {row["lead_id"]: row for row in self.recipients(owner)}
        ids = [current[value]["queue_id"] for value in lead_ids]
        if any(value is None for value in ids):
            raise ValueError("邮箱重复或状态已变化，请刷新收件人列表")
        self.select(ids, owner, sendable=True)
        return ids

    def history(self, owner=None):
        conn = self.connection()
        try:
            return [dict(row) for row in conn.execute(
                """SELECT s.*,q.email,c.name AS company_name,c.country_type,l.contact_name
                FROM mail_queue_sends s JOIN mail_queue q ON q.id=s.queue_id
                JOIN companies c ON c.id=q.company_id JOIN leads l ON l.id=q.lead_id
                WHERE (? IS NULL OR c.owner=?) ORDER BY s.id DESC LIMIT 500""", (owner, owner)
            ).fetchall()]
        finally:
            conn.close()

    def sent_count_today(self):
        conn = self.connection()
        try:
            return int(conn.execute(
                "SELECT count(*) FROM mail_queue_sends WHERE date(created_at)=date('now')"
            ).fetchone()[0])
        finally:
            conn.close()

    def rows(self, owner=None):
        conn = self.connection()
        try:
            rows = conn.execute(
                """SELECT q.*, c.name AS company_name, c.country_type, c.owner,
                l.contact_name, l.email AS current_email,
                (SELECT max(created_at) FROM mail_queue_sends s WHERE s.queue_id=q.id) AS last_sent_at
                FROM mail_queue q JOIN companies c ON c.id=q.company_id
                JOIN leads l ON l.id=q.lead_id
                WHERE (? IS NULL OR c.owner=?) ORDER BY q.id DESC""",
                (owner, owner),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def add(self, lead_id, actor):
        conn = self.connection()
        try:
            row = conn.execute("SELECT id,company_id,email FROM leads WHERE id=?", (lead_id,)).fetchone()
            email = str(row["email"] or "").strip().lower() if row else ""
            if not EMAIL_RE.fullmatch(email):
                raise ValueError("联系人没有有效邮箱")
            # Honor old unsubscribes and sent records, without migrating unattached lists.
            old = conn.execute(
                "SELECT status FROM email_prospects WHERE email=? AND status IN ('sent','unsubscribed')",
                (email,),
            ).fetchone()
            if old:
                raise ValueError("该邮箱在旧名单中已发送或已退订")
            existing = conn.execute(
                "SELECT status FROM mail_queue WHERE lead_id=? OR email=?", (lead_id, email)
            ).fetchone()
            if existing:
                raise ValueError("联系人或邮箱已在队列中，请勿重复加入")
            conn.execute(
                "INSERT INTO mail_queue(company_id,lead_id,email,created_by) VALUES(?,?,?,?)",
                (row["company_id"], lead_id, email, actor),
            )
            conn.commit()
        finally:
            conn.close()

    def select(self, ids, owner=None, sendable=False):
        visible = {row["id"]: row for row in self.rows(owner)}
        if not ids or any(value not in visible for value in ids):
            raise ValueError("请选择有权访问的队列项")
        rows = [visible[value] for value in ids]
        allowed = {row["lead_id"] for row in self.recipients(owner) if row["status"] == "pending"} if sendable else set()
        for row in rows:
            if sendable and str(row["current_email"] or "").strip().lower() != row["email"]:
                raise ValueError("客户邮箱已变更，请先核实队列邮箱，不能发送旧地址")
            if sendable and row["status"] != "pending":
                raise ValueError("仅待发送项可发送，已发送、发送中、退订或移除项已拦截")
            if sendable and row["lead_id"] not in allowed:
                raise ValueError("邮箱已发送或退订，不能重复推送")
        return rows

    def status(self, queue_id, status, owner=None):
        self.select([queue_id], owner)
        if status not in ("removed", "unsubscribed"):
            raise ValueError("仅支持移除或退订，不允许重置已发送记录")
        conn = self.connection()
        try:
            conn.execute(
                "UPDATE mail_queue SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=? AND status!='sending'",
                (status, queue_id),
            )
            changed = conn.execute(
                "SELECT count(*) AS total FROM mail_queue WHERE id=? AND status=?",
                (queue_id, status),
            ).fetchone()["total"]
            if changed != 1:
                conn.rollback()
                raise ValueError("发送中的记录不能修改")
            conn.commit()
        finally:
            conn.close()

    def claim(self, ids):
        # One conditional statement claims the entire batch, including on libSQL.
        batch = str(uuid.uuid4())
        marks = ",".join("?" for _ in ids)
        conn = self.connection()
        try:
            conn.execute(
                f"""UPDATE mail_queue SET status='sending',batch_id=?,updated_at=CURRENT_TIMESTAMP
                WHERE id IN ({marks}) AND status='pending'
                AND (SELECT count(*) FROM mail_queue WHERE id IN ({marks}) AND status='pending')=?""",
                (batch, *ids, *ids, len(ids)),
            )
            claimed = conn.execute(
                f"""SELECT count(*) AS total FROM mail_queue
                WHERE id IN ({marks}) AND status='sending' AND batch_id=?""",
                (*ids, batch),
            ).fetchone()["total"]
            if claimed != len(ids):
                conn.rollback()
                raise ValueError("队列状态已变化，已阻止重复发送")
            conn.commit()
        finally:
            conn.close()
        return batch

    def recover_unsent(self, queue_id, owner=None):
        row = self.select([queue_id], owner)[0]
        if row["status"] != "sending":
            raise ValueError("仅发送中的记录可以恢复")
        conn = self.connection()
        try:
            sent = conn.execute(
                "SELECT count(*) AS total FROM mail_queue_sends WHERE queue_id=?",
                (queue_id,),
            ).fetchone()["total"]
            if sent:
                raise ValueError("已有发送记录，不能恢复以免重复发送")
            conn.execute(
                """UPDATE mail_queue SET status='pending',batch_id=NULL,updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND status='sending'
                AND NOT EXISTS (SELECT 1 FROM mail_queue_sends WHERE queue_id=?)""",
                (queue_id, queue_id),
            )
            recovered = conn.execute(
                """SELECT count(*) AS total FROM mail_queue
                WHERE id=? AND status='pending' AND batch_id IS NULL""",
                (queue_id,),
            ).fetchone()["total"]
            if recovered != 1:
                conn.rollback()
                raise ValueError("队列状态已变化，恢复失败")
            conn.commit()
        finally:
            conn.close()

    def complete(self, row, subject, body, sender, provider_id, actor):
        conn = self.connection()
        try:
            send = conn.execute(
                """INSERT INTO mail_queue_sends
                (queue_id,subject,body,sender,provider_message_id,sent_by) VALUES(?,?,?,?,?,?)""",
                (row["id"], subject, body, sender, provider_id, actor),
            )
            conn.execute(
                "UPDATE mail_queue SET status='sent',updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (row["id"],),
            )
            from core.feishu_sync import record_touch_event, set_channel_state
            source_key = f"mail-send:{send.lastrowid}"
            record_touch_event(
                conn, row["company_id"], row["lead_id"], "cold_email", "sent",
                "smtp", source_key, payload={"provider_message_id": provider_id},
            )
            set_channel_state(
                conn, row["company_id"], row["lead_id"], "cold_email", "sent",
                "smtp", source_key,
            )
            conn.commit()
        finally:
            conn.close()
