"""Read-only IMAP reply monitoring linked to CRM email sends."""
import email
import html
import imaplib
import re
import ssl
from datetime import datetime, timezone
from email.header import decode_header
from email.utils import parseaddr, parsedate_to_datetime

from core.database import get_connection
from core.email_queue import EmailQueue, default_imap_host


def decode_text(value):
    parts = []
    for item, charset in decode_header(str(value or "")):
        if isinstance(item, bytes):
            try:
                parts.append(item.decode(charset or "utf-8", errors="replace"))
            except LookupError:
                parts.append(item.decode("utf-8", errors="replace"))
        else:
            parts.append(item)
    return "".join(parts).strip()


def message_snippet(message, limit=500):
    plain, rich = [], []
    parts = message.walk() if message.is_multipart() else [message]
    for part in parts:
        if part.get_content_disposition() == "attachment":
            continue
        content_type = part.get_content_type()
        if content_type not in {"text/plain", "text/html"}:
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            text = payload.decode(charset, errors="replace")
        except LookupError:
            text = payload.decode("utf-8", errors="replace")
        if content_type == "text/plain":
            plain.append(text)
        else:
            rich.append(re.sub(r"<[^>]+>", " ", html.unescape(text)))
    text = "\n".join(plain or rich)
    text = re.split(r"(?im)^\s*(?:on .+ wrote:|from:|发件人[:：])\s*$", text, maxsplit=1)[0]
    return re.sub(r"\s+", " ", text).strip()[:limit]


def received_at(value):
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OverflowError):
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class EmailReplyMonitor:
    def __init__(self, db, imap_factory=None):
        self.db = db
        self.imap_factory = imap_factory

    def connection(self):
        return get_connection(self.db.db_path)

    def status(self):
        conn = self.connection()
        try:
            rows = conn.execute(
                "SELECT * FROM mail_reply_monitor_state ORDER BY sender_id"
            ).fetchall()
            unread = conn.execute(
                "SELECT count(*) AS total FROM mail_replies WHERE is_read=0"
            ).fetchone()["total"]
            return {"senders": [dict(row) for row in rows], "unread": int(unread)}
        finally:
            conn.close()

    def replies(self, owner=None, limit=200):
        conn = self.connection()
        try:
            rows = conn.execute(
                """SELECT r.*,c.name AS company_name,c.owner,l.contact_name,q.email
                   FROM mail_replies r
                   JOIN mail_queue q ON q.id=r.queue_id
                   JOIN companies c ON c.id=r.company_id
                   JOIN leads l ON l.id=r.lead_id
                   WHERE (? IS NULL OR c.owner=?)
                   ORDER BY r.received_at DESC,r.id DESC LIMIT ?""",
                (owner, owner, int(limit)),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def mark_read(self, ids, owner=None):
        ids = list(dict.fromkeys(int(value) for value in ids))
        if not ids:
            raise ValueError("请选择要标记的回复")
        marks = ",".join("?" for _ in ids)
        conn = self.connection()
        try:
            visible = conn.execute(
                f"""SELECT r.id FROM mail_replies r
                    JOIN companies c ON c.id=r.company_id
                    WHERE r.id IN ({marks}) AND (? IS NULL OR c.owner=?)""",
                (*ids, owner, owner),
            ).fetchall()
            if len(visible) != len(ids):
                raise ValueError("回复不存在或无权访问")
            conn.execute(
                f"UPDATE mail_replies SET is_read=1 WHERE id IN ({marks})",
                tuple(ids),
            )
            conn.commit()
        finally:
            conn.close()

    def _sent_contacts(self, conn):
        rows = conn.execute(
            """SELECT q.id AS queue_id,q.company_id,q.lead_id,lower(q.email) AS email,
                      max(s.created_at) AS last_sent_at
               FROM mail_queue q JOIN mail_queue_sends s ON s.queue_id=q.id
               GROUP BY q.id,q.company_id,q.lead_id,q.email"""
        ).fetchall()
        return {row["email"]: dict(row) for row in rows}

    def _client(self, host, port, security):
        if self.imap_factory:
            return self.imap_factory(host, port, security)
        if security == "ssl":
            return imaplib.IMAP4_SSL(
                host, port, ssl_context=ssl.create_default_context(), timeout=20
            )
        client = imaplib.IMAP4(host, port, timeout=20)
        if security == "starttls":
            client.starttls(ssl_context=ssl.create_default_context())
        return client

    def poll_sender(self, sender_id=1):
        settings = EmailQueue(self.db).settings(private=True, sender_id=sender_id)
        if settings.get("provider") != "smtp":
            raise ValueError("回复监控仅支持自有 SMTP 企业邮箱")
        host = str(settings.get("imap_host") or default_imap_host(settings.get("smtp_host"))).strip()
        port = int(settings.get("imap_port") or 993)
        security = str(settings.get("imap_security") or "ssl").strip().lower()
        username = str(settings.get("imap_username") or settings.get("smtp_username") or "").strip()
        password = settings.get("smtp_password") or ""
        if not host or not username or not password or security not in {"ssl", "starttls", "none"}:
            raise ValueError("IMAP 回复监控配置不完整")

        conn = self.connection()
        client = None
        scanned = matched = created = 0
        try:
            state = conn.execute(
                "SELECT * FROM mail_reply_monitor_state WHERE sender_id=?",
                (sender_id,),
            ).fetchone()
            last_uid = int(state["last_uid"] if state else 0)
            contacts = self._sent_contacts(conn)
            client = self._client(host, port, security)
            client.login(username, password)
            status, _ = client.select("INBOX", readonly=True)
            if status != "OK":
                raise ValueError("无法以只读方式打开收件箱")
            status, data = client.uid("search", None, f"UID {last_uid + 1}:*")
            if status != "OK":
                raise ValueError("无法查询收件箱 UID")
            uids = [int(value) for value in (data[0] or b"").split() if value.isdigit()]
            if not last_uid and len(uids) > 1000:
                uids = uids[-1000:]
            max_uid = last_uid
            for uid in uids:
                max_uid = max(max_uid, uid)
                status, payload = client.uid("fetch", str(uid), "(BODY.PEEK[])")
                if status != "OK" or not payload:
                    continue
                raw = next(
                    (item[1] for item in payload if isinstance(item, tuple) and len(item) > 1),
                    None,
                )
                if not raw:
                    continue
                scanned += 1
                message = email.message_from_bytes(raw)
                sender = parseaddr(message.get("From") or "")[1].strip().lower()
                contact = contacts.get(sender)
                if not contact:
                    continue
                received = received_at(message.get("Date"))
                if received < str(contact["last_sent_at"] or "")[:19]:
                    continue
                auto_submitted = str(message.get("Auto-Submitted") or "").strip().lower()
                precedence = str(message.get("Precedence") or "").strip().lower()
                if (
                    (auto_submitted and auto_submitted != "no")
                    or precedence in {"bulk", "junk", "list"}
                    or message.get("List-Unsubscribe")
                ):
                    continue
                matched += 1
                exists = conn.execute(
                    "SELECT id FROM mail_replies WHERE sender_id=? AND imap_uid=?",
                    (sender_id, uid),
                ).fetchone()
                if exists:
                    continue
                conn.execute(
                    """INSERT INTO mail_replies
                       (sender_id,imap_uid,queue_id,company_id,lead_id,from_email,
                        subject,snippet,message_id,in_reply_to,received_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        sender_id, uid, contact["queue_id"], contact["company_id"],
                        contact["lead_id"], sender, decode_text(message.get("Subject")),
                        message_snippet(message), str(message.get("Message-ID") or "").strip(),
                        str(message.get("In-Reply-To") or "").strip(), received,
                    ),
                )
                created += 1
            conn.execute(
                """INSERT INTO mail_reply_monitor_state
                   (sender_id,last_uid,last_checked_at,last_success_at,last_error)
                   VALUES(?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, '')
                   ON CONFLICT(sender_id) DO UPDATE SET
                   last_uid=excluded.last_uid,last_checked_at=CURRENT_TIMESTAMP,
                   last_success_at=CURRENT_TIMESTAMP,last_error=''""",
                (sender_id, max_uid),
            )
            conn.commit()
            return {
                "sender_id": sender_id, "scanned": scanned,
                "matched": matched, "created": created, "last_uid": max_uid,
            }
        except Exception as error:
            conn.rollback()
            conn.execute(
                """INSERT INTO mail_reply_monitor_state
                   (sender_id,last_checked_at,last_error)
                   VALUES(?,CURRENT_TIMESTAMP,?)
                   ON CONFLICT(sender_id) DO UPDATE SET
                   last_checked_at=CURRENT_TIMESTAMP,last_error=excluded.last_error""",
                (sender_id, str(error)[:500]),
            )
            conn.commit()
            raise
        finally:
            if client is not None:
                try:
                    client.logout()
                except Exception:
                    pass
            conn.close()

    def poll_all(self):
        results = []
        for sender in EmailQueue(self.db).senders():
            if sender.get("provider") == "smtp" and sender.get("configured"):
                results.append(self.poll_sender(int(sender["id"])))
        return {
            "senders": len(results),
            "scanned": sum(item["scanned"] for item in results),
            "matched": sum(item["matched"] for item in results),
            "created": sum(item["created"] for item in results),
            "results": results,
        }
