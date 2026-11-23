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
            {"rows": fingerprint_rows, "conflicts": conflicts},
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
        }

    def preview(self) -> Dict[str, Any]:
        return self.plan(self.fetch_records())

    def apply(self, actor: str, expected_fingerprint: Optional[str] = None) -> Dict[str, int]:
        """Reread the source and apply the current plan idempotently."""
        plan = self.plan(self.fetch_records())
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
        }
