"""Read-only Feishu mail-sheet sync and idempotent CRM import."""
import json
import hashlib
import os
import re
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit


DEFAULT_SPREADSHEET_TOKEN = "CuTNs47dqhohkTtGH13cQirdnge"
DEFAULT_SHEET_ID = "U8RTY1"
DEFAULT_X_SHEET_ID = "bca5fd"


class FeishuImportError(ValueError):
    pass


def normalize_company_name(value: Any) -> str:
    """Fold case, Unicode and punctuation without dropping meaningful words."""
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(char for char in text if char.isalnum())


def normalize_domain(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    parsed = urlsplit(raw if "://" in raw else f"https://{raw}")
    host = (parsed.hostname or "").casefold().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def parse_market(value: Any) -> str:
    market = str(value or "").strip().casefold()
    if market in {"海外", "国外", "overseas", "international"}:
        return "overseas"
    if market in {"国内", "domestic", "中国"}:
        return "domestic"
    return "pending_verification"


def parse_sheet_rows(values: Any) -> List[Dict[str, Any]]:
    if not isinstance(values, list) or not values:
        raise FeishuImportError("飞书邮件表没有可读取的数据")
    headers = [str(value or "").strip() for value in values[0]]
    required = {"姓名", "公司", "邮箱"}
    if not required.issubset(set(headers)):
        raise FeishuImportError("表头缺少姓名、公司或邮箱列")
    rows = []
    for offset, raw in enumerate(values[1:], start=2):
        cells = list(raw) if isinstance(raw, list) else []
        record = {header: cells[index] if index < len(cells) else "" for index, header in enumerate(headers)}
        if not any(str(value or "").strip() for value in record.values()):
            continue
        rows.append({
            "sheet_row": offset,
            "contact_name": str(record.get("姓名") or "").strip(),
            "company_name": str(record.get("公司") or "").strip(),
            "job_title": str(record.get("职位") or "").strip(),
            "contact_role_category": str(record.get("类别") or "").strip(),
            "email": str(record.get("邮箱") or "").strip().casefold(),
            "x_url": str(record.get("X链接") or "").strip(),
            "website": str(record.get("官网") or "").strip(),
            "notes": str(record.get("找客理由") or "").strip(),
            "source": str(record.get("邮箱来源") or "").strip(),
            "market": parse_market(record.get("目标市场")),
            "mail_status": str(record.get("状态") or "").strip(),
        })
    return rows


class FeishuMailSheetImporter:
    def __init__(self, db, environ=None, opener=None):
        self.db = db
        self.environ = os.environ if environ is None else environ
        self.opener = opener or urllib.request.urlopen
        self.spreadsheet_token = self.environ.get(
            "FEISHU_SPREADSHEET_TOKEN", DEFAULT_SPREADSHEET_TOKEN
        ).strip()
        self.sheet_id = self.environ.get("FEISHU_SHEET_ID", DEFAULT_SHEET_ID).strip()

    def configured(self) -> bool:
        return bool(
            self.environ.get("FEISHU_APP_ID", "").strip()
            and self.environ.get("FEISHU_APP_SECRET", "").strip()
            and self.spreadsheet_token
            and self.sheet_id
        )

    def _json_request(self, url, method="GET", payload=None, token=None):
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with self.opener(request, timeout=20) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            raise FeishuImportError(f"飞书接口请求失败（HTTP {error.code}）") from error
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
            raise FeishuImportError("飞书接口暂时不可用，请稍后重试") from error
        if not isinstance(data, dict) or data.get("code") not in (0, None):
            raise FeishuImportError("飞书接口返回错误，请检查应用权限和表格授权")
        return data

    def fetch_records(self) -> List[Dict[str, Any]]:
        app_id = self.environ.get("FEISHU_APP_ID", "").strip()
        app_secret = self.environ.get("FEISHU_APP_SECRET", "").strip()
        if not self.configured():
            raise FeishuImportError("尚未配置飞书应用凭证，请先设置 FEISHU_APP_ID 和 FEISHU_APP_SECRET")
        token_result = self._json_request(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            method="POST",
            payload={"app_id": app_id, "app_secret": app_secret},
        )
        tenant_token = (token_result.get("tenant_access_token")
                        or token_result.get("data", {}).get("tenant_access_token"))
        if not tenant_token:
            raise FeishuImportError("飞书应用认证失败，请检查应用凭证")
        range_name = f"{self.sheet_id}!A1:N1000"
        encoded_range = urllib.parse.quote(range_name, safe="!:")
        url = (
            "https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/"
            f"{urllib.parse.quote(self.spreadsheet_token, safe='')}/values/{encoded_range}"
            "?valueRenderOption=ToString"
        )
        result = self._json_request(url, token=tenant_token)
        data = result.get("data") or {}
        values = data.get("valueRange", {}).get("values") or data.get("values")
        if values is None:
            raise FeishuImportError("飞书返回的数据格式不符合预期")
        return parse_sheet_rows(values)

    def _existing_companies(self, cursor):
        return [dict(row) for row in cursor.execute(
            """SELECT id,name,website,country_type,market_classification,industry,scale,
                      city_or_region,source_channel,notes,company_description,description_source,
                      discovery_url,customer_type,models_used,qualification_evidence
               FROM companies"""
        ).fetchall()]

    def _existing_leads(self, cursor):
        result = defaultdict(list)
        for row in cursor.execute(
            """SELECT l.id,l.company_id,l.email,c.name AS company_name
               FROM leads l JOIN companies c ON c.id=l.company_id
               WHERE trim(coalesce(l.email,''))<>''"""
        ).fetchall():
            result[str(row["email"]).strip().casefold()].append(dict(row))
        return result

    def plan(self, records: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Build a non-mutating import plan with company and email collision checks."""
        conn = self.db.connection() if hasattr(self.db, "connection") else None
        if conn is None:
            from core.database import get_connection
            conn = get_connection(self.db.db_path)
        try:
            cursor = conn.cursor()
            companies = self._existing_companies(cursor)
            existing_leads = self._existing_leads(cursor)
            status_sync = self._status_sync_plan(cursor, records)
        finally:
            conn.close()

        by_name, by_domain = defaultdict(list), defaultdict(list)
        for company in companies:
            key = normalize_company_name(company.get("name"))
            domain = normalize_domain(company.get("website"))
            if key:
                by_name[key].append(company)
            if domain:
                by_domain[domain].append(company)

        staged_companies = {}
        staged_company_contributions = defaultdict(list)
        staged_company_keys = {}
        staged_emails = {}
        entries, conflicts = [], []
        for record in records:
            row_no = record.get("sheet_row")
            email = str(record.get("email") or "").strip().casefold()
            name = str(record.get("company_name") or "").strip()
            name_key, domain = normalize_company_name(name), normalize_domain(record.get("website"))
            if not name or not name_key or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
                conflicts.append({"row": row_no, "company": name, "email": email,
                                  "reason": "公司名或有效邮箱缺失"})
                continue

            matched = {company["id"]: company for company in by_name.get(name_key, [])}
            if domain:
                matched.update({company["id"]: company for company in by_domain.get(domain, [])})
            if len(matched) > 1:
                conflicts.append({"row": row_no, "company": name, "email": email,
                                  "reason": "公司名/官网域名命中多个CRM公司，需人工核对"})
                continue

            if matched:
                staged_key = ("id", next(iter(matched)))
            elif name_key in staged_company_keys:
                staged_key = staged_company_keys[name_key]
            elif domain and domain in staged_company_keys:
                staged_key = staged_company_keys[domain]
            else:
                staged_key = ("new", name_key)
            if staged_key[0] == "new":
                staged_company = {
                    "name": name, "website": str(record.get("website") or "").strip(),
                    "country_type": record.get("market") or "pending_verification",
                }
                staged_companies[staged_key[1]] = staged_company
                staged_company_contributions[staged_key[1]].append(record)
                staged_company_keys[name_key] = staged_key
                if domain:
                    staged_company_keys[domain] = staged_key
            else:
                staged_company = staged_companies.get(staged_key[1]) if staged_key[0] == "new" else matched[staged_key[1]]
                if staged_key[0] == "new":
                    staged_company_contributions[staged_key[1]].append(record)

            if email in existing_leads:
                linked_names = sorted({lead["company_name"] for lead in existing_leads[email]})
                conflicts.append({
                    "row": row_no, "company": name, "email": email,
                    "reason": (
                        "邮箱已关联其他CRM公司，跳过避免误合并"
                        if linked_names and normalize_company_name(name) not in {
                            normalize_company_name(value) for value in linked_names
                        }
                        else "邮箱已存在于CRM，联系人不重复导入"
                    ),
                })
                continue
            if email in staged_emails:
                prior = staged_emails[email]
                prior_key = prior["company_key"]
                if prior_key != staged_key:
                    conflicts.append({"row": row_no, "company": name, "email": email,
                                      "reason": "同一邮箱在飞书表中关联多个公司，需人工核对"})
                else:
                    conflicts.append({"row": row_no, "company": name, "email": email,
                                      "reason": f"表内重复邮箱，已由第 {prior['row']} 行保留"})
                continue
            entry = dict(record)
            entry.update(company_key=staged_key, company=staged_company, action="create_company" if staged_key[0] == "new" else "reuse_company")
            entries.append(entry)
            staged_emails[email] = {"company_key": staged_key, "row": row_no}

        new_company_count = len({entry["company_key"] for entry in entries if entry["action"] == "create_company"})
        matched_company_count = len({entry["company_key"] for entry in entries if entry["action"] == "reuse_company"})
        for entry in entries:
            if entry["company_key"][0] == "new":
                entry["company_records"] = staged_company_contributions[entry["company_key"][1]]
        fingerprint_rows = [
            {
                "row": entry.get("sheet_row"), "email": entry.get("email"),
                "company": entry.get("company_name"), "website": entry.get("website"),
                "action": entry.get("action"), "company_key": entry.get("company_key"),
            }
            for entry in entries
        ]
        fingerprint = hashlib.sha256(json.dumps(
            {"rows": fingerprint_rows, "conflicts": conflicts,
             "source_statuses": records, "status_sync": status_sync},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        return {
            "total_rows": len(records),
            "eligible_contacts": len(entries),
            "new_companies": new_company_count,
            "matched_companies": matched_company_count,
            "conflict_count": len(conflicts),
            "conflicts": conflicts[:100],
            "fingerprint": fingerprint,
            "entries": entries,
            "status_sync": status_sync,
        }

    def preview(self) -> Dict[str, Any]:
        return self.plan(self.fetch_records())

    @staticmethod
    def _mail_status(value: Any) -> str:
        status = str(value or "").strip().casefold()
        if status in {"已推送", "已发送", "sent", "pushed"}:
            return "sent"
        if status in {"已退订", "退订", "unsubscribed"}:
            return "unsubscribed"
        if status in {"待联系", "pending", "待发送"}:
            return "pending"
        return ""

    def _status_sync_plan(self, cursor, records: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Preview Feishu outreach-state reconciliation without changing CRM data."""
        counts = defaultdict(int)
        conflicts, actions = [], []
        by_email = defaultdict(list)
        for row in cursor.execute(
            """SELECT l.id,l.company_id,l.email,c.name AS company_name,c.website
               FROM leads l JOIN companies c ON c.id=l.company_id
               WHERE trim(coalesce(l.email,''))<>''"""
        ).fetchall():
            by_email[str(row["email"]).strip().casefold()].append(dict(row))
        by_queue_email, by_queue_lead = defaultdict(list), defaultdict(list)
        for row in cursor.execute("SELECT id,status,lead_id,email FROM mail_queue").fetchall():
            queue = dict(row)
            by_queue_email[str(row["email"]).strip().casefold()].append(queue)
            by_queue_lead[row["lead_id"]].append(queue)
        legacy = {
            str(row["email"]).strip().casefold(): row["status"]
            for row in cursor.execute(
                "SELECT email,status FROM email_prospects WHERE status IN ('sent','unsubscribed')"
            ).fetchall()
        }
        latest_outreach = {
            row["lead_id"]: dict(row)
            for row in cursor.execute(
                """SELECT r.* FROM outreach_records r
                   WHERE r.id=(SELECT max(r2.id) FROM outreach_records r2 WHERE r2.lead_id=r.lead_id)"""
            ).fetchall()
        }
        outreach_actions = []
        source = defaultdict(list)
        for record in records:
            source_status = self._mail_status(record.get("mail_status"))
            email = str(record.get("email") or "").strip().casefold()
            if not source_status or not email:
                continue
            source[email].append(record)
        for email, source_rows in source.items():
            record = source_rows[0]
            leads = by_email[email]
            if not leads:
                counts["unmatched"] += 1
                continue
            if len(leads) != 1 or any(
                normalize_company_name(item.get("company_name")) != normalize_company_name(leads[0]["company_name"])
                and not (
                    normalize_domain(item.get("website"))
                    and normalize_domain(item.get("website")) == normalize_domain(leads[0]["website"])
                )
                for item in source_rows
            ):
                conflicts.append({
                    "row": record.get("sheet_row"), "email": email,
                    "company": record.get("company_name") or "",
                    "reason": "邮箱或公司归属不唯一，未同步触达状态",
                })
                counts["conflicts"] += 1
                continue
            lead = leads[0]
            queues = {
                row["id"]: row for row in by_queue_email[email] + by_queue_lead[lead["id"]]
            }
            if len(queues) > 1 or any(
                row["lead_id"] != lead["id"] or str(row["email"]).strip().casefold() != email
                for row in queues.values()
            ):
                conflicts.append({
                    "row": record.get("sheet_row"), "email": email,
                    "company": record.get("company_name") or "",
                    "reason": "邮箱命中多个后台队列记录，未同步触达状态",
                })
                counts["conflicts"] += 1
                continue
            statuses = {self._mail_status(item.get("mail_status")) for item in source_rows}
            source_status = next(value for value in ("unsubscribed", "sent", "pending") if value in statuses)
            queue = next(iter(queues.values())) if queues else None
            current = queue["status"] if queue else None
            latest = latest_outreach.get(lead["id"])
            if source_status == "pending":
                counts["preserved" if current and current != "pending" else "unchanged"] += 1
            elif current == source_status:
                counts["unchanged"] += 1
            elif current in {"sending", "unsubscribed", "removed"} or (
                current == "sent" and source_status != "unsubscribed"
            ) or (legacy.get(email) == "unsubscribed" and source_status != "unsubscribed"):
                counts["preserved"] += 1
            else:
                action = "update" if queue else "create"
                counts[f"{action}_{source_status}"] += 1
                actions.append({
                    "action": action, "status": source_status, "email": email,
                    "lead_id": lead["id"], "company_id": lead["company_id"],
                    "queue_id": queue["id"] if queue else None,
                    "previous_status": current,
                })
            if (
                source_status == "sent"
                and current in {None, "pending", "sent"}
                and (not latest or latest["status"] == "pending")
            ):
                outreach_actions.append({
                    "lead_id": lead["id"],
                    "previous_id": latest["id"] if latest else None,
                })
        return {
            "records_with_status": sum(value for key, value in counts.items()
                                       if key not in {"conflicts", "unmatched"}),
            "create_sent": counts["create_sent"],
            "create_unsubscribed": counts["create_unsubscribed"],
            "update_sent": counts["update_sent"],
            "update_unsubscribed": counts["update_unsubscribed"],
            "preserved": counts["preserved"],
            "unchanged": counts["unchanged"],
            "unmatched": counts["unmatched"],
            "conflicts": conflicts[:100],
            "conflict_count": counts["conflicts"],
            "actions": actions,
            "outreach_updates": len(outreach_actions),
            "outreach_actions": outreach_actions,
        }

    def _sync_outreach_statuses(self, cursor, records: List[Dict[str, Any]], actor: str) -> Dict[str, int]:
        """Reconcile source statuses without downgrading protected CRM states."""
        result = {
            "created_sent_statuses": 0,
            "created_unsubscribed_statuses": 0,
            "updated_sent_statuses": 0,
            "updated_unsubscribed_statuses": 0,
            "preserved_statuses": 0,
            "unmatched_statuses": 0,
            "status_conflicts": 0,
            "synced_outreach_records": 0,
        }
        plan = self._status_sync_plan(cursor, records)
        result.update(preserved_statuses=plan["preserved"],
                      unmatched_statuses=plan["unmatched"],
                      status_conflicts=plan["conflict_count"])
        for action in plan["actions"]:
            source_status = action["status"]
            if action["action"] == "create":
                cursor.execute(
                    """INSERT OR IGNORE INTO mail_queue
                       (company_id,lead_id,email,status,created_by)
                       VALUES (?,?,?,?,?)""",
                    (action["company_id"], action["lead_id"], action["email"], source_status, actor),
                )
                verb = "created"
            else:
                cursor.execute(
                    """UPDATE mail_queue
                       SET status=?, updated_at=CURRENT_TIMESTAMP
                       WHERE id=? AND status=? AND lead_id=? AND lower(email)=?""",
                    (source_status, action["queue_id"], action["previous_status"],
                     action["lead_id"], action["email"]),
                )
                verb = "updated"
            changed = cursor.execute("SELECT changes() AS count").fetchone()["count"]
            result[f"{verb}_{source_status}_statuses"] += int(changed)
        for action in plan["outreach_actions"]:
            cursor.execute(
                """INSERT INTO outreach_records(lead_id,channel,status,interaction_notes)
                   SELECT ?,'cold_email','contacted',?
                   WHERE coalesce((SELECT max(id) FROM outreach_records WHERE lead_id=?),0)=?""",
                (action["lead_id"],
                 "飞书邮件表：已推送（历史触达状态同步；本记录时间为同步时间，原触达日期未知；未发送新邮件）",
                 action["lead_id"], action["previous_id"] or 0),
            )
            result["synced_outreach_records"] += int(
                cursor.execute("SELECT changes() AS count").fetchone()["count"]
            )
        return result

    def apply(self, actor: str, expected_fingerprint: Optional[str] = None) -> Dict[str, int]:
        """Reread the source and apply the current plan idempotently."""
        records = self.fetch_records()
        plan = self.plan(records)
        if expected_fingerprint and expected_fingerprint != plan["fingerprint"]:
            raise FeishuImportError("飞书表或CRM数据在预览后发生变化，请重新预览再导入")
        conn = self.db.connection() if hasattr(self.db, "connection") else None
        if conn is None:
            from core.database import get_connection
            conn = get_connection(self.db.db_path)
        created_companies = created_contacts = updated_companies = suppressed = 0
        try:
            cursor = conn.cursor()
            company_ids = {}
            for entry in plan["entries"]:
                key = tuple(entry["company_key"])
                if key not in company_ids:
                    company = entry["company"]
                    if entry["action"] == "create_company":
                        contributions = entry.get("company_records") or [entry]
                        websites = list(dict.fromkeys(
                            str(item.get("website") or "").strip()
                            for item in contributions if str(item.get("website") or "").strip()
                        ))
                        website = websites[0] if websites else ""
                        notes = "\n".join(dict.fromkeys(
                            str(item.get("notes") or "").strip()
                            for item in contributions if str(item.get("notes") or "").strip()
                        ))
                        markets = {item.get("market") for item in contributions}
                        if "pending_verification" in markets or len(markets) != 1:
                            market_classification = "pending_verification"
                            market = "overseas"
                        else:
                            market_classification = "verified"
                            market = next(iter(markets))
                        cursor.execute(
                            """INSERT INTO companies
                               (name,country_type,market_classification,industry,website,source_channel,notes,
                                discovery_url,company_description,description_source)
                               VALUES (?,?,?,?,?,?,?,?,?,?)""",
                            (company["name"], market, market_classification, "未知赛道",
                             website, "飞书邮件表", notes, website,
                             notes, "飞书邮件表" if notes else ""),
                        )
                        company_ids[key] = cursor.lastrowid
                        created_companies += 1
                    else:
                        company_id = int(company["id"])
                        company_ids[key] = company_id
                        updates = {
                            "website": entry.get("website"),
                            "notes": entry.get("notes"),
                            "source_channel": "飞书邮件表",
                            "discovery_url": entry.get("website"),
                            "company_description": entry.get("notes"),
                            "description_source": "飞书邮件表",
                        }
                        changed = False
                        for field, value in updates.items():
                            if value and not company.get(field):
                                cursor.execute(
                                    f"UPDATE companies SET {field}=?, updated_at=CURRENT_TIMESTAMP WHERE id=? AND (trim(coalesce({field},''))='')",
                                    (value, company_id),
                                )
                                changed = True
                        if changed:
                            updated_companies += 1
                company_id = company_ids[key]
                cursor.execute(
                    """INSERT INTO leads
                       (company_id,contact_name,job_title,contact_role_category,email,x_url,source_method,
                        identity_source_url,identity_evidence,verification_status)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (company_id, entry.get("contact_name") or "官方团队",
                     entry.get("job_title") or "业务联系人",
                     entry.get("contact_role_category") or entry.get("job_title") or "",
                     entry["email"],
                     entry.get("x_url") or "", entry.get("source") or "飞书邮件表",
                     entry.get("x_url") or "", "飞书邮件表公开联系人记录",
                     "pending_review"),
                )
                lead_id = cursor.lastrowid
                created_contacts += 1

                email_status = str(entry.get("mail_status") or "").strip().casefold()
                queue_status = "sent" if email_status in {"已推送", "已发送", "sent", "pushed"} else (
                    "unsubscribed" if email_status in {"已退订", "退订", "unsubscribed"} else "pending"
                )
                if queue_status != "pending":
                    cursor.execute(
                        """INSERT OR IGNORE INTO mail_queue
                           (company_id,lead_id,email,status,created_by)
                           VALUES (?,?,?,?,?)""",
                        (company_id, lead_id, entry["email"], queue_status, actor),
                    )
                    suppressed += 1
            status_sync = self._sync_outreach_statuses(cursor, records, actor)
            conn.commit()
        except Exception:
            rollback = getattr(conn, "rollback", None)
            if rollback:
                rollback()
            raise
        finally:
            conn.close()
        return {
            "created_companies": created_companies,
            "updated_companies": updated_companies,
            "created_contacts": created_contacts,
            "conflicts": plan["conflict_count"],
            "suppressed_existing_statuses": suppressed,
            **status_sync,
        }


class FeishuXStatusSync:
    """Reconcile the Feishu X push monitor into X-channel outreach history."""

    STATUS_MAP = {
        "已推送未回复": "sample_sent",
        "已回复": "contacted",
        "暂缓/私信关闭": "lost",
        "无法触达(私信关闭)": "lost",
        "暂缓": "lost",
        "暂缓(不合适)": "lost",
        "不推送": "lost",
        "已起草": "pending",
        "待处理": "pending",
        "已调研": "pending",
        "": "pending",
    }
    ADVANCED_STAGES = {"contacted", "negotiation", "team_trial", "won"}

    def __init__(self, db, environ=None, opener=None):
        self.db = db
        self.environ = os.environ if environ is None else environ
        self.opener = opener or urllib.request.urlopen
        self.spreadsheet_token = self.environ.get(
            "FEISHU_SPREADSHEET_TOKEN", DEFAULT_SPREADSHEET_TOKEN
        ).strip()
        self.sheet_id = self.environ.get("FEISHU_X_SHEET_ID", DEFAULT_X_SHEET_ID).strip()

    def _client(self):
        importer = FeishuMailSheetImporter(
            self.db, environ=self.environ, opener=self.opener
        )
        if not importer.configured():
            raise FeishuImportError("尚未配置飞书应用凭证，无法同步 X 推送监控")
        token_result = importer._json_request(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            method="POST",
            payload={
                "app_id": self.environ.get("FEISHU_APP_ID", "").strip(),
                "app_secret": self.environ.get("FEISHU_APP_SECRET", "").strip(),
            },
        )
        token = (token_result.get("tenant_access_token")
                 or token_result.get("data", {}).get("tenant_access_token"))
        if not token:
            raise FeishuImportError("飞书应用认证失败，无法同步 X 推送监控")
        return importer, token

    def fetch_records(self):
        importer, token = self._client()
        range_name = f"{self.sheet_id}!A1:L1000"
        encoded_range = urllib.parse.quote(range_name, safe="!:")
        url = (
            "https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/"
            f"{urllib.parse.quote(self.spreadsheet_token, safe='')}/values/{encoded_range}"
            "?valueRenderOption=ToString"
        )
        result = importer._json_request(url, token=token)
        data = result.get("data") or {}
        values = data.get("valueRange", {}).get("values") or data.get("values")
        if not isinstance(values, list) or not values:
            raise FeishuImportError("飞书 X 推送监控没有可读取的数据")
        headers = [str(value or "").strip() for value in values[0]]
        required = {"公司", "X链接", "推送状态"}
        if not required.issubset(headers):
            raise FeishuImportError("X 推送监控表头缺少公司、X链接或推送状态列")
        records = []
        for offset, raw in enumerate(values[1:], start=2):
            cells = list(raw) if isinstance(raw, list) else []
            row = {header: cells[index] if index < len(cells) else ""
                   for index, header in enumerate(headers)}
            if not any(str(value or "").strip() for value in row.values()):
                continue
            records.append({
                "sheet_row": offset,
                "contact_name": str(row.get("姓名") or "").strip(),
                "job_title": str(row.get("职位") or "").strip(),
                "company_name": str(row.get("公司") or "").strip(),
                "x_url": str(row.get("X链接") or "").strip(),
                "website": str(row.get("官网") or "").strip(),
                "source_status": str(row.get("推送状态") or "").strip(),
                "source_date": str(row.get("推送日期") or "").strip(),
                "x_account": str(row.get("X账号") or "").strip(),
                "channel": str(row.get("渠道") or "").strip(),
            })
        return records

    @staticmethod
    def normalize_identity(value, channel="x_dm"):
        raw = str(value or "").strip()
        if not raw:
            return ""
        if "://" not in raw:
            if re.match(r"^(www\.)?(x|twitter|linkedin)\.com/", raw, re.I):
                raw = "https://" + raw
            else:
                host = "linkedin.com/in/" if channel == "linkedin_inmail" else "x.com/"
                raw = "https://" + host + raw.lstrip("@")
        try:
            parsed = urlsplit(raw)
            host = (parsed.hostname or "").casefold().removeprefix("www.")
        except ValueError:
            return ""
        allowed_hosts = (
            {"linkedin.com"} if channel == "linkedin_inmail"
            else {"x.com", "twitter.com"}
        )
        if (parsed.scheme not in {"https", "http"} or parsed.username or parsed.password
                or host not in allowed_hosts):
            return ""
        path = parsed.path.strip("/").split("/")
        if channel == "linkedin_inmail":
            if len(path) < 2 or path[0].casefold() not in {"in", "pub"}:
                return ""
            handle = "/".join(path[:2]).casefold()
            return handle if re.fullmatch(r"(in|pub)/[a-z0-9-]{2,100}", handle) else ""
        handle = path[0].casefold() if path else ""
        if not re.fullmatch(r"[a-z0-9_]{1,15}", handle):
            return ""
        return "" if handle in {"home", "explore", "search", "i", "intent", "messages", "settings"} else handle

    @staticmethod
    def normalize_x_identity(value):
        return FeishuXStatusSync.normalize_identity(value, "x_dm")

    @staticmethod
    def company_matches(source, company):
        source_name = normalize_company_name(source.get("company_name"))
        target_name = normalize_company_name(company.get("company_name"))
        source_domain = normalize_domain(source.get("website"))
        target_domain = normalize_domain(company.get("website"))
        shared_hosts = {"calendly.com", "linktr.ee", "youtube.com", "youtu.be",
                        "notion.site", "linkedin.com", "x.com", "twitter.com"}
        same_name = bool(source_name and source_name == target_name)
        same_domain = bool(source_domain and target_domain
                           and source_domain not in shared_hosts
                           and target_domain not in shared_hosts
                           and source_domain == target_domain)
        conflicting_domain = bool(source_domain and target_domain
                                  and source_domain not in shared_hosts
                                  and target_domain not in shared_hosts
                                  and source_domain != target_domain)
        return (same_name or same_domain) and not conflicting_domain

    @staticmethod
    def importable_company_name(value):
        text = str(value or "").strip()
        if not text:
            return False
        folded = text.casefold()
        if normalize_company_name(text) in {
            "freelance", "independent", "personalbrand", "creator", "individual",
        }:
            return False
        return not any(marker.casefold() in folded for marker in (
            "未具名", "个人品牌", "某DTC", "某健康", "某8位数", "某9位数",
            "旗下3个", "旗下多个", "旗下DTC", "anonymous", "unidentified",
            "unknown brand",
        ))

    @staticmethod
    def verified_website_candidate(value):
        raw = str(value or "").strip()
        domain = normalize_domain(raw)
        return bool(domain and domain not in {
            "calendly.com", "linktr.ee", "youtube.com", "youtu.be",
            "linkedin.com", "x.com", "twitter.com", "notion.site",
        })

    def plan(self, records, cursor=None):
        conn = self.db.connection() if hasattr(self.db, "connection") else None
        if cursor is not None:
            conn = None
        elif conn is None:
            from core.database import get_connection
            conn = get_connection(self.db.db_path)
        try:
            cursor = cursor or conn.cursor()
            companies = [dict(row) for row in cursor.execute(
                "SELECT id AS company_id,name AS company_name,website FROM companies"
            ).fetchall()]
            all_leads = [dict(row) for row in cursor.execute(
                "SELECT id AS lead_id,company_id,contact_name,x_url,linkedin_url FROM leads"
            ).fetchall()]
            leads = defaultdict(list)
            for row in cursor.execute(
                """SELECT l.id AS lead_id,l.company_id,l.x_url,l.linkedin_url,
                          l.contact_name,c.name AS company_name,c.website,country_type
                   FROM leads l JOIN companies c ON c.id=l.company_id
                   WHERE trim(coalesce(l.x_url,''))<>'' OR trim(coalesce(l.linkedin_url,''))<>''"""
            ).fetchall():
                lead = dict(row)
                x_identity = self.normalize_identity(lead.get("x_url"), "x_dm")
                linkedin_identity = self.normalize_identity(
                    lead.get("linkedin_url"), "linkedin_inmail"
                )
                if x_identity:
                    leads[("x_dm", x_identity)].append(lead)
                if linkedin_identity:
                    leads[("linkedin_inmail", linkedin_identity)].append(lead)
            latest = {}
            for row in cursor.execute(
                """SELECT r.* FROM outreach_records r
                   WHERE r.channel IN ('x_dm','linkedin_inmail') AND r.id=(
                       SELECT max(r2.id) FROM outreach_records r2
                       WHERE r2.lead_id=r.lead_id AND r2.channel=r.channel)"""
            ).fetchall():
                latest[(row["lead_id"], row["channel"])] = dict(row)
            latest_any = {}
            for row in cursor.execute(
                """SELECT r.* FROM outreach_records r
                   WHERE r.id=(SELECT max(r2.id) FROM outreach_records r2
                               WHERE r2.lead_id=r.lead_id)"""
            ).fetchall():
                latest_any[row["lead_id"]] = dict(row)
            source_states = {}
            if cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='feishu_x_states'"
            ).fetchone():
                for row in cursor.execute(
                    """SELECT * FROM feishu_x_states
                       WHERE spreadsheet_token=? AND sheet_id=?""",
                    (self.spreadsheet_token, self.sheet_id),
                ).fetchall():
                    source_states[row["identity"]] = dict(row)
        finally:
            if conn:
                conn.close()

        conflicts, actions, state_actions, imports, counts = [], [], [], [], defaultdict(int)
        grouped = defaultdict(list)
        for record in records:
            status = str(record.get("source_status") or "").strip()
            source_channel = (
                "linkedin_inmail"
                if "linkedin" in str(record.get("channel") or "").casefold()
                or "linkedin.com/" in str(record.get("x_url") or "").casefold()
                else "x_dm"
            )
            identity = self.normalize_identity(
                record.get("x_url") or record.get("x_account"), source_channel
            )
            account = self.normalize_identity(
                record.get("x_account"), source_channel
            ) if record.get("x_account") else identity
            if record.get("x_url") and record.get("x_account") and identity != account:
                conflicts.append({"row": record.get("sheet_row"),
                                  "company": record.get("company_name") or "",
                                  "reason": "X 链接与 X账号不一致，未同步"})
                counts["malformed"] += 1
                continue
            if status not in self.STATUS_MAP:
                conflicts.append({"row": record.get("sheet_row"),
                                  "company": record.get("company_name") or "",
                                  "reason": f"未知 X 推送状态：{status}"})
                counts["malformed"] += 1
                continue
            if not identity:
                conflicts.append({"row": record.get("sheet_row"),
                                  "company": record.get("company_name") or "",
                                  "reason": "缺少有效 X 链接/账号，未同步"})
                counts["malformed"] += 1
                continue
            grouped[(source_channel, identity)].append(record)

        for (source_channel, identity), source_rows in grouped.items():
            statuses = {row.get("source_status", "").strip() for row in source_rows}
            source_companies = {
                normalize_company_name(row.get("company_name")) for row in source_rows
            }
            source_domains = {
                normalize_domain(row.get("website")) for row in source_rows
                if normalize_domain(row.get("website"))
            }
            source_dates = {row.get("source_date") or "" for row in source_rows}
            if (len(statuses) > 1 or len(source_companies) > 1
                    or len(source_domains) > 1 or len(source_dates) > 1):
                conflicts.append({"row": source_rows[0].get("sheet_row"),
                                  "company": source_rows[0].get("company_name") or "",
                                  "reason": "同一社媒账号在飞书表出现多个状态或公司归属，未同步"})
                counts["conflicts"] += 1
                continue
            matches = leads.get((source_channel, identity), [])
            source = source_rows[0]
            if not matches:
                company_matches = [company for company in companies
                                   if self.company_matches(source, company)]
                if (self.importable_company_name(source.get("company_name"))
                        and source.get("contact_name")
                        and len(company_matches) <= 1):
                    company = company_matches[0] if company_matches else None
                    same_name_leads = [lead for lead in all_leads
                                       if company and lead["company_id"] == company["company_id"]
                                       and normalize_company_name(lead["contact_name"])
                                       == normalize_company_name(source["contact_name"])]
                    field = "linkedin_url" if source_channel == "linkedin_inmail" else "x_url"
                    if len(same_name_leads) <= 1 and not any(
                        str(lead.get(field) or "").strip() for lead in same_name_leads
                    ):
                        imports.append({
                            "source": source, "channel": source_channel, "identity": identity,
                            "company_id": company["company_id"] if company else None,
                            "lead_id": same_name_leads[0]["lead_id"] if same_name_leads else None,
                        })
                        counts["importable"] += len(source_rows)
                        continue
                conflicts.append({"row": source_rows[0].get("sheet_row"),
                                  "company": source_rows[0].get("company_name") or "",
                                  "reason": "CRM 中未找到唯一可归属的社媒联系人"})
                counts["unmatched"] += 1
                continue
            if len(matches) != 1:
                narrowed = [lead for lead in matches if self.company_matches(source, lead)]
                if len(narrowed) > 1:
                    named = [lead for lead in narrowed
                             if normalize_company_name(lead.get("contact_name"))
                             == normalize_company_name(source.get("contact_name"))]
                    if len(named) == 1:
                        narrowed = named
                if len(narrowed) != 1:
                    conflicts.append({"row": source_rows[0].get("sheet_row"),
                                      "company": source_rows[0].get("company_name") or "",
                                      "reason": "X 账号命中多个 CRM 线索"})
                    counts["conflicts"] += 1
                    continue
                matches = narrowed
            lead = matches[0]
            source_company = normalize_company_name(source.get("company_name"))
            crm_company = normalize_company_name(lead.get("company_name"))
            source_domain = normalize_domain(source.get("website"))
            crm_domain = normalize_domain(lead.get("website"))
            name_matches = bool(source_company and crm_company and (
                source_company == crm_company or source_company in crm_company
                or crm_company in source_company
            ))
            domain_matches = bool(source_domain and crm_domain and (
                source_domain == crm_domain or source_domain.endswith("." + crm_domain)
                or crm_domain.endswith("." + source_domain)
            ))
            if (source_domain and crm_domain and not domain_matches) or (
                not domain_matches and not name_matches
            ):
                conflicts.append({"row": source.get("sheet_row"),
                                  "company": source.get("company_name") or "",
                                  "reason": "X 账号匹配但公司名/官网域名不一致，需人工核对"})
                counts["conflicts"] += 1
                continue

            desired = self.STATUS_MAP[source["source_status"].strip()]
            counts["matched"] += len(source_rows)
            state = {
                "identity": f"{source_channel}:{identity}", "lead_id": lead["lead_id"],
                "source_status": source["source_status"].strip(),
                "source_date": source.get("source_date") or "",
                "sheet_row": source.get("sheet_row"),
            }
            previous_state = source_states.get(state["identity"])
            if not previous_state or any(previous_state.get(key) != value for key, value in state.items()):
                state_actions.append(state)
            current = latest.get((lead["lead_id"], source_channel))
            current_status = current.get("status") if current else "pending"
            active_any = latest_any.get(lead["lead_id"])
            if active_any and active_any.get("channel") != source_channel and (
                active_any.get("status") in self.ADVANCED_STAGES | {"lost"}
            ):
                counts["preserved"] += 1
                continue
            if current and current_status == desired:
                counts["unchanged"] += 1
                continue
            if current_status in self.ADVANCED_STAGES:
                counts["preserved"] += 1
                continue
            if desired == "pending":
                counts["preserved" if current else "unchanged"] += 1
                continue
            if current and current_status == "lost" and desired != "lost":
                conflicts.append({"row": source.get("sheet_row"),
                                  "company": source.get("company_name") or "",
                                  "reason": "CRM 已有 X 渠道不触达/关闭状态，未被普通触达状态覆盖"})
                counts["conflicts"] += 1
                continue
            action = {
                "lead_id": lead["lead_id"], "previous_id": current.get("id") if current else None,
                "channel": source_channel,
                "previous_status": current_status, "status": desired,
                "source_status": source["source_status"].strip(),
                "source_date": source.get("source_date") or "",
                "sheet_row": source.get("sheet_row"),
                "company_id": lead["company_id"],
                "previous_latest_id": active_any["id"] if active_any else 0,
            }
            actions.append(action)
            counts[f"update_{desired}" if current else f"create_{desired}"] += 1
        result = {
            "total_rows": len(records), "matched_actions": len(actions),
            "importable_rows": counts["importable"], "imports": imports,
            "matched_rows": counts["matched"], "source_updates": len(state_actions),
            "create_sample_sent": counts["create_sample_sent"],
            "create_contacted": counts["create_contacted"],
            "create_lost": counts["create_lost"],
            "update_sample_sent": counts["update_sample_sent"],
            "update_contacted": counts["update_contacted"],
            "update_lost": counts["update_lost"],
            "unchanged": counts["unchanged"], "preserved": counts["preserved"],
            "unmatched": counts["unmatched"], "malformed": counts["malformed"],
            "conflict_count": counts["conflicts"],
            "conflicts": conflicts, "actions": actions, "state_actions": state_actions,
        }
        result["fingerprint"] = hashlib.sha256(json.dumps(
            {"records": records, "plan": result}, sort_keys=True, ensure_ascii=False,
        ).encode()).hexdigest()
        return result

    def preview(self):
        return self.plan(self.fetch_records())

    def apply(self, actor="Vercel Feishu Sync", expected_fingerprint=None):
        records = self.fetch_records()
        conn = self.db.connection() if hasattr(self.db, "connection") else None
        if conn is None:
            from core.database import get_connection
            conn = get_connection(self.db.db_path)
        try:
            cursor = conn.cursor()
            if hasattr(conn, "in_transaction"):
                cursor.execute("BEGIN IMMEDIATE")
            plan = self.plan(records, cursor=cursor)
            if expected_fingerprint and expected_fingerprint != plan["fingerprint"]:
                raise FeishuImportError("飞书表或CRM数据在预览后发生变化，请重新预览再同步")
            created_companies = created_contacts = linked_contacts = 0
            for entry in plan["imports"]:
                source = entry["source"]
                company_id = entry["company_id"]
                if not company_id:
                    cursor.execute(
                        """INSERT OR IGNORE INTO companies
                           (name,country_type,market_classification,industry,website,source_channel,notes)
                           VALUES (?,'overseas','pending_verification','待核验',?,'飞书X推送监控',?)""",
                        (source["company_name"],
                         source.get("website") if self.verified_website_candidate(source.get("website")) else "",
                         "飞书原始公司描述，未经独立核验；市场与身份待核验。"),
                    )
                    created_companies += int(cursor.execute(
                        "SELECT changes() AS count"
                    ).fetchone()["count"])
                    company_id = cursor.execute(
                        "SELECT id FROM companies WHERE name=?", (source["company_name"],)
                    ).fetchone()["id"]
                field = "linkedin_url" if entry["channel"] == "linkedin_inmail" else "x_url"
                url = ("https://www.linkedin.com/" + entry["identity"]
                       if field == "linkedin_url" else "https://x.com/" + entry["identity"])
                if entry["lead_id"]:
                    cursor.execute(
                        f"""UPDATE leads SET {field}=?,updated_at=CURRENT_TIMESTAMP
                            WHERE id=? AND trim(coalesce({field},''))=''""",
                        (url, entry["lead_id"]),
                    )
                    linked_contacts += int(cursor.execute(
                        "SELECT changes() AS count"
                    ).fetchone()["count"])
                else:
                    cursor.execute(
                        f"""INSERT INTO leads(company_id,contact_name,job_title,{field},
                           source_method,verification_status,identity_source_url,identity_evidence)
                           VALUES (?,?,?,?,?,'pending_review',?,?)""",
                        (company_id, source["contact_name"], source.get("job_title") or "",
                         url, "飞书X推送监控", url,
                         "飞书源表联系人记录，未经独立核验；不推断邮箱或其他联系方式。"),
                    )
                    created_contacts += 1
            if plan["imports"]:
                plan = self.plan(records, cursor=cursor)
            cursor.execute(
                """CREATE TABLE IF NOT EXISTS feishu_x_states (
                   spreadsheet_token TEXT NOT NULL, sheet_id TEXT NOT NULL,
                   identity TEXT NOT NULL, lead_id INTEGER NOT NULL,
                   source_status TEXT NOT NULL, source_date TEXT NOT NULL,
                   sheet_row INTEGER, synced_at TEXT DEFAULT CURRENT_TIMESTAMP,
                   PRIMARY KEY(spreadsheet_token,sheet_id,identity))"""
            )
            result = {key: value for key, value in plan.items()
                      if key not in {"actions", "state_actions", "imports"}}
            result.update(created_companies=created_companies, created_contacts=created_contacts,
                          linked_contacts=linked_contacts)
            result["created_outreach_records"] = 0
            result["updated_outreach_records"] = 0
            for state in plan["state_actions"]:
                cursor.execute(
                    """INSERT INTO feishu_x_states
                       (spreadsheet_token,sheet_id,identity,lead_id,source_status,source_date,sheet_row)
                       VALUES (?,?,?,?,?,?,?) ON CONFLICT(spreadsheet_token,sheet_id,identity)
                       DO UPDATE SET lead_id=excluded.lead_id,source_status=excluded.source_status,
                       source_date=excluded.source_date,sheet_row=excluded.sheet_row,
                       synced_at=CURRENT_TIMESTAMP""",
                    (self.spreadsheet_token, self.sheet_id, state["identity"],
                     state["lead_id"], state["source_status"], state["source_date"], state["sheet_row"]),
                )
            for action in plan["actions"]:
                source_date = action["source_date"] or "未填写"
                note = (
                    f"飞书社媒推送监控（{action['channel']}）：源表状态「{action['source_status']}」；"
                    f"源表日期：{source_date}。历史状态同步，记录创建时间为同步时间；"
                    "未触发任何私信或邮件发送。"
                )
                cursor.execute(
                    """INSERT INTO outreach_records
                       (lead_id,channel,status,interaction_notes)
                       SELECT ?,?,?,?
                       WHERE coalesce((SELECT max(id) FROM outreach_records
                                       WHERE lead_id=?),0)=?""",
                    (action["lead_id"], action["channel"], action["status"], note, action["lead_id"],
                     action["previous_latest_id"]),
                )
                changed = int(cursor.execute("SELECT changes() AS count").fetchone()["count"])
                result["created_outreach_records"] += changed
                if not changed:
                    raise FeishuImportError("CRM 触达记录在同步时发生变化，请重新预览")
            cursor.execute(
                """CREATE TABLE IF NOT EXISTS feishu_x_source_rows (
                   spreadsheet_token TEXT NOT NULL,sheet_id TEXT NOT NULL,
                   sheet_row INTEGER NOT NULL,lead_id INTEGER,
                   source_status TEXT NOT NULL,source_date TEXT NOT NULL,
                   source_channel TEXT NOT NULL,payload TEXT NOT NULL,
                   resolution TEXT NOT NULL,synced_at TEXT DEFAULT CURRENT_TIMESTAMP,
                   PRIMARY KEY(spreadsheet_token,sheet_id,sheet_row))"""
            )
            states = {row["identity"]: dict(row) for row in cursor.execute(
                "SELECT * FROM feishu_x_states WHERE spreadsheet_token=? AND sheet_id=?",
                (self.spreadsheet_token, self.sheet_id),
            ).fetchall()}
            issues = {item["row"]: item["reason"] for item in plan["conflicts"]}
            rows = []
            for source in records:
                channel = ("linkedin_inmail" if "linkedin" in str(source.get("channel") or "").casefold()
                           or "linkedin.com/" in str(source.get("x_url") or "").casefold()
                           else "x_dm")
                identity = self.normalize_identity(
                    source.get("x_url") or source.get("x_account"), channel
                )
                state = states.get(f"{channel}:{identity}")
                reason = issues.get(source["sheet_row"])
                lead_id = state["lead_id"] if state and not reason else None
                rows.append((self.spreadsheet_token, self.sheet_id, source["sheet_row"],
                             lead_id, source.get("source_status") or "",
                             source.get("source_date") or "", channel,
                             json.dumps(source, ensure_ascii=False, sort_keys=True),
                             reason or "已同步"))
            cursor.executemany(
                """INSERT INTO feishu_x_source_rows
                   (spreadsheet_token,sheet_id,sheet_row,lead_id,source_status,source_date,
                    source_channel,payload,resolution) VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(spreadsheet_token,sheet_id,sheet_row) DO UPDATE SET
                   lead_id=excluded.lead_id,source_status=excluded.source_status,
                   source_date=excluded.source_date,source_channel=excluded.source_channel,
                   payload=excluded.payload,resolution=excluded.resolution,
                   synced_at=CURRENT_TIMESTAMP""", rows,
            )
            result["archived_source_rows"] = len(rows)
            conn.commit()
        except Exception:
            rollback = getattr(conn, "rollback", None)
            if rollback:
                rollback()
            raise
        finally:
            conn.close()
        result.pop("actions", None)
        return result
