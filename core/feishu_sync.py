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
             payload=excluded.payload,resolution=excluded.resolution,synced_at=CURRENT_TIMESTAMP""",
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
