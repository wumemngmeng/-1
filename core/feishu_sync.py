"""Shared durable audit and cross-process import coordination."""
import hashlib
import json
from functools import wraps
from uuid import uuid4

from core.database import RemoteConnection, get_connection


def json_text(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(value):
    return hashlib.sha256(json_text(value).encode()).hexdigest()


def link_source(cursor, source_type, source_key, company_id, lead_id, payload):
    cursor.execute(
        """INSERT INTO source_entity_links
           (source_type,source_key,company_id,lead_id,payload_hash)
           VALUES (?,?,?,?,?)
           ON CONFLICT(source_type,source_key) DO UPDATE SET
             company_id=excluded.company_id,lead_id=excluded.lead_id,
             payload_hash=excluded.payload_hash,updated_at=CURRENT_TIMESTAMP
           WHERE source_entity_links.company_id!=excluded.company_id
              OR coalesce(source_entity_links.lead_id,0)!=coalesce(excluded.lead_id,0)
              OR source_entity_links.payload_hash!=excluded.payload_hash""",
        (source_type, source_key, company_id, lead_id, fingerprint(payload)),
    )


def add_contact_method(
    cursor, lead_id, method_type, value, normalized_value, source_type,
    is_primary=False,
):
    if not str(value or "").strip() or not str(normalized_value or "").strip():
        return 0
    cursor.execute(
        """INSERT OR IGNORE INTO lead_contact_methods
           (lead_id,method_type,value,normalized_value,source_type,is_primary)
           VALUES (?,?,?,?,?,?)""",
        (lead_id, method_type, str(value).strip(), str(normalized_value).strip(),
         source_type, int(bool(is_primary))),
    )
    return int(cursor.execute("SELECT changes() AS count").fetchone()["count"])


def set_channel_state(
    cursor, company_id, lead_id, channel, status, source_type, source_key,
    occurred_at="",
):
    cursor.execute(
        """INSERT INTO channel_states
           (company_id,lead_id,channel,current_status,source_type,source_key,
            source_occurred_at)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(lead_id,channel) DO UPDATE SET
             company_id=excluded.company_id,current_status=excluded.current_status,
             source_type=excluded.source_type,source_key=excluded.source_key,
             source_occurred_at=excluded.source_occurred_at,
             updated_at=CURRENT_TIMESTAMP
           WHERE channel_states.company_id!=excluded.company_id
              OR channel_states.current_status!=excluded.current_status
              OR channel_states.source_type!=excluded.source_type
              OR channel_states.source_key!=excluded.source_key
              OR channel_states.source_occurred_at!=excluded.source_occurred_at""",
        (company_id, lead_id, channel, status, source_type, source_key,
         str(occurred_at or "")),
    )


def record_touch_event(
    cursor, company_id, lead_id, channel, event_type, source_type,
    source_event_key, occurred_at="", payload=None,
):
    cursor.execute(
        """INSERT OR IGNORE INTO touch_events
           (company_id,lead_id,channel,event_type,source_type,source_event_key,
            occurred_at,payload_json)
           VALUES (?,?,?,?,?,?,?,?)""",
        (company_id, lead_id, channel, event_type, source_type,
         source_event_key, str(occurred_at or ""), json_text(payload or {})),
    )
    return int(cursor.execute("SELECT changes() AS count").fetchone()["count"])


def commit_statements(conn, statements):
    if isinstance(conn, RemoteConnection):
        if statements:
            conn.client.batch(statements)
    else:
        try:
            for sql, params in statements:
                conn.execute(sql, params)
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def source_statement(kind, token, sheet, key, row, target_id, payload, resolution):
    return (
        """INSERT INTO feishu_sync_sources
           (source_type,spreadsheet_token,sheet_id,source_key,sheet_row,target_id,payload,resolution)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(source_type,spreadsheet_token,sheet_id,source_key) DO UPDATE SET
             sheet_row=excluded.sheet_row,target_id=excluded.target_id,
             payload=excluded.payload,resolution=excluded.resolution,synced_at=CURRENT_TIMESTAMP
           WHERE feishu_sync_sources.sheet_row!=excluded.sheet_row
              OR coalesce(feishu_sync_sources.target_id,0)!=coalesce(excluded.target_id,0)
              OR feishu_sync_sources.payload!=excluded.payload
              OR feishu_sync_sources.resolution!=excluded.resolution""",
        (kind, token, sheet, key, row, target_id, json_text(payload), resolution),
    )


def run_statement(kind, actor, result):
    return (
        "INSERT INTO feishu_sync_runs(id,source_type,actor,result) VALUES(?,?,?,?)",
        (uuid4().hex, kind, actor, json_text(result)),
    )


def synchronized(kind):
    """Serialize the same importer across cron and serverless instances."""
    def decorate(method):
        @wraps(method)
        def run(self, *args, **kwargs):
            from core.feishu_import import FeishuImportError
            owner = uuid4().hex
            conn = get_connection(self.db.db_path)
            try:
                cursor = conn.execute(
                    """INSERT INTO feishu_sync_locks(source_type,owner,expires_at)
                       VALUES (?,?,datetime('now','+10 minutes'))
                       ON CONFLICT(source_type) DO UPDATE SET
                         owner=excluded.owner,expires_at=excluded.expires_at
                       WHERE feishu_sync_locks.expires_at < CURRENT_TIMESTAMP
                       RETURNING owner""", (kind, owner),
                )
                acquired = cursor.fetchone()
                conn.commit()
                if not acquired or acquired["owner"] != owner:
                    raise FeishuImportError("该来源正在同步，请稍后重试")
                try:
                    return method(self, *args, **kwargs)
                finally:
                    conn.execute("DELETE FROM feishu_sync_locks WHERE source_type=? AND owner=?",
                                 (kind, owner))
                    conn.commit()
            finally:
                conn.close()
        return run
    return decorate
