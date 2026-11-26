"""Idempotent Feishu community import with durable source and run audit records."""
import re
import urllib.parse
from collections import defaultdict

from core.database import get_connection
from core.feishu_import import FeishuImportError, FeishuMailSheetImporter, normalize_company_name
from core.feishu_sync import (
    commit_statements, fingerprint, json_text, run_statement, source_statement, synchronized,
)


def invite_identity(value):
    try:
        parsed = urllib.parse.urlsplit(str(value or "").strip())
        if parsed.scheme not in {"https", "http"} or parsed.username or parsed.password:
            return ""
        host = (parsed.hostname or "").lower()
        path = parsed.path.strip("/")
        if host in {"discord.com", "www.discord.com", "discordapp.com"}:
            if not path.startswith("invite/"):
                return ""
            path = path[7:]
        elif host != "discord.gg":
            return ""
        # Discord invitation codes are case-sensitive.
        return path if re.fullmatch(r"[A-Za-z0-9_-]{2,150}", path) else ""
    except ValueError:
        return ""


class FeishuDiscordImporter:
    def __init__(self, db, environ=None, opener=None):
        self.db = db
        self.client = FeishuMailSheetImporter(db, environ=environ, opener=opener)
        self.spreadsheet_token = self.client.spreadsheet_token
        self.sheet_id = self.client.environ.get("FEISHU_DISCORD_SHEET_ID", "7u5M4j").strip()

    def fetch_records(self):
        client = self.client
        if not client.configured():
            raise FeishuImportError("尚未配置飞书应用凭证")
        auth = client._json_request(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            method="POST", payload={
                "app_id": client.environ.get("FEISHU_APP_ID", "").strip(),
                "app_secret": client.environ.get("FEISHU_APP_SECRET", "").strip(),
            },
        )
        token = auth.get("tenant_access_token") or auth.get("data", {}).get("tenant_access_token")
        if not token:
            raise FeishuImportError("飞书应用认证失败")
        values = client.read_sheet_values(token, self.sheet_id, "J")
        headers = [str(x or "").strip() for x in values[0]]
        if not {"社区名称", "邀请链接", "核验状态", "说明"}.issubset(headers):
            raise FeishuImportError("Discord 表头缺少社区名称、邀请链接、核验状态或说明")
        records = []
        for offset, cells in enumerate(values[1:], start=2):
            row = {k: str(cells[i] or "").strip() if i < len(cells) else ""
                   for i, k in enumerate(headers) if k}
            if not any(row.values()):
                continue
            records.append({
                "sheet_row": offset, "name": row["社区名称"],
                "invite_url": row["邀请链接"], "verification": row["核验状态"],
                "description": row["说明"], "notes": row.get("备注", ""),
            })
        return records

    def plan(self, records):
        conn = get_connection(self.db.db_path)
        try:
            existing = [dict(x) for x in conn.execute("SELECT * FROM discord_communities").fetchall()]
            saved = {x["source_key"]: dict(x) for x in conn.execute(
                """SELECT * FROM feishu_sync_sources WHERE source_type='discord'
                   AND spreadsheet_token=? AND sheet_id=?""",
                (self.spreadsheet_token, self.sheet_id),
            ).fetchall()}
        finally:
            conn.close()
        by_name, by_invite = defaultdict(list), defaultdict(list)
        for row in existing:
            by_name[normalize_company_name(row["name"])].append(row)
            identity = invite_identity(row["invite_url"])
            if identity:
                by_invite[identity].append(row)
        seen_names, seen_codes = {}, {}
        entries, conflicts, duplicates = [], [], []
        for record in records:
            key = invite_identity(record["invite_url"])
            name = normalize_company_name(record["name"])
            if not key or not name:
                conflicts.append({"row": record["sheet_row"], "company": record["name"],
                                  "reason": "社群名称或有效 Discord 邀请链接缺失"})
                continue
            if name in seen_names or key in seen_codes:
                previous = seen_names.get(name) or seen_codes[key]
                if previous["name"] == name and previous["key"] == key:
                    duplicates.append(record["sheet_row"])
                else:
                    conflicts.append({"row": record["sheet_row"], "company": record["name"],
                                      "reason": "表内社群名称与邀请链接映射冲突"})
                continue
            seen_names[name] = seen_codes[key] = {"name": name, "key": key}
            matches = {row["id"]: row for row in by_name[name] + by_invite[key]}
            old = saved.get(key)
            if old and old["target_id"] is not None:
                target = next((row for row in existing if row["id"] == old["target_id"]), None)
                if target:
                    matches[target["id"]] = target
            if len(matches) > 1:
                conflicts.append({"row": record["sheet_row"], "company": record["name"],
                                  "reason": "社群名称/邀请链接命中多个后台记录，需核对"})
                continue
            target = next(iter(matches.values()), None)
            action = "create" if not target else (
                "unchanged" if old and old["payload"] == json_text(record) else "update_source"
            )
            # An edited invitation is not silently allowed to overwrite a user's link.
            if target and invite_identity(target["invite_url"]) != key:
                conflicts.append({"row": record["sheet_row"], "company": record["name"],
                                  "reason": "已有社群邀请链接不同，保留运营记录待核对"})
                continue
            entries.append({"record": record, "key": key, "target_id": target["id"] if target else None,
                            "action": action, "target": target})
        summary = {kind: sum(x["action"] == kind for x in entries)
                   for kind in ("create", "update_source", "unchanged")}
        return {
            "total_rows": len(records), **summary, "duplicates": len(duplicates),
            "conflict_count": len(conflicts), "conflicts": conflicts, "entries": entries,
            "source_records": records, "duplicate_rows": duplicates,
            "fingerprint": fingerprint({"source": records, "targets": existing, "saved": saved}),
        }

    def preview(self):
        return self.plan(self.fetch_records())

    @synchronized("discord")
    def apply(self, actor, expected_fingerprint=None, records=None, plan=None):
        records = self.fetch_records() if records is None else records
        plan = self.plan(records) if plan is None else plan
        if expected_fingerprint and plan["fingerprint"] != expected_fingerprint:
            raise FeishuImportError("Discord 表或后台记录已变化，请重新检测")
        statements = []
        archived_rows = set()
        for entry in plan["entries"]:
            record, target = entry["record"], entry["target"]
            if entry["action"] == "create":
                statements.append((
                    """INSERT INTO discord_communities
                       (name,invite_url,focus,language,fit_level,priority,join_status,account_type,notes,added_by)
                       VALUES (?,?,?,'英文','中',?,'待加入','官方号',?,?)""",
                    (record["name"], record["invite_url"], record["description"] or "待核验",
                     record["sheet_row"] - 1,
                     "\n".join(filter(None, [record["verification"], record["notes"]])), actor),
                ))
            sql, params = source_statement(
                "discord", self.spreadsheet_token, self.sheet_id, entry["key"],
                record["sheet_row"], entry["target_id"], record, entry["action"],
            )
            if not target:
                sql = sql.replace("VALUES (?,?,?,?,?,?,?,?)",
                                  "VALUES (?,?,?,?,?,(SELECT id FROM discord_communities WHERE name=?),?,?)")
                params = (*params[:5], record["name"], *params[6:])
            statements.append((sql, params))
            archived_rows.add(record["sheet_row"])
        entry_by_row = {item["record"]["sheet_row"]: item for item in plan["entries"]}
        conflict_by_row = {item["row"]: item["reason"] for item in plan["conflicts"]}
        duplicate_rows = set(plan["duplicate_rows"])
        for record in plan["source_records"]:
            if record["sheet_row"] in archived_rows:
                continue
            duplicate_or_conflict = (
                record["sheet_row"] in duplicate_rows or record["sheet_row"] in conflict_by_row
            )
            key = (
                f"row-{record['sheet_row']}" if duplicate_or_conflict
                else invite_identity(record.get("invite_url")) or f"row-{record['sheet_row']}"
            )
            entry = entry_by_row.get(record["sheet_row"])
            statements.append(source_statement(
                "discord", self.spreadsheet_token, self.sheet_id, key,
                record["sheet_row"], entry["target_id"] if entry else None,
                record,
                "duplicate" if record["sheet_row"] in duplicate_rows else
                f"conflict:{conflict_by_row[record['sheet_row']]}" if record["sheet_row"] in conflict_by_row
                else "unmatched",
            ))
        result = {k: v for k, v in plan.items()
                  if k not in {"entries", "fingerprint", "source_records", "duplicate_rows"}}
        statements.append(run_statement("discord", actor, result))
        conn = get_connection(self.db.db_path)
        try:
            commit_statements(conn, statements)
        finally:
            conn.close()
        return result
