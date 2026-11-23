# -*- coding: utf-8 -*-
"""
Rich modern Web Application for Aibubu Outbound Engine.
Features:
- Left sidebar navigation (线索作战中台, 组织与权限管理, 销售漏斗与数据)
- User Authentication (完整的注册、登录、退出、会话维持与角色权限控制)
- Two-Tier Filter:
    - Tier 1: 市场区域 (全部/国内/海外), 公司数据来源筛选 (15个核心源), 客户分级筛选 (S/A/B/C)
    - Tier 2 (二级筛选): 触达状态 (全部/未触达/已触达), 漏斗阶段 (pending/contacted/sample_sent...)
- Customer Tier Badges (S级战略大客户, A级腰部核心, B级高意向, C级潜力)
- Inline Customer Notes editor with SQLite persistence
- Official clickable company links
- Role-based Access Control (Admin, Sales Lead, Sales Rep, Observer)
- Direct Pitch generator & CSV Export
"""
import http.server
import socketserver
import base64
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
import re
import smtplib
import ssl
from email.message import EmailMessage
from uuid import uuid4
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from http.cookies import SimpleCookie
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.database import DatabaseManager
from core.email_queue import EmailQueue

PORT = 8899
SESSION_COOKIE = "aibubu_session"
SESSION_TTL = 30 * 24 * 60 * 60
SESSION_SECRET = (
    os.environ.get("SESSION_SECRET")
    or os.environ.get("TURSO_AUTH_TOKEN")
    or "aibubu-local-development-session-key"
).encode()
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def render_email_template(template, prospect):
    values = {
        "name": prospect.get("contact_name") or "there",
        "company": prospect.get("company_name") or "your team",
        "email": prospect.get("email") or "",
    }
    rendered = str(template or "")
    for key, value in values.items():
        rendered = rendered.replace("{{" + key + "}}", str(value))
    return rendered


def send_resend_batch(messages, settings=None, batch_id=None):
    settings = settings or {}
    api_key = settings.get("api_key") or os.environ.get("RESEND_API_KEY", "").strip()
    sender = settings.get("from_email") or os.environ.get("RESEND_FROM_EMAIL", "").strip()
    if not api_key or not sender:
        raise RuntimeError("邮件服务尚未配置 RESEND_API_KEY 和 RESEND_FROM_EMAIL")
    display_sender = f'{settings["from_name"]} <{sender}>' if settings.get("from_name") else sender
    payload = json.dumps([{
        "from": display_sender, "to": [message["email"]],
        "subject": message["subject"], "text": message["body"],
        **({"reply_to": settings["reply_to"]} if settings.get("reply_to") else {}),
    } for message in messages]).encode("utf-8")
    request = urllib.request.Request(
        "https://api.resend.com/emails/batch",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "Aibubu-CRM/1.0",
            **({"Idempotency-Key": batch_id} if batch_id else {}),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            result = json.loads(response.read().decode("utf-8"))
            return [item.get("id", "") for item in result.get("data", [])]
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"Resend 返回 {error.code}，请管理员检查发件域名和配置") from None


def send_smtp_batch(messages, settings=None, batch_id=None):
    settings = settings or {}
    password = settings.get("smtp_password", "")
    host = settings.get("smtp_host", "")
    username = settings.get("smtp_username", "")
    if not host or not username or not password:
        raise RuntimeError("SMTP 尚未完整配置服务器、账号和密码")
    port = int(settings.get("smtp_port", 465))
    security = settings.get("smtp_security", "ssl")
    client = smtplib.SMTP_SSL(host, port, timeout=20) if security == "ssl" else smtplib.SMTP(host, port, timeout=20)
    try:
        client.ehlo()
        if security == "starttls":
            client.starttls(context=ssl.create_default_context())
            client.ehlo()
        if security != "none" or username:
            client.login(username, password)
        ids = []
        for message in messages:
            mail = EmailMessage()
            mail["From"] = f'{settings["from_name"]} <{settings["from_email"]}>' if settings.get("from_name") else settings["from_email"]
            mail["To"] = message["email"]
            mail["Subject"] = message["subject"]
            if settings.get("reply_to"):
                mail["Reply-To"] = settings["reply_to"]
            mail.set_content(message["body"])
            client.send_message(mail)
            ids.append(f"smtp-{uuid4().hex}")
        return ids
    finally:
        client.quit()


def test_smtp_connection(settings=None):
    settings = settings or {}
    password = settings.get("smtp_password", "")
    host = settings.get("smtp_host", "")
    username = settings.get("smtp_username", "")
    if not host or not username or not password:
        raise RuntimeError("SMTP 尚未完整配置服务器、账号和密码")
    port = int(settings.get("smtp_port", 465))
    security = settings.get("smtp_security", "ssl")
    client = smtplib.SMTP_SSL(host, port, timeout=20) if security == "ssl" else smtplib.SMTP(host, port, timeout=20)
    try:
        client.ehlo()
        if security == "starttls":
            client.starttls(context=ssl.create_default_context())
            client.ehlo()
        client.login(username, password)
    finally:
        try:
            client.quit()
        except smtplib.SMTPException:
            pass


def send_mail_batch(messages, settings=None, batch_id=None):
    if settings.get("provider") == "smtp":
        return send_smtp_batch(messages, settings=settings, batch_id=batch_id)
    return send_resend_batch(messages, settings=settings, batch_id=batch_id)


def run_email_automation(db):
    queue = EmailQueue(db)
    automation = queue.automation()
    if not automation.get("enabled"):
        return {"sent": 0, "skipped": "disabled"}
    now = datetime.now(ZoneInfo(automation["timezone"]))
    if now.hour != int(automation["send_hour"]) or now.minute > 10:
        return {"sent": 0, "skipped": "outside_schedule"}
    settings = queue.settings(private=True, sender_id=int(automation["sender_id"]))
    if not settings["configured"]:
        return {"sent": 0, "skipped": "sender_not_configured"}
    remaining = int(automation["daily_limit"]) - queue.sent_count_today()
    if remaining <= 0:
        return {"sent": 0, "skipped": "daily_limit"}
    pending = [row for row in queue.recipients() if row["status"] == "pending"][:remaining]
    if not pending:
        return {"sent": 0, "skipped": "no_pending"}
    actor = "自动邮件任务"
    lead_ids = [row["lead_id"] for row in pending]
    ids = queue.prepare(lead_ids, actor)
    rows = queue.select(ids, sendable=True)
    previews = [{
        **row,
        "subject": render_email_template(automation["subject_template"], row),
        "body": render_email_template(automation["body_template"], row),
    } for row in rows]
    batch = queue.claim(ids)
    try:
        message_ids = send_mail_batch(previews, settings=settings, batch_id=batch)
        if len(message_ids) != len(rows) or not all(message_ids):
            raise RuntimeError("邮件服务返回结果不完整")
        for row, provider_id in zip(previews, message_ids):
            queue.complete(row, row["subject"], row["body"], settings["from_email"], provider_id, actor)
    except Exception:
        raise
    return {"sent": len(rows), "sender": settings["from_email"]}

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Aibubu 工业级 ToB 销售作战中台</title>
    <style>
        :root {
            --bg: #0b0f19;
            --sidebar-bg: #111827;
            --card-bg: #1f2937;
            --border: #374151;
            --primary: #3b82f6;
            --primary-hover: #2563eb;
            --text-main: #f9fafb;
            --text-muted: #9ca3af;
            --accent: #10b981;
            --tag-cn: #0284c7;
            --tag-global: #8b5cf6;
            --tag-pending: #d97706;
            --tier-s: #ef4444;
            --tier-a: #f59e0b;
            --tier-b: #3b82f6;
            --tier-c: #6b7280;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "PingFang SC", sans-serif; }
        body { background-color: var(--bg); color: var(--text-main); display: flex; height: 100vh; overflow: hidden; }
        body.auth-checking > aside, body.auth-checking > main,
        body.auth-pending > aside, body.auth-pending > main { visibility: hidden; }
        body.auth-checking #auth-modal { display: none; }
        body.auth-checking::before {
            content: "正在恢复登录状态…";
            position: fixed;
            inset: 0;
            display: grid;
            place-items: center;
            color: var(--text-muted);
            background: var(--bg);
            z-index: 9999;
        }
        body.auth-pending #auth-modal { display: flex; }
        
        /* 左侧菜单栏 Sidebar */
        aside {
            width: 250px;
            background: var(--sidebar-bg);
            border-right: 1px solid var(--border);
            display: flex;
            flex-direction: column;
            justify-content: space-between;
            flex-shrink: 0;
        }
        .brand {
            padding: 20px 18px;
            border-bottom: 1px solid var(--border);
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .brand-logo {
            width: 34px;
            height: 34px;
            background: linear-gradient(135deg, #3b82f6, #8b5cf6);
            border-radius: 8px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-weight: 800;
            font-size: 19px;
            color: #fff;
        }
        .brand-title {
            font-size: 15px;
            font-weight: 700;
            color: #fff;
            letter-spacing: 0.5px;
        }
        .nav-list {
            list-style: none;
            padding: 14px 10px;
            display: flex;
            flex-direction: column;
            gap: 4px;
        }
        .nav-item {
            display: flex;
            align-items: center;
            gap: 12px;
            padding: 10px 14px;
            border-radius: 6px;
            color: var(--text-muted);
            cursor: pointer;
            font-size: 13.5px;
            font-weight: 500;
            transition: all 0.15s ease;
        }
        .nav-item:hover {
            background: rgba(255, 255, 255, 0.05);
            color: #fff;
        }
        .nav-item.active {
            background: var(--primary);
            color: #fff;
            font-weight: 600;
        }
        .sidebar-footer {
            padding: 16px;
            border-top: 1px solid var(--border);
            display: flex;
            align-items: center;
            justify-content: space-between;
            background: rgba(0,0,0,0.25);
        }
        .user-block {
            display: flex;
            align-items: center;
            gap: 10px;
            overflow: hidden;
        }
        .user-avatar {
            width: 36px;
            height: 36px;
            background: #2563eb;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 14px;
            font-weight: 700;
            color: #fff;
            border: 2px solid #3b82f6;
            flex-shrink: 0;
        }
        .user-info {
            display: flex;
            flex-direction: column;
            overflow: hidden;
        }
        .user-name {
            font-size: 13px;
            font-weight: 600;
            color: #fff;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        .user-role-badge {
            font-size: 11px;
            color: #10b981;
            font-weight: 500;
        }
        .btn-auth-switch {
            background: transparent;
            border: 1px solid var(--border);
            color: var(--text-muted);
            font-size: 11px;
            padding: 4px 8px;
            border-radius: 4px;
            cursor: pointer;
        }
        .btn-auth-switch:hover {
            background: #374151;
            color: #fff;
        }
        
        /* 主体区域 */
        main {
            flex: 1;
            display: flex;
            flex-direction: column;
            overflow-y: auto;
            padding: 24px 30px;
        }
        header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
        }
        .page-title {
            font-size: 21px;
            font-weight: 700;
            color: #fff;
        }
        .top-actions {
            display: flex;
            align-items: center;
            gap: 12px;
        }
        
        /* 卡片统计 */
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 12px;
            margin-bottom: 20px;
        }
        .yesterday-stats {
            margin: -8px 0 20px;
            padding: 14px 16px;
            background: var(--sidebar-bg);
            border: 1px solid var(--border);
            border-radius: 8px;
        }
        .yesterday-stats h3 { margin: 0 0 10px; font-size: 14px; }
        .recent-outreach-header { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
        .recent-outreach-scroll { overflow-x: auto; }
        .recent-outreach-table { width: 100%; min-width: 560px; border-collapse: collapse; }
        .recent-outreach-table th, .recent-outreach-table td { padding: 8px 12px; text-align: right; border-bottom: 1px solid var(--border); white-space: nowrap; }
        .recent-outreach-table th:first-child { text-align: left; }
        .recent-outreach-table td { color: #38bdf8; }
        .yesterday-stats p { color: var(--text-muted); font-size: 12px; }
        @media (max-width: 600px) { .stats-grid { grid-template-columns: repeat(2, 1fr); } }
        .stat-card {
            background: var(--card-bg);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 14px 16px;
        }
        .stat-label {
            font-size: 12px;
            color: var(--text-muted);
            margin-bottom: 4px;
        }
        .stat-value {
            font-size: 22px;
            font-weight: 700;
            color: #fff;
        }
        
        /* 筛选器容器 (一级与二级两层结构) */
        .controls-panel {
            background: var(--sidebar-bg);
            padding: 14px 16px;
            border-radius: 8px;
            border: 1px solid var(--border);
            margin-bottom: 16px;
            display: flex;
            flex-direction: column;
            gap: 12px;
        }
        .filter-tier {
            display: flex;
            flex-wrap: wrap;
            align-items: center;
            justify-content: space-between;
            gap: 10px;
        }
        .filter-tier-left {
            display: flex;
            flex-wrap: wrap;
            align-items: center;
            gap: 10px;
        }
        .filter-label {
            font-size: 12px;
            color: var(--text-muted);
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            min-width: 65px;
        }
        .filter-group {
            display: flex;
            gap: 5px;
            flex-wrap: wrap;
        }
        button, select {
            background: var(--card-bg);
            border: 1px solid var(--border);
            color: #fff;
            padding: 5px 11px;
            border-radius: 6px;
            cursor: pointer;
            font-size: 12.5px;
            outline: none;
            transition: all 0.15s ease;
        }
        button:hover, select:hover {
            background: #374151;
        }
        button.active {
            background: var(--primary);
            border-color: var(--primary);
            font-weight: 600;
        }
        button.tier-btn-s.active { background: #dc2626; border-color: #dc2626; }
        button.tier-btn-a.active { background: #d97706; border-color: #d97706; }
        button.tier-btn-b.active { background: #2563eb; border-color: #2563eb; }
        button.tier-btn-c.active { background: #4b5563; border-color: #4b5563; }

        /* 来源胶囊按钮群 */
        .source-capsules {
            display: flex;
            flex-wrap: wrap;
            gap: 6px;
            align-items: center;
        }
        .capsule-btn {
            font-size: 11.5px;
            padding: 3px 9px;
            border-radius: 14px;
            background: rgba(255,255,255,0.04);
            border: 1px solid rgba(255,255,255,0.12);
            color: #94a3b8;
            cursor: pointer;
            transition: all 0.15s ease;
            user-select: none;
        }
        .capsule-btn:hover {
            background: rgba(255,255,255,0.09);
            color: #f1f5f9;
        }
        .capsule-btn.active {
            background: #0284c7;
            border-color: #38bdf8;
            color: #ffffff;
            font-weight: 600;
        }
        .capsule-btn.active-exclude {
            background: #991b1b;
            border-color: #f87171;
            color: #ffffff;
            font-weight: 600;
        }
        .capsule-lead-btn {
            font-size: 11.5px;
            padding: 3px 9px;
            border-radius: 14px;
            background: rgba(245, 158, 11, 0.05);
            border: 1px solid rgba(245, 158, 11, 0.2);
            color: #d97706;
            cursor: pointer;
            transition: all 0.15s ease;
            user-select: none;
        }
        .capsule-lead-btn:hover {
            background: rgba(245, 158, 11, 0.15);
            color: #fbbf24;
        }
        .capsule-lead-btn.active {
            background: #d97706;
            border-color: #fbbf24;
            color: #ffffff;
            font-weight: 600;
        }
        
        .mode-toggle-btn {
            font-size: 11px;
            padding: 3px 8px;
            border-radius: 4px;
            cursor: pointer;
            border: 1px solid var(--border);
            background: #1e293b;
            color: #cbd5e1;
        }
        .mode-toggle-btn.exclude-active {
            background: #7f1d1d;
            border-color: #ef4444;
            color: #fca5a5;
            font-weight: 600;
        }

        /* 可编辑单元格交互 */
        .editable-cell {
            cursor: pointer;
            border-radius: 4px;
            padding: 2px 4px;
            transition: background 0.15s;
            display: inline-block;
        }
        .editable-cell:hover {
            background: rgba(56, 189, 248, 0.12);
            outline: 1px dashed #38bdf8;
        }
        
        .btn-export {
            background: #059669;
            border-color: #059669;
            font-weight: 600;
        }
        .btn-export:hover {
            background: #047857;
        }

        .filter-divider {
            height: 1px;
            background: var(--border);
            width: 100%;
            opacity: 0.7;
        }
        
        /* 表格样式 */
        .table-container {
            border: 1px solid var(--border);
            border-radius: 8px;
            overflow-x: auto;
            background: var(--card-bg);
        }
        table {
            width: 100%;
            border-collapse: collapse;
        }
        .pipeline-table {
            min-width: 1500px;
            table-layout: fixed;
        }
        .pipeline-table th,
        .pipeline-table td {
            overflow-wrap: anywhere;
            word-break: break-word;
            padding: 8px 9px;
            vertical-align: top;
        }
        .pipeline-table th:nth-child(1),
        .pipeline-table td:nth-child(1) { width: 68px; }
        .pipeline-table th:nth-child(2),
        .pipeline-table td:nth-child(2) { width: 230px; }
        .pipeline-table th:nth-child(3),
        .pipeline-table td:nth-child(3) { width: 125px; }
        .pipeline-table th:nth-child(4),
        .pipeline-table td:nth-child(4) { width: 290px; }
        .company-description { font-size:12px;line-height:1.6; }
        .company-description summary { cursor:pointer; }
        .company-description summary span {
            display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;
            overflow:hidden;
        }
        .company-description[open] summary span { display:block; }
        .description-source { white-space:pre-wrap;overflow-wrap:anywhere;color:#94a3b8;margin-top:6px; }
        .pipeline-table th:nth-child(5),
        .pipeline-table td:nth-child(5) { width: 65px; }
        .pipeline-table th:nth-child(6),
        .pipeline-table td:nth-child(6) { width: 135px; }
        .pipeline-table th:nth-child(7),
        .pipeline-table td:nth-child(7) { width: 125px; }
        .pipeline-table th:nth-child(8),
        .pipeline-table td:nth-child(8) { width: 118px; padding-left: 8px; padding-right: 8px; }
        .pipeline-table th:nth-child(9),
        .pipeline-table td:nth-child(9) { width: 105px; padding-left: 8px; padding-right: 8px; }
        .pipeline-table th:nth-child(10),
        .pipeline-table td:nth-child(10) { width: 180px; }
        .pipeline-table th:nth-child(11),
        .pipeline-table td:nth-child(11) { width: 270px; }
        .pipeline-table th:nth-child(12),
        .pipeline-table td:nth-child(12) { width: 150px; }
        .company-note-summary {
            display: block;
            max-width: 210px;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }
        .pitch-preview {
            display: block;
            width: 100%;
            max-height: 48px;
            overflow: hidden;
            text-align: left;
            line-height: 1.45;
            color: #93c5fd;
            background: transparent;
            border: 0;
            padding: 0;
            cursor: pointer;
        }
        .pitch-preview:hover { color: #fff; text-decoration: underline; }
        .pipeline-table .source-tag,
        .pipeline-table .source-lead-tag {
            white-space: normal;
            display: inline-block;
            line-height: 1.35;
        }
        .pipeline-table .action-cell {
            flex-wrap: wrap;
            align-items: center;
            row-gap: 6px;
        }
        th, td {
            padding: 10px 12px;
            text-align: left;
            font-size: 12.5px;
            border-bottom: 1px solid var(--border);
            vertical-align: middle;
        }
        th {
            background: #111827;
            color: var(--text-muted);
            font-weight: 600;
            font-size: 11px;
            letter-spacing: 0.5px;
            text-transform: uppercase;
            white-space: nowrap;
        }
        tr:hover {
            background: rgba(255, 255, 255, 0.02);
        }
        
        .company-link {
            color: #38bdf8;
            text-decoration: none;
            font-weight: 600;
            display: inline-flex;
            align-items: center;
            gap: 4px;
        }
        .company-link:hover {
            color: #7dd3fc;
            text-decoration: underline;
        }
        .company-link-icon {
            font-size: 11px;
            opacity: 0.8;
        }

        /* 客户等级徽章 */
        .tier-badge {
            display: inline-block;
            font-size: 11px;
            font-weight: 800;
            padding: 2px 7px;
            border-radius: 4px;
            letter-spacing: 0.5px;
            text-align: center;
        }
        .tier-S { background: rgba(239, 68, 68, 0.2); color: #f87171; border: 1px solid rgba(239, 68, 68, 0.5); }
        .tier-A { background: rgba(245, 158, 11, 0.2); color: #fbbf24; border: 1px solid rgba(245, 158, 11, 0.5); }
        .tier-B { background: rgba(59, 130, 246, 0.2); color: #60a5fa; border: 1px solid rgba(59, 130, 246, 0.5); }
        .tier-C { background: rgba(107, 114, 128, 0.2); color: #9ca3af; border: 1px solid rgba(107, 114, 128, 0.5); }
        
        .source-tag {
            font-size: 11px;
            padding: 2px 7px;
            border-radius: 4px;
            background: rgba(148, 163, 184, 0.12);
            color: #cbd5e1;
            border: 1px solid rgba(148, 163, 184, 0.25);
            white-space: nowrap;
        }
        .source-lead-tag {
            font-size: 11px;
            padding: 2px 7px;
            border-radius: 4px;
            background: rgba(245, 158, 11, 0.12);
            color: #fcd34d;
            border: 1px solid rgba(245, 158, 11, 0.25);
            white-space: nowrap;
        }
        .tag {
            font-size: 11px;
            padding: 2px 6px;
            border-radius: 4px;
            font-weight: 600;
        }
        .tag-cn {
            background: rgba(2, 132, 199, 0.2);
            color: #38bdf8;
            border: 1px solid rgba(2, 132, 199, 0.4);
        }
        .tag-global {
            background: rgba(139, 92, 246, 0.2);
            color: #c084fc;
            border: 1px solid rgba(139, 92, 246, 0.4);
        }
        .tag-pending {
            background: rgba(217, 119, 6, 0.2);
            color: #fbbf24;
            border: 1px solid rgba(217, 119, 6, 0.4);
        }
        
        .status-badge {
            font-size: 11px;
            padding: 2px 8px;
            border-radius: 12px;
            font-weight: 600;
            white-space: nowrap;
            display: inline-block;
        }
        .status-pending { background: #374151; color: #d1d5db; }
        .status-contacted { background: #0284c7; color: #bae6fd; }
        .status-sample_sent { background: #0369a1; color: #e0f2fe; }
        .status-team_trial { background: #7c3aed; color: #ede9fe; }
        .status-negotiation { background: #d97706; color: #fef3c7; }
        .status-won { background: #15803d; color: #bbf7d0; }
        .status-lost { background: #991b1b; color: #fecaca; }
        
        .action-cell {
            display: flex;
            align-items: center;
            gap: 6px;
        }
        .select-status {
            font-size: 12px;
            padding: 4px 6px;
            border-radius: 4px;
            background: #111827;
            border: 1px solid var(--border);
            color: #f8fafc;
        }
        .btn-pitch {
            background: #2563eb;
            color: #fff;
            padding: 4px 8px;
            font-size: 12px;
            border-radius: 4px;
            border: none;
            white-space: nowrap;
        }
        .btn-pitch:hover { background: #1d4ed8; }
        
        /* View 管理 */
        .view-section {
            display: none;
        }
        .view-section.active {
            display: block;
        }
        .perm-card {
            background: var(--card-bg);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 20px;
            margin-bottom: 20px;
        }
        .role-desc-grid {
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 14px;
            margin-top: 14px;
        }
        .role-desc-item {
            background: #111827;
            border: 1px solid var(--border);
            padding: 14px;
            border-radius: 6px;
        }
        .role-title {
            font-size: 13.5px;
            font-weight: 700;
            color: #60a5fa;
            margin-bottom: 6px;
        }
        .role-rules {
            font-size: 12px;
            color: var(--text-muted);
            line-height: 1.5;
        }
        
        /* Modal 通用弹窗 */
        #modal, #auth-modal, #edit-company-modal, #edit-lead-modal, #strategy-detail-modal {
            display: none;
            position: fixed;
            inset: 0;
            background: rgba(0,0,0,0.75);
            align-items: center;
            justify-content: center;
            z-index: 100;
        }
        .modal-content {
            background: var(--card-bg);
            border: 1px solid var(--border);
            border-radius: 8px;
            max-width: 650px;
            width: 90%;
            padding: 24px;
            max-height: 85vh;
            overflow-y: auto;
        }
        .modal-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 16px;
            border-bottom: 1px solid var(--border);
            padding-bottom: 12px;
        }
        .modal-body pre {
            background: #0f172a;
            border: 1px solid var(--border);
            border-radius: 6px;
            padding: 14px;
            color: #e2e8f0;
            white-space: pre-wrap;
            font-family: monospace;
            font-size: 13px;
            line-height: 1.5;
            margin-top: 10px;
        }
        .pitch-section-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-top: 16px;
            margin-bottom: 6px;
        }
        .pitch-section-header h4 {
            margin: 0;
            font-size: 13.5px;
            color: #93c5fd;
        }
        .pitch-copy-btn {
            background: #1e293b;
            border: 1px solid #475569;
            color: #cbd5e1;
            padding: 3px 8px;
            border-radius: 4px;
            font-size: 11px;
            cursor: pointer;
            transition: all 0.2s;
        }
        .pitch-copy-btn:hover {
            background: #334155;
            color: #fff;
        }
        .pitch-trans-box {
            background: rgba(30, 41, 59, 0.6);
            border-left: 3px solid #38bdf8;
            padding: 10px 14px;
            border-radius: 0 6px 6px 0;
            font-size: 13px;
            color: #cbd5e1;
            line-height: 1.5;
            margin-top: 8px;
            margin-bottom: 12px;
        }
        .pitch-lang-tabs {
            display: flex;
            gap: 8px;
            margin-bottom: 14px;
            background: #0f172a;
            padding: 4px;
            border-radius: 6px;
            border: 1px solid var(--border);
            width: fit-content;
        }
        .pitch-lang-tab {
            background: transparent;
            border: none;
            color: var(--text-muted);
            padding: 4px 12px;
            font-size: 12px;
            border-radius: 4px;
            cursor: pointer;
        }
        .pitch-lang-tab.active {
            background: #2563eb;
            color: #fff;
            font-weight: 600;
        }
        .notes-textarea {
            width: 100%;
            height: 140px;
            background: #0f172a;
            border: 1px solid var(--border);
            border-radius: 6px;
            color: #fff;
            padding: 12px;
            font-size: 13px;
            line-height: 1.5;
            outline: none;
            resize: vertical;
            margin: 12px 0 16px 0;
        }
        .notes-textarea:focus {
            border-color: var(--primary);
        }
        .modal-actions {
            display: flex;
            justify-content: flex-end;
            gap: 10px;
        }
        .btn-save {
            background: var(--primary);
            border-color: var(--primary);
            font-weight: 600;
        }

        /* 登录/注册表单控件 */
        .auth-tab-row {
            display: flex;
            gap: 8px;
            margin-bottom: 16px;
            border-bottom: 1px solid var(--border);
            padding-bottom: 8px;
        }
        .auth-tab {
            background: transparent;
            border: none;
            color: var(--text-muted);
            font-size: 14px;
            font-weight: 600;
            padding: 6px 12px;
            cursor: pointer;
        }
        .auth-tab.active {
            color: #38bdf8;
            border-bottom: 2px solid #38bdf8;
        }
        .form-group {
            margin-bottom: 14px;
        }
        .form-group label {
            display: block;
            font-size: 12.5px;
            color: var(--text-muted);
            margin-bottom: 6px;
        }
        .form-control {
            width: 100%;
            padding: 9px 12px;
            background: #0f172a;
            border: 1px solid var(--border);
            border-radius: 6px;
            color: #fff;
            font-size: 13.5px;
            outline: none;
        }
        .form-control:focus {
            border-color: var(--primary);
        }
        .btn-primary {
            width: 100%;
            padding: 10px;
            background: var(--primary);
            border: none;
            border-radius: 6px;
            color: #fff;
            font-size: 14px;
            font-weight: 600;
            cursor: pointer;
            margin-top: 8px;
        }
        .btn-primary:hover {
            background: var(--primary-hover);
        }
        .auth-msg {
            margin-top: 10px;
            font-size: 12.5px;
            text-align: center;
        }
    
        .compact-filters .filter-toolbar {display:flex;justify-content:space-between;align-items:center;gap:16px;margin-bottom:18px;flex-wrap:wrap}
        .compact-filters .filter-toolbar-actions {display:flex;align-items:center;gap:12px;flex-wrap:wrap}
        .compact-filters .filter-toolbar-actions .filter-field {display:flex;align-items:center;gap:8px}
        .compact-filters .filter-grid {display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:18px 24px}
        .compact-filters .filter-field {display:flex;flex-direction:column;gap:8px;min-width:0;color:#aab5c5;font-size:13px}
        .compact-filters .filter-toolbar-actions .filter-field {flex-direction:row}
        .compact-filters .filter-grid select {width:100%;height:40px;min-width:0;font-size:14px;border-radius:8px}
        .compact-filters select:focus-visible {outline:2px solid #60a5fa;outline-offset:2px}
        .compact-filters .filter-footer {display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-top:18px;padding-top:14px;border-top:1px solid #293344;color:#94a3b8;font-size:12px}
        @media(max-width:900px){.compact-filters .filter-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}
        @media(max-width:520px){.compact-filters .filter-grid{grid-template-columns:1fr}}
        .report-date { margin-bottom: 16px; color: var(--text-muted); font-size: 13px; }
        .touch-funnel { margin: 0 0 24px; padding: 20px; border: 1px solid #334155; border-radius: 12px; background: #111827; }
        .touch-funnel h3 { margin: 0 0 8px; font-size: 16px; }
        .touch-funnel p { color: var(--text-muted); font-size: 12px; line-height: 1.7; }
        .touch-funnel-row { display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); gap: 10px; margin: 14px 0; }
        .touch-funnel-stage { padding: 12px; background: #1f2937; border-radius: 8px; }
        .touch-funnel-stage strong { display: block; margin: 8px 0; color: #38bdf8; font-size: 24px; }
        .touch-funnel-stage small { display: block; color: var(--text-muted); line-height: 1.6; }
        @media(max-width:1100px) { .touch-funnel-row { grid-template-columns: repeat(4, minmax(0, 1fr)); } }
        @media(max-width:600px) { .touch-funnel-row { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
        .daily-funnel { margin: 0 0 24px; padding: 20px; border: 1px solid #334155; border-radius: 12px; background: #111827; overflow-x: auto; }
        .daily-funnel h3 { margin: 0 0 8px; font-size: 16px; }
        .daily-funnel p { color: var(--text-muted); font-size: 12px; }
        .daily-funnel table { width: 100%; min-width: 1050px; border-collapse: collapse; margin-top: 14px; }
        .daily-funnel th, .daily-funnel td { padding: 10px 12px; border-bottom: 1px solid #334155; text-align: right; white-space: nowrap; }
        .daily-funnel th:first-child, .daily-funnel td:first-child { text-align: left; position: sticky; left: 0; background: #111827; }
        .daily-funnel th { color: var(--text-muted); font-size: 12px; }
        .daily-funnel .rate { color: #38bdf8; font-weight: 600; }
        .prospect-drawer {
            background: #111827;
            border-left: 1px solid #374151;
            width: 680px;
            max-width: 95vw;
            height: 100vh;
            overflow-y: auto;
            box-shadow: -12px 0 48px #0005;
        }
        .prospect-drawer-header {
            position: sticky;
            top: 0;
            z-index: 2;
            padding: 22px 28px 14px;
            background: #111827;
            border-bottom: 1px solid #293344;
        }
        .prospect-drawer-title {
            display: flex;
            align-items: flex-start;
            justify-content: space-between;
            gap: 16px;
        }
        .prospect-form {
            display: grid;
            gap: 14px;
            padding: 18px 28px 0;
        }
        .prospect-form label {
            display: grid;
            gap: 6px;
            color: #cbd5e1;
            font-size: 12.5px;
        }
        .prospect-edit-actions {
            position: sticky;
            bottom: 0;
            z-index: 2;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
            margin: 4px -28px 0;
            padding: 14px 28px 18px;
            background: linear-gradient(to top, #111827 78%, rgba(17,24,39,.92));
            border-top: 1px solid #374151;
        }
        .prospect-edit-buttons {
            display: flex;
            gap: 8px;
            margin-left: auto;
        }
        .btn-save-details {
            min-width: 112px;
            background: var(--primary);
            border-color: var(--primary);
            font-weight: 700;
        }
        .btn-save-details:disabled {
            cursor: not-allowed;
            opacity: .45;
        }
        #p-message {
            min-height: 18px;
            color: #94a3b8;
            font-size: 12px;
        }
        #p-message.success { color: #34d399; }
        #p-message.error { color: #f87171; }
        .community-stats {
            display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:14px; margin-bottom:18px;
        }
        .community-grid {
            display:grid; grid-template-columns:repeat(auto-fill,minmax(310px,1fr)); gap:14px;
        }
        .community-card {
            background:var(--card-bg); border:1px solid var(--border-color); border-radius:10px;
            padding:16px; display:flex; flex-direction:column; gap:11px;
        }
        .community-card-head { display:flex; justify-content:space-between; gap:12px; align-items:flex-start; }
        .community-card h3 { margin:0; font-size:16px; }
        .community-card a { color:#60a5fa; text-decoration:none; }
        .community-meta { display:flex; flex-wrap:wrap; gap:7px; }
        .community-tag {
            border:1px solid #334155; border-radius:999px; padding:3px 8px;
            color:#cbd5e1; font-size:11px;
        }
        .community-fit-high { color:#34d399; border-color:#166534; }
        .community-card p { margin:0; color:var(--text-muted); font-size:12px; line-height:1.55; }
        .community-priority { color:#fbbf24; font-weight:700; white-space:nowrap; }
        .email-layout { display:grid; grid-template-columns:minmax(300px,.8fr) minmax(420px,1.2fr); gap:16px; }
        .email-panel { background:var(--card-bg); border:1px solid var(--border); border-radius:10px; padding:16px; }
        .email-panel h3 { margin:0 0 12px; font-size:15px; }
        .email-panel label { display:block; color:var(--text-muted); font-size:12px; margin:10px 0 5px; }
        .email-panel textarea, .email-panel input {
            width:100%; background:#111827; color:var(--text-main); border:1px solid var(--border);
            border-radius:6px; padding:9px;
        }
        .email-actions { display:flex; flex-wrap:wrap; gap:8px; margin-top:12px; align-items:center; }
        .email-status { color:var(--text-muted); font-size:12px; }
        .email-preview { white-space:pre-wrap; background:#0f172a; border:1px solid #334155; border-radius:7px; padding:10px; margin-top:8px; font-size:12px; }
        .email-table input[type=checkbox] { width:auto; }
        .email-configured { color:#34d399; }
        .email-unconfigured { color:#fbbf24; }
        .email-config-savebar {
            position:sticky;
            bottom:12px;
            z-index:5;
            display:flex;
            align-items:center;
            justify-content:space-between;
            gap:12px;
            margin-top:16px;
            padding:12px 14px;
            background:rgba(15,23,42,.97);
            border:1px solid #3b82f6;
            border-radius:10px;
            box-shadow:0 8px 24px #0008;
        }
        .email-config-savebar .email-status { color:#cbd5e1; }
        .email-config-savebar button {
            flex:0 0 auto;
            min-height:42px;
            padding:10px 18px;
            background:#2563eb;
            border:1px solid #60a5fa;
            border-radius:7px;
            color:#fff;
            font-weight:700;
            cursor:pointer;
        }
        .email-config-savebar button:hover { background:#1d4ed8; }
        .email-config-savebar button:focus-visible { outline:2px solid #bfdbfe; outline-offset:2px; }
        .email-push-badge { display:inline-block; padding:3px 9px; border-radius:999px; font-size:11px; font-weight:700; white-space:nowrap; }
        .email-push-pending { color:#cbd5e1; background:#334155; }
        .email-push-sent { color:#bbf7d0; background:#166534; }
        .email-push-warning { color:#fde68a; background:#92400e; }
        .email-push-blocked { color:#fecaca; background:#991b1b; }
        @media (max-width: 900px) {
            .community-stats { grid-template-columns:repeat(2,minmax(0,1fr)); }
            .email-layout { grid-template-columns:1fr; }
        }
        @media (max-width: 600px) {
            .email-config-savebar { align-items:stretch; flex-direction:column; }
            .email-config-savebar button { width:100%; }
        }
    </style>
</head>
<body class="auth-checking">
    <!-- 左侧菜单栏 -->
    <aside>
        <div>
            <div class="brand">
                <div class="brand-logo">A</div>
                <div>
                    <div class="brand-title">Aibubu BD 中台</div>
                    <div style="font-size: 11px; color: var(--text-muted);">ToB 大客户销售引擎</div>
                </div>
            </div>
            <ul class="nav-list">
                <li class="nav-item" id="overseas-nav" onclick="switchPipelineMarket('overseas', this)">
                    <span>🌐</span> <span>海外客户</span>
                </li>
                <li class="nav-item active" id="domestic-nav" onclick="switchPipelineMarket('domestic', this)">
                    <span>⚡</span> <span>国内客户</span>
                </li>
                <li class="nav-item" id="pending-verification-nav" onclick="switchPipelineMarket('pending_verification', this)">
                    <span>🔎</span> <span>待核验线索</span>
                </li>
                <li class="nav-item" id="email-pool-nav" onclick="switchNav('email-pool', this)">
                    <span>✉️</span> <span>邮件线索池</span>
                </li>
                <li class="nav-item" id="email-settings-nav" onclick="switchNav('email-settings', this)">
                    <span>⚙️</span> <span>邮件发送配置</span>
                </li>
                <li class="nav-item" id="permissions-nav" onclick="switchNav('permissions', this)">
                    <span>🛡️</span> <span>组织与权限管理</span>
                </li>
                <li class="nav-item" id="funnel-nav" onclick="switchNav('funnel', this)">
                    <span>📊</span> <span>跟进漏斗与数据</span>
                </li>
                <li class="nav-item" onclick="switchNav('strategy', this)">
                    <span>📝</span> <span>筛选策略存档</span>
                </li>
                <li class="nav-item" onclick="switchNav('discord', this)">
                    <span>💬</span> <span>Discord 社群看板</span>
                </li>
            </ul>
        </div>
        <div class="sidebar-footer">
            <div class="user-block">
                <div class="user-avatar" id="current-user-avatar">访</div>
                <div class="user-info">
                    <div class="user-name" id="current-user-name">未登录</div>
                    <div class="user-role-badge" id="current-user-role">● 请先登录</div>
                </div>
            </div>
            <div style="display:flex;gap:8px">
                <button class="btn-auth-switch" onclick="openAuthModal('password')">修改密码</button>
                <button class="btn-auth-switch" id="auth-switch-btn" onclick="handleAuthSwitch()">退出登录</button>
            </div>
        </div>
    </aside>

    <!-- 主视窗 -->
    <main>
        <div class="report-date">统计日期：<time id="report-date"></time>（北京时间） · <span id="data-freshness" aria-live="polite">数据状态：正在同步…</span></div>
        <!-- 视窗 1: 线索作战中台 -->
        <div id="view-pipeline" class="view-section active">
            <header>
                <div>
                    <h2 class="page-title" id="pipeline-title">国内客户</h2>
                    <div style="font-size: 13px; color: var(--text-muted); margin-top: 4px;">
                        三段解耦流水线: 智能评级 ➔ 真实决策人 ➔ ROI话术直达 (双层精细筛选)
                    </div>
                </div>
                <div class="top-actions">
                    <span style="color: var(--text-muted); font-size: 12px;">每日增量客户池 • 公开来源核查</span>
                    <button class="btn-export" onclick="exportData()">📥 导出当前筛选结果 CSV</button>
                </div>
            </header>

            <div class="stats-grid">
                <div class="stat-card">
                    <div class="stat-label">🎯 目标企业总数</div>
                    <div class="stat-value" id="stat-companies">-</div>
                </div>
                <div class="stat-card" title="悬浮或点击查看 S 级规则" onclick="openTierRule('S')" style="cursor:help">
                    <div class="stat-label">🔥 S级战略大客户</div>
                    <div class="stat-value" id="stat-tier-s" style="color:#f87171;">-</div>
                </div>
                <div class="stat-card" title="悬浮或点击查看 A 级规则" onclick="openTierRule('A')" style="cursor:help">
                    <div class="stat-label">⭐ A级核心腰部</div>
                    <div class="stat-value" id="stat-tier-a" style="color:#fbbf24;">-</div>
                </div>
                <div class="stat-card" title="悬浮或点击查看 B 级规则" onclick="openTierRule('B')" style="cursor:help">
                    <div class="stat-label">🚀 B级高意向</div>
                    <div class="stat-value" id="stat-tier-b" style="color:#60a5fa;">-</div>
                </div>
            </div>

            <section id="pipeline-touch-funnel" class="touch-funnel" aria-label="触达动作转化漏斗"></section>
            <section id="yesterday-outreach-stats" class="yesterday-stats" aria-label="最近5天触达数据"></section>

            <div class="controls-panel compact-filters">
                <div class="filter-toolbar"><strong>筛选企业</strong><div class="filter-toolbar-actions"><label class="filter-field" title=""><span>名单</span><select id="exclusion-filter" onchange="renderTable()"><option value="active">待开发 / 跟进中</option><option value="excluded">已剔除</option><option value="all">全部（含已剔除）</option></select></label><button onclick="resetFilters()">重置筛选</button></div></div>
                <div class="filter-grid"><label class="filter-field" title=""><span>市场区域</span><select id="country-filter" onchange="activeCountry=this.value;renderTable()"><option value="all">全部地区</option><option value="domestic">国内</option><option value="overseas">国外</option><option value="pending_verification">待核验</option></select></label><label class="filter-field" title="沿用现有综合预估等级，非核实后的企业规模"><span>企业级别（预估）</span><select id="tier-filter" onchange="activeTier=this.value;renderTable()"><option value="all">全部级别</option><option value="S">S级 · 超大规模</option><option value="A">A级 · 大型企业</option><option value="B">B级 · 成长企业</option><option value="C">C级 · 小微 / 潜力企业</option></select></label><label class="filter-field" title=""><span>公司来源</span><select id="source-filter" onchange="selectedSources.clear();if(this.value!=='all')selectedSources.add(this.value);activeSourceMode='include';renderTable()"><option value="all">全部来源</option></select></label><label class="filter-field" title=""><span>触达动作</span><select id="stage-filter" onchange="activeTouch='all';activeStage=this.value;renderTable()"><option value="all">全部动作</option><option value="pending">待触达</option><option value="sample_sent">已推送未回复</option><option value="contacted">已建连</option><option value="negotiation">有意向</option><option value="team_trial">已注册</option><option value="won">已充值</option><option value="lost">不触达</option></select></label><label class="filter-field" title=""><span>客户类型</span><select id="type-filter" onchange="renderTable()"><option value="all">全部类型</option><option value="电商商拍客户">电商商拍客户</option><option value="API / AI 应用客户">API / AI 应用客户</option><option value="广告 / 代运营机构">广告 / 代运营机构</option><option value="unknown">待分类</option></select></label><label class="filter-field" title=""><span>跟进情况</span><select id="due-filter" onchange="renderTable()"><option value="all">全部情况</option><option value="due">今天到期 / 已逾期</option><option value="unclaimed">未领取</option></select></label><label class="filter-field" title=""><span>行业</span><select id="industry-filter" onchange="renderTable()"><option value="all">全部行业</option></select></label><label class="filter-field" title=""><span>每月预计消耗</span><select id="spend-filter" onchange="renderTable()"><option value="all">全部消耗</option><option value="unknown">待评估</option><option value="small">5,000 元以下</option><option value="medium">5,000–2万元（不含）</option><option value="large">2–5万元（不含）</option><option value="enterprise">5万元及以上</option></select></label></div>
                <div class="filter-footer"><span>企业级别沿用现有综合预估；消耗单位为人民币 / 月，非已确认预算。</span><span id="filter-count" aria-live="polite">加载中…</span></div>
            </div>

            <!-- 数据总表 -->
            <div class="table-container">
                <table class="pipeline-table">
                    <thead>
                        <tr>
                            <th>等级</th>
                            <th>企业名称 (官网直达)</th>
                            <th>公司来源</th>
                            <th id="company-description-heading">赛道/类目</th>
                            <th>市场</th>
                            <th>创始人 / X 主页</th>
                            <th>直连渠道 (微信/电话/邮箱)</th>
                            <th>线索来源</th>
                            <th>当前状态</th>
                            <th>剔除原因</th>
                            <th style="min-width: 250px;">触达操作区域 (实时切换与话术)</th>
                            <th style="min-width: 320px;">推送话术</th>
                        </tr>
                    </thead>
                    <tbody id="pipeline-body">
                        <!-- 动态渲染 -->
                    </tbody>
                </table>
            </div>
        </div>

        <div id="view-email-pool" class="view-section">
            <header>
                <div>
                    <h2 class="page-title">邮件线索池</h2>
                    <div class="email-status">与国内、海外客户池独立并列，自动读取有效邮箱。已发送客户显示“已推送”；待核验市场线索只展示、不可发送。</div>
                </div>
                <div id="email-provider-status" class="email-status">正在检查 Resend 配置…</div>
            </header>
            <section id="feishu-mail-import-panel" class="email-panel" hidden style="margin:16px 0">
                <h3>同步飞书邮件表到 CRM</h3>
                <p class="email-status">按公司名称或官网域名匹配企业；已有公司只补空字段。有效邮箱按地址去重，冲突行跳过并列出原因。导入只写 CRM，不会发送邮件。</p>
                <button id="feishu-mail-import-preview" onclick="previewFeishuMailImport()">检查飞书表并预览</button>
                <span id="feishu-mail-import-status" class="email-status"></span>
                <div id="feishu-mail-import-summary" class="email-status" aria-live="polite"></div>
                <div id="feishu-mail-import-conflicts"></div>
                <button id="feishu-mail-import-apply" onclick="applyFeishuMailImport()" hidden disabled>确认导入可导入线索</button>
            </section>
            <div>
                <section class="email-panel">
                    <h3>编写、预览并发送</h3>
                    <label for="email-sender">本批发件邮箱</label><select id="email-sender" onchange="updateMailSenderStatus()"></select>
                    <div class="email-status">变量：<code>{{name}}</code>、<code>{{company}}</code>、<code>{{email}}</code></div>
                    <label for="email-subject">邮件标题</label>
                    <input id="email-subject" value="{{company}} × Aibubu：更高效的 AI 图片与视频工作流">
                    <label for="email-body">邮件正文</label>
                    <textarea id="email-body" rows="10">Hi {{name}},

I’m reaching out from Aibubu. We provide an AI workspace and API for creating product images and short videos at scale.

I’d love to learn how {{company}} currently produces visual content and see whether a small test would be useful.

Best,
Aibubu Team</textarea>
                    <div class="email-actions">
                        <button onclick="previewSelectedEmails()">预览所选邮件</button>
                        <button class="btn-save" id="email-send-btn" onclick="sendSelectedEmails()">一键发送所选</button>
                        <span id="email-selection-count" class="email-status">已选择 0 人</span>
                    </div>
                    <div id="email-preview-list"></div>
                    <div id="email-send-result" class="email-status"></div>
                </section>
            </div>
            <section class="email-panel" style="margin-top:16px">
                <h3>创建邮件推送包</h3>
                <p class="email-status">按线索入库日期筛选后，批量选择或逐条勾选联系人。包创建后可按该包预览并发送。</p>
                <div class="filter-grid">
                    <label>入库日期从<input type="date" id="package-date-from" onchange="renderPackageCandidates()"></label>
                    <label>入库日期至<input type="date" id="package-date-to" onchange="renderPackageCandidates()"></label>
                    <button onclick="togglePackageCandidates(true)">全选当前筛选</button>
                    <button onclick="togglePackageCandidates(false)">取消全选</button>
                </div>
                <div id="package-candidate-count" class="email-status"></div>
                <div class="table-container"><table class="email-table">
                    <thead><tr><th>选择</th><th>入库日期</th><th>联系人</th><th>公司</th><th>邮箱</th><th>状态</th></tr></thead>
                    <tbody id="package-candidate-body"></tbody>
                </table></div>
                <label for="email-package-title">推送包名称</label>
                <input id="email-package-title" maxlength="120" placeholder="例如：AI 视频工具创始人首轮联系">
                <div class="email-actions">
                    <button id="email-package-create" onclick="createEmailPackage()">创建推送包（仅保存草稿）</button>
                    <span id="email-package-result" class="email-status"></span>
                </div>
                <div id="email-package-list" class="email-status"></div>
                <div id="email-package-preview"></div>
            </section>
            <section class="email-panel" style="margin-top:16px">
                <h3>邮箱客户线索</h3>
                <div class="filter-grid">
                    <label>市场<select id="email-market-filter" onchange="renderEmailRecipients()"><option value="">全部市场</option><option value="domestic">国内</option><option value="overseas">海外</option><option value="pending_verification">待核验</option></select></label>
                    <label>推送状态<select id="email-status-filter" onchange="renderEmailRecipients()"><option value="pending">未推送（可发送）</option><option value="sent">已推送</option><option value="sending">发送中 / 待核实</option><option value="unsubscribed">已退订</option><option value="removed">已移除</option><option value="email_changed">邮箱变更待核实</option><option value="">全部</option></select></label>
                    <label>公司 / 姓名 / 邮箱<input id="email-search" oninput="renderEmailRecipients()" placeholder="搜索客户"></label>
                    <button onclick="fetchEmailProspects()">刷新客户及发送状态</button>
                </div>
                <div id="email-recipient-count" class="email-status"></div>
            </section>
            <div class="table-container" style="margin-top:16px">
                <table class="email-table">
                    <thead><tr>
                        <th><input type="checkbox" id="email-select-all" onchange="toggleAllEmailProspects(this.checked)"></th>
                        <th>邮箱</th><th>姓名</th><th>公司 / 市场</th><th>推送状态</th><th>最近发送</th><th>最近发件邮箱</th><th>操作</th>
                    </tr></thead>
                    <tbody id="email-prospect-body"></tbody>
                </table>
            </div>
            <section class="email-panel" style="margin-top:16px"><h3>独立发送记录（最近 500 条）</h3><div id="email-history"></div></section>
        </div>

        <div id="view-email-settings" class="view-section">
            <header>
                <div>
                    <h2 class="page-title">邮件发送配置</h2>
                    <div class="email-status">这里管理自有 SMTP 发信、备用 Resend 配置和每日自动发送规则，不展示或修改客户线索。</div>
                </div>
            </header>
            <section class="email-panel">
                <h3>自有 SMTP 发件服务</h3>
                <p>选择 SMTP 后，邮件从你自己的企业邮箱服务器发出。腾讯、阿里、网易等企业邮箱通常需要使用 SMTP 专用密码或授权码，不要填写网页登录密码。</p>
                <p>页面保存的 SMTP 密码会加密保存。服务器必须设置 <code>MAIL_CONFIG_KEY</code>；没有它只能预览，不能保存密码或发送。</p>
                <div id="email-config-fields">
                    <label for="mail-edit-sender">管理发件邮箱（仅管理员可保存）</label><select id="mail-edit-sender" onchange="loadMailSender()"></select>
                    <label for="mail-provider">邮件服务</label><select id="mail-provider" onchange="toggleMailProvider()"><option value="smtp">自有 SMTP</option><option value="resend">Resend（备用）</option></select>
                    <label for="mail-smtp-host">SMTP 服务器</label><input id="mail-smtp-host" placeholder="smtp.example.com">
                    <label for="mail-smtp-port">SMTP 端口</label><input id="mail-smtp-port" type="number" min="1" max="65535" value="465">
                    <label for="mail-smtp-security">SMTP 加密</label><select id="mail-smtp-security"><option value="ssl">SSL/TLS（465）</option><option value="starttls">STARTTLS（587）</option><option value="none">不加密（不推荐）</option></select>
                    <label for="mail-smtp-username">SMTP 账号</label><input id="mail-smtp-username" type="email" placeholder="sales@example.com">
                    <label for="mail-smtp-password">SMTP 密码 / 授权码（留空保留原密码）</label><input id="mail-smtp-password" type="password" autocomplete="new-password">
                    <label for="mail-sending-domain">发件域名</label><input id="mail-sending-domain" type="text" placeholder="mail.your-domain.com" autocomplete="off">
                    <p>填写发件邮箱域名。SMTP 服务器、发件邮箱和账号应属于同一个企业邮箱服务。</p>
                    <label>发件邮箱（已验证域名）</label><input id="mail-from-email" type="email" placeholder="sales@your-domain.com">
                    <label>发件人名称</label><input id="mail-from-name" maxlength="100">
                    <label>回复邮箱 Reply-To（可选）</label><input id="mail-reply-to" type="email">
                    <label>单批发送上限（1–50）</label><input id="mail-batch-limit" type="number" min="1" max="50" value="20">
                    <label>Resend API Key（仅备用 Resend 模式使用）</label><input id="mail-api-key" type="password" autocomplete="new-password">
                </div>
                <div class="email-config-savebar" role="group" aria-label="保存邮件发件配置">
                    <span class="email-status">参数填写完成后点击保存。保存只更新配置，不会发送邮件。</span>
                    <button id="mail-save-settings" onclick="saveMailSettings()">保存发件配置</button>
                </div>
                <div id="email-config-result" class="email-status"></div>
                <details><summary>第一次配置 SMTP？按这几步完成</summary>
                    <ol>
                        <li>从企业邮箱后台获取 SMTP 主机、端口、SSL/STARTTLS 和授权码。</li>
                        <li>填写 SMTP 账号、发件邮箱和 Reply-To；保存不会发送邮件。</li>
                        <li>先用 1 个测试收件人预览，确认无误后再批量发送。</li>
                    </ol>
                    <p>不要把 SMTP 密码或授权码发到聊天中。自动发送只会在你打开开关后运行。</p>
                </details>
                <p>参数齐全不代表 SMTP 已连通；发送成功表示服务器接受，不代表送达或已读。</p>
            </section>
            <section class="email-panel" style="margin-top:16px">
                <h3>每日自动发送</h3>
                <p>自动任务按北京时间运行，每天只取未发送、未退订、未移除的联系人；每日上限 1–50。</p>
                <label><input id="mail-auto-enabled" type="checkbox"> 开启每日自动发送</label>
                <label>使用发件邮箱<select id="mail-auto-sender"></select></label>
                <label>每日上限<input id="mail-auto-limit" type="number" min="1" max="50" value="20"></label>
                <label>发送时间（小时）<input id="mail-auto-hour" type="number" min="0" max="23" value="9"></label>
                <label>标题模板<input id="mail-auto-subject"></label>
                <label>正文模板<textarea id="mail-auto-body" rows="8"></textarea></label>
                <button id="mail-save-automation" onclick="saveMailAutomation()">保存自动发送规则（不会立即发送）</button>
                <div id="mail-automation-result" class="email-status"></div>
            </section>
        </div>

        <!-- 视窗 2: 组织与权限管理 -->
        <div id="view-permissions" class="view-section">
            <header>
                <div>
                    <h2 class="page-title">组织架构与权限管理</h2>
                    <div style="font-size: 13px; color: var(--text-muted); margin-top: 4px;">
                        包含用户注册登录鉴权、四级角色分权与销售团队成员配置
                    </div>
                </div>
                <div>
                    <button class="btn-save" onclick="openAuthModal('register')">+ 创建团队账号</button>
                </div>
            </header>

            <div class="perm-card">
                <h3 style="font-size: 15px; margin-bottom: 8px;">四级权限体系与角色说明</h3>
                <div class="role-desc-grid">
                    <div class="role-desc-item">
                        <div class="role-title">👑 超级管理员 (Admin)</div>
                        <div class="role-rules">
                            • 全局数据库读写与导出<br>
                            • 企业导入与批量删除<br>
                            • 分配销售人员与调配客户<br>
                            • 管理全部账号密码与角色权限
                        </div>
                    </div>
                    <div class="role-desc-item">
                        <div class="role-title">💼 销售总监 (Sales Lead)</div>
                        <div class="role-rules">
                            • 跨大区客户数据查看<br>
                            • 查看全员跟进进度与漏斗<br>
                            • 审批商务报价与折扣额度<br>
                            • 支持批量导出跟进表
                        </div>
                    </div>
                    <div class="role-desc-item">
                        <div class="role-title">📞 销售专员 (Sales Rep)</div>
                        <div class="role-rules">
                            • 查看分配的国内/海外客户<br>
                            • 生成定制话术与样张建议<br>
                            • 切换触达状态与填写沟通备注<br>
                            • 推进至团队空间试用阶段
                        </div>
                    </div>
                    <div class="role-desc-item">
                        <div class="role-title">👀 运营观察员 (Observer)</div>
                        <div class="role-rules">
                            • 仅具有数据只读查看权限<br>
                            • 不可修改客户状态与备注<br>
                            • 不可导出核心客户隐私联系方式<br>
                            • 供交付支持/客服团队查验
                        </div>
                    </div>
                </div>
            </div>

            <div class="table-container">
                <table>
                    <thead>
                        <tr>
                            <th>用户账号</th>
                            <th>显示姓名</th>
                            <th>权限角色</th>
                            <th>电子邮箱</th>
                            <th>联系电话</th>
                            <th>账号状态</th>
                            <th>角色权限调整 (实时生效)</th>
                        </tr>
                    </thead>
                    <tbody id="users-body">
                        <!-- 动态渲染用户列表 -->
                    </tbody>
                </table>
            </div>
        </div>

        <!-- 视窗 3: 跟进漏斗与数据 -->
        <div id="view-funnel" class="view-section">
            <header>
                <div>
                    <h2 class="page-title">销售漏斗分析与大盘数据</h2>
                    <div style="font-size: 13px; color: var(--text-muted); margin-top: 4px;">
                        按企业统计跟进进度、阶段分布与待办；推进比例按当前状态推算，非历史转化率
                    </div>
                </div>
            </header>
            <div class="controls-panel">
                <label>市场 <select id="funnel-country" onchange="renderFunnelView()"><option value="all">全部市场</option><option value="domestic">国内</option><option value="overseas">海外</option></select></label>
                <label>等级 <select id="funnel-tier" onchange="renderFunnelView()"><option value="all">全部等级</option><option>S</option><option>A</option><option>B</option><option>C</option></select></label>
                <button onclick="fetchPipeline()">刷新数据</button>
            </div>
            <section id="daily-funnel-table" class="daily-funnel" aria-label="最近30天触达动作数据表"></section>
            <div id="funnel-container" class="perm-card">
                正在统计漏斗节点...
            </div>
        </div>

        <div id="view-strategy" class="view-section">
            <header>
                <div>
                    <h2 class="page-title">筛选策略存档</h2>
                    <div style="font-size:13px;color:var(--text-muted);margin-top:4px">
                        保存策略文本快照；同名策略每次保存自动生成下一版本，历史版本不会被覆盖。
                    </div>
                </div>
            </header>
            <div class="perm-card">
                <h3>新建策略版本</h3>
                <form id="strategy-form" onsubmit="saveStrategy(event)">
                    <label>策略名称
                        <input class="form-control" id="strategy-title" maxlength="120" required placeholder="例如：海外 DTC 创始人筛选策略">
                    </label>
                    <label>策略文本
                        <textarea class="form-control" id="strategy-content" rows="12" required placeholder="记录筛选条件、目标画像、排除规则、来源和执行说明"></textarea>
                    </label>
                    <div style="display:flex;justify-content:flex-end;gap:10px;margin-top:12px">
                        <span id="strategy-message" aria-live="polite"></span>
                        <button class="btn-primary" type="submit">保存为新版本</button>
                    </div>
                </form>
            </div>
            <div class="controls-panel">
                <div class="filter-toolbar"><strong>历史存档</strong><button onclick="resetStrategyFilters()">重置筛选</button></div>
                <div class="filter-grid">
                    <label class="filter-field"><span>版本</span><select id="strategy-version-filter" onchange="fetchStrategies()"><option value="">全部版本</option></select></label>
                    <label class="filter-field"><span>更新时间从</span><input id="strategy-start-date" type="date" onchange="fetchStrategies()"></label>
                    <label class="filter-field"><span>更新时间至</span><input id="strategy-end-date" type="date" onchange="fetchStrategies()"></label>
                </div>
            </div>
            <div class="table-container">
                <table class="pipeline-table">
                    <thead><tr><th>策略名称</th><th>版本</th><th>更新时间</th><th>保存人</th><th>策略文本</th></tr></thead>
                    <tbody id="strategy-table-body"><tr><td colspan="5">正在加载…</td></tr></tbody>
                </table>
            </div>
        </div>

        <div id="view-discord" class="view-section">
            <header>
                <div>
                    <h2 class="page-title">Discord 社群观察看板</h2>
                    <div style="font-size:13px;color:var(--text-muted);margin-top:4px">
                        用个人号先观察、参与和学习；记录加入状态、社群属性、推荐频道与匹配度。
                    </div>
                </div>
                <button class="btn-save" onclick="document.getElementById('discord-form-card').style.display='block'">+ 添加 / 更新社群</button>
            </header>
            <div class="community-stats">
                <div class="stat-card"><div class="stat-label">社群总数</div><div class="stat-value" id="discord-total">-</div></div>
                <div class="stat-card"><div class="stat-label">已加入</div><div class="stat-value" id="discord-joined" style="color:#34d399">-</div></div>
                <div class="stat-card"><div class="stat-label">高匹配</div><div class="stat-value" id="discord-high-fit" style="color:#60a5fa">-</div></div>
                <div class="stat-card"><div class="stat-label">中文优先</div><div class="stat-value" id="discord-chinese" style="color:#fbbf24">-</div></div>
            </div>
            <div id="discord-form-card" class="perm-card" style="display:none">
                <h3>添加或同步社群</h3>
                <form id="discord-form" onsubmit="saveDiscordCommunity(event)">
                    <div class="filter-grid">
                        <label class="filter-field"><span>社群名称</span><input id="discord-name" required></label>
                        <label class="filter-field"><span>邀请链接</span><input id="discord-url" type="url" required></label>
                        <label class="filter-field"><span>方向</span><input id="discord-focus" placeholder="AI 图像 / 视频"></label>
                        <label class="filter-field"><span>语言</span><select id="discord-language"><option>英文</option><option>中文</option><option>中英双语</option></select></label>
                        <label class="filter-field"><span>匹配度</span><select id="discord-fit"><option>高</option><option selected>中</option><option>低</option></select></label>
                        <label class="filter-field"><span>加入状态</span><select id="discord-status"><option>已加入</option><option>待加入</option><option>观察中</option><option>暂停</option></select></label>
                        <label class="filter-field"><span>使用账号</span><select id="discord-account"><option>个人号</option><option>官方号</option></select></label>
                        <label class="filter-field"><span>优先级</span><input id="discord-priority" type="number" min="1" value="99"></label>
                        <label class="filter-field"><span>用户画像</span><input id="discord-audience"></label>
                        <label class="filter-field"><span>推荐频道</span><input id="discord-channels" placeholder="#showcase / #feedback / #tools"></label>
                    </div>
                    <label style="display:block;margin-top:12px">观察备注<textarea class="form-control" id="discord-notes" rows="3"></textarea></label>
                    <div style="display:flex;justify-content:flex-end;gap:10px;margin-top:12px">
                        <span id="discord-message" aria-live="polite"></span>
                        <button type="button" onclick="document.getElementById('discord-form-card').style.display='none'">取消</button>
                        <button class="btn-primary" type="submit">保存社群</button>
                    </div>
                </form>
            </div>
            <div class="controls-panel">
                <div class="filter-toolbar"><strong>社群筛选</strong><button onclick="resetDiscordFilters()">重置筛选</button></div>
                <div class="filter-grid">
                    <label class="filter-field"><span>语言</span><select id="discord-language-filter" onchange="fetchDiscordCommunities()"><option value="">全部语言</option><option>中文</option><option>英文</option><option>中英双语</option></select></label>
                    <label class="filter-field"><span>匹配度</span><select id="discord-fit-filter" onchange="fetchDiscordCommunities()"><option value="">全部匹配度</option><option>高</option><option>中</option><option>低</option></select></label>
                    <label class="filter-field"><span>加入状态</span><select id="discord-status-filter" onchange="fetchDiscordCommunities()"><option value="">全部状态</option><option>已加入</option><option>待加入</option><option>观察中</option><option>暂停</option></select></label>
                </div>
            </div>
            <div id="discord-community-grid" class="community-grid"><div class="perm-card">正在加载…</div></div>
        </div>
    </main>

    <!-- 弹窗 1: 直触话术 -->
    <div id="modal">
        <div class="modal-content">
            <div class="modal-header">
                <h3 id="modal-title">直触话术方案</h3>
                <button onclick="closeModal()">✕ 关闭</button>
            </div>
            <div class="modal-body" id="modal-body"></div>
        </div>
    </div>

    <div id="strategy-detail-modal" onclick="if(event.target===this) closeStrategyDetail()">
        <div class="modal-content" style="max-width:760px">
            <div class="modal-header">
                <h3 id="strategy-detail-title">策略文本</h3>
                <button onclick="closeStrategyDetail()">✕ 关闭</button>
            </div>
            <div class="modal-body">
                <div id="strategy-detail-meta" style="color:var(--text-muted);font-size:12px;margin-bottom:14px"></div>
                <pre id="strategy-detail-content" style="white-space:pre-wrap;word-break:break-word;line-height:1.7;font-family:inherit"></pre>
            </div>
        </div>
    </div>

    <!-- 弹窗 2.1: 编辑企业主体信息 (Company) -->
    <div id="edit-company-modal">
        <div class="modal-content" style="max-width: 600px;">
            <div class="modal-header">
                <h3 id="edit-company-title">编辑企业主体信息</h3>
                <button onclick="closeEditCompanyModal()">✕ 关闭</button>
            </div>
            <form onsubmit="handleSaveCompany(event)">
                <input type="hidden" id="edit-comp-id">
                <div style="display:grid; grid-template-columns: 1fr 1fr; gap:12px;">
                    <div class="form-group" style="grid-column: 1 / -1;">
                        <label>企业名称</label>
                        <input type="text" id="edit-comp-name" class="form-control" required>
                    </div>
                    <div class="form-group">
                        <label>客户等级</label>
                        <select id="edit-comp-tier" class="form-control">
                            <option value="S">S级 战略客户</option>
                            <option value="A">A级 核心腰部</option>
                            <option value="B">B级 高意向</option>
                            <option value="C">C级 潜力探索</option>
                        </select>
                    </div>
                    <div class="form-group">
                        <label>市场区域</label>
                        <select id="edit-comp-country" class="form-control">
                            <option value="domestic">🇨🇳 国内电商/千川</option>
                            <option value="overseas">🌐 海外 DTC / Agency</option>
                        </select>
                    </div>
                    <div class="form-group">
                        <label>赛道/类目</label>
                        <input type="text" id="edit-comp-industry" class="form-control" required placeholder="例如: 美妆个护, 广告代投">
                    </div>
                    <div class="form-group">
                        <label>企业规模</label>
                        <input type="text" id="edit-comp-scale" class="form-control" placeholder="例如: 50-150人">
                    </div>
                    <div class="form-group">
                        <label>公司来源渠道</label>
                        <input type="text" id="edit-comp-source" class="form-control" required placeholder="例如: 千川核心服务商榜单">
                    </div>
                    <div class="form-group">
                        <label>所在城市/地区</label>
                        <input type="text" id="edit-comp-city" class="form-control" placeholder="例如: 杭州, 广州, 深圳">
                    </div>
                    <div class="form-group" style="grid-column: 1 / -1;">
                        <label>官网地址 (URL)</label>
                        <input type="text" id="edit-comp-website" class="form-control" placeholder="例如: https://example.com">
                    </div>
                </div>
                <div class="modal-actions" style="margin-top:16px;">
                    <button type="button" onclick="closeEditCompanyModal()">取消</button>
                    <button type="submit" class="btn-save">💾 保存企业更新</button>
                </div>
            </form>
        </div>
    </div>

    <!-- 弹窗 2.2: 编辑决策人线索信息 (Lead) -->
    <div id="edit-lead-modal">
        <div class="modal-content" style="max-width: 820px;">
            <div class="modal-header">
                <h3 id="edit-lead-title">编辑决策人与触达联系方式</h3>
                <button onclick="closeEditLeadModal()">✕ 关闭</button>
            </div>
            <form onsubmit="handleSaveLead(event)">
                <input type="hidden" id="edit-lead-id">
                <div style="display:grid; grid-template-columns: 1fr 1fr; gap:12px;">
                    <div class="form-group">
                        <label>决策人姓名</label>
                        <input type="text" id="edit-lead-name" class="form-control" required placeholder="必须是公开资料中的真实姓名">
                    </div>
                    <div class="form-group">
                        <label>真实职位</label>
                        <input type="text" id="edit-lead-title" class="form-control" required placeholder="创始人 / 联合创始人 / CEO">
                    </div>
                    <div class="form-group" style="grid-column: 1 / -1;">
                        <label>创始人身份来源 URL</label>
                        <input type="url" id="edit-lead-identity-url" class="form-control" placeholder="企业官网或创始人本人公开页面">
                    </div>
                    <div class="form-group" style="grid-column: 1 / -1;">
                        <label>创始人身份证据原文</label>
                        <textarea id="edit-lead-identity-evidence" class="form-control" rows="2" placeholder="粘贴能证明姓名和职位的公开原文，不要推测"></textarea>
                    </div>
                    <div class="form-group">
                        <label>公开商务手机号</label>
                        <input type="text" id="edit-lead-phone" class="form-control" placeholder="138xxxxxxxx">
                    </div>
                    <div class="form-group">
                        <label>手机号可信等级</label>
                        <select id="edit-lead-phone-confidence" class="form-control"><option value="">未核验</option><option>A</option><option>B</option><option>C</option><option>D</option></select>
                    </div>
                    <div class="form-group" style="grid-column: 1 / -1;">
                        <label>手机号来源 URL</label>
                        <input type="url" id="edit-lead-phone-url" class="form-control" placeholder="公开页面 URL">
                    </div>
                    <div class="form-group" style="grid-column: 1 / -1;">
                        <label>手机号证据原文</label>
                        <textarea id="edit-lead-phone-evidence" class="form-control" rows="2"></textarea>
                    </div>
                    <div class="form-group">
                        <label>微信号码</label>
                        <input type="text" id="edit-lead-wechat" class="form-control" placeholder="微信ID">
                    </div>
                    <div class="form-group">
                        <label>微信可信等级</label>
                        <select id="edit-lead-wechat-confidence" class="form-control"><option value="">未核验</option><option>A</option><option>B</option><option>C</option><option>D</option></select>
                    </div>
                    <div class="form-group" style="grid-column: 1 / -1;">
                        <label>微信来源 URL</label>
                        <input type="url" id="edit-lead-wechat-url" class="form-control" placeholder="公开页面 URL">
                    </div>
                    <div class="form-group" style="grid-column: 1 / -1;">
                        <label>微信证据原文</label>
                        <textarea id="edit-lead-wechat-evidence" class="form-control" rows="2"></textarea>
                    </div>
                    <div class="form-group" style="grid-column: 1 / -1;">
                        <label><input type="checkbox" id="edit-lead-same-contact"> 公开原文明确写明“手机号同微信”</label>
                    </div>
                    <div class="form-group">
                        <label>Aibubu 适配评分（0–100）</label>
                        <input type="number" min="0" max="100" id="edit-lead-fit-score" class="form-control" value="0">
                    </div>
                    <div class="form-group">
                        <label>最近核验日期</label>
                        <input type="date" id="edit-lead-verified-at" class="form-control">
                    </div>
                    <div class="form-group">
                        <label>工作邮箱</label>
                        <input type="email" id="edit-lead-email" class="form-control" placeholder="name@company.com">
                    </div>
                    <div class="form-group">
                        <label>线索来源渠道</label>
                        <input type="text" id="edit-lead-source" class="form-control" placeholder="如: 脉脉直通, 企查查">
                    </div>
                    <div class="form-group" style="grid-column: 1 / -1;">
                        <label>LinkedIn 主页链接</label>
                        <input type="text" id="edit-lead-linkedin" class="form-control" placeholder="https://linkedin.com/in/...">
                    </div>
                    <div class="form-group" style="grid-column: 1 / -1;">
                        <label>创始人 X 主页链接</label>
                        <input type="text" id="edit-lead-x" class="form-control" placeholder="https://x.com/...">
                    </div>
                    <div class="form-group" style="grid-column: 1 / -1;">
                        <strong id="edit-lead-verification-status">核验状态：待审核</strong>
                        <p style="font-size:12px;color:var(--text-muted)">手机号和微信分别保存来源。只有身份、手机号、微信均为 A/B 级且适配评分不低于 70，才会自动进入“可销售触达”。</p>
                    </div>
                </div>
                <div class="modal-actions" style="margin-top:16px;">
                    <button type="button" onclick="closeEditLeadModal()">取消</button>
                    <button type="submit" class="btn-save">💾 保存决策人更新</button>
                </div>
            </form>
        </div>
    </div>

    <!-- 弹窗 3: 完整的用户登录与注册鉴权系统 -->
    <div id="auth-modal">
        <div class="modal-content" style="max-width: 440px;">
            <div class="modal-header">
                <h3 id="auth-modal-title">团队用户身份验证</h3>
                <button id="auth-close-btn" onclick="closeAuthModal()">✕ 关闭</button>
            </div>
            
            <div class="auth-tab-row">
                <button class="auth-tab active" id="tab-login-btn" onclick="switchAuthTab('login')">账号登录</button>
                <button class="auth-tab" id="tab-register-btn" onclick="switchAuthTab('register')">管理员创建账号</button>
            </div>

            <!-- 登录表单 -->
            <form id="login-form" onsubmit="handleLogin(event)">
                <div class="form-group">
                    <label>用户名 / 账号</label>
                    <input type="text" id="login-username" class="form-control" required placeholder="请输入用户名">
                </div>
                <div class="form-group">
                    <label>登录密码</label>
                    <input type="password" id="login-password" class="form-control" required placeholder="请输入密码">
                </div>
                <button type="submit" class="btn-primary">立即登录系统</button>
                <div id="login-msg" class="auth-msg"></div>
            </form>

            <!-- 注册表单 -->
            <form id="register-form" style="display:none;" onsubmit="handleRegister(event)">
                <div class="form-group">
                    <label>用户名 (英文字符/数字)</label>
                    <input type="text" id="reg-username" class="form-control" required placeholder="例如: sales_hangzhou">
                </div>
                <div class="form-group">
                    <label>初始登录密码</label>
                    <input type="password" id="reg-password" class="form-control" required minlength="8" placeholder="至少 8 位密码">
                </div>
                <div class="form-group">
                    <label>真实姓名 / 显示昵称</label>
                    <input type="text" id="reg-name" class="form-control" required placeholder="例如: 杭州拓展专员-小李">
                </div>
                <div class="form-group">
                    <label>初始权限角色</label>
                    <select id="reg-role" class="form-control">
                        <option value="sales_rep">📞 销售专员 (Sales Rep)</option>
                        <option value="sales_lead">💼 销售总监 (Sales Lead)</option>
                        <option value="observer">👀 运营观察员 (Observer)</option>
                        <option value="admin">👑 超级管理员 (Admin)</option>
                    </select>
                </div>
                <div class="form-group">
                    <label>工作邮箱 (选填)</label>
                    <input type="email" id="reg-email" class="form-control" placeholder="li@aibubu.com">
                </div>
                <div class="form-group">
                    <label>手机号码 (选填)</label>
                    <input type="text" id="reg-phone" class="form-control" placeholder="13800000000">
                </div>
                <button type="submit" class="btn-primary" style="background:#10b981;">完成注册并入库</button>
                <div id="reg-msg" class="auth-msg"></div>
            </form>

            <form id="password-form" style="display:none;" onsubmit="handleChangePassword(event)">
                <div class="form-group"><label>当前密码</label><input type="password" id="current-password" class="form-control" required></div>
                <div class="form-group"><label>新密码</label><input type="password" id="new-password" class="form-control" minlength="10" required placeholder="至少 10 位"></div>
                <div class="form-group"><label>确认新密码</label><input type="password" id="confirm-password" class="form-control" minlength="10" required></div>
                <button type="submit" class="btn-primary">更新登录密码</button>
                <div id="password-msg" class="auth-msg"></div>
            </form>
        </div>
    </div>

    <div id="prospect-modal" style="display:none;position:fixed;inset:0;background:#0006;z-index:2000;align-items:stretch;justify-content:flex-end" onclick="if(event.target===this) closeProspectDetails()">
        <div role="dialog" aria-modal="true" aria-labelledby="prospect-name" class="prospect-drawer">
            <div class="prospect-drawer-header">
                <div class="prospect-drawer-title">
                    <div><h2 id="prospect-name"></h2><p id="prospect-summary" style="margin-top:8px;color:#9ca3af"></p></div>
                    <button onclick="closeProspectDetails()">关闭</button>
                </div>
            </div>
            <form id="prospect-form" class="prospect-form" onsubmit="saveProspectDetails(event)">
                <label>客户类型 <select id="p-type"><option value="">待分类</option><option>电商商拍客户</option><option>API / AI 应用客户</option><option>广告 / 代运营机构</option></select></label>
                <label>企业描述 <textarea class="form-control" id="p-description" rows="4" placeholder="服务对象、具体业务、公开业务规模；未核实的数据请注明"></textarea></label>
                <label>描述来源与核验日期 <textarea class="form-control" id="p-description-source" rows="2" placeholder="官网或 X 主页链接、核验日期与未确认事项"></textarea></label>
                <label>客户情况与备注 <textarea class="form-control" id="p-notes" rows="4" placeholder="记录客户当前痛点、沟通情况、采购计划和下一步跟进安排"></textarea></label>
                <label>涉及的模型 <input class="form-control" id="p-models" placeholder="如 Kling、GPT Image；仅填写有来源的信息"></label>
                <label>用户 / 调用规模 <input class="form-control" id="p-audience" placeholder="注明单位、时间和出处；未核实写待核查"></label>
                <label>为什么是客户（证据及来源）<textarea class="form-control" id="p-evidence" rows="3"></textarea></label>
                <div id="p-score" style="background:#1f2937;padding:14px;border-radius:8px"></div>
                <label>来源链接 <input type="url" class="form-control" id="p-source"></label>
                <label>预计月消费（人民币）<input type="number" min="0" step="0.01" class="form-control" id="p-spend" placeholder="未知留空"></label>
                <label>消费估算依据 <textarea class="form-control" id="p-basis" rows="2"></textarea></label>
                <label>负责人 <input class="form-control" id="p-owner" readonly></label>
                <div><button type="button" onclick="setProspectOwner(currentUser?.display_name || '')">领取给我</button> <button type="button" onclick="setProspectOwner('')">释放回线索池</button></div>
                <label>阶段 <select id="p-status"><option value="pending">待联系</option><option value="contacted">已联系</option><option value="researching">背调中</option><option value="trial">试用中</option><option value="negotiation">洽谈中</option><option value="won">已成交</option><option value="lost">不适合</option></select></label>
                <label>剔除原因（阶段为“不适合”时必填）<textarea class="form-control" id="p-exclusion" rows="2" placeholder="例如：同行、无采购需求、规模不符合"></textarea></label>
                <label>下次跟进 <input type="date" id="p-date" class="form-control"></label>
                <div class="prospect-edit-actions">
                    <p id="p-message" role="status">修改内容后即可保存</p>
                    <div class="prospect-edit-buttons">
                        <button type="button" onclick="copyResearchPrompt()">复制背调指令</button>
                        <button type="button" id="prospect-reset-btn" onclick="resetProspectForm()" disabled>取消修改</button>
                        <button type="submit" id="prospect-save-btn" class="btn-save-details" disabled>保存编辑</button>
                    </div>
                </div>
            </form>
        </div>
    </div>
    <script>
        let currentData = [];
        let visibleData = [];
        let currentUsers = [];
        let currentSourceChannels = [];
        let currentLeadSources = [];
        
        // 筛选状态
        let activeCountry = 'domestic';
        const marketFilters = {};
        const pipelineFilterIds = ['tier-filter', 'type-filter', 'due-filter',
            'industry-filter', 'spend-filter', 'stage-filter', 'exclusion-filter'];

        function marketRows() {
            return currentData.filter(row => row.country_type === activeCountry);
        }

        const MARKET_LABELS = {domestic:'国内', overseas:'海外', pending_verification:'待核验'};
        function marketLabel(country) {
            return MARKET_LABELS[country] || '待核验';
        }
        function marketTagClass(country) {
            return country === 'domestic' ? 'tag-cn' : country === 'overseas' ? 'tag-global' : 'tag-pending';
        }
        function isEmailSendable(row) {
            return row.status === 'pending' && row.country_type !== 'pending_verification';
        }

        function refreshMarketOptions() {
            const rows = marketRows();
            currentSourceChannels = [...new Set(rows.map(r => r.source_channel).filter(Boolean))].sort();
            currentLeadSources = [...new Set(rows.map(r => r.lead_source).filter(Boolean))].sort();
            const industry = document.getElementById('industry-filter');
            const previous = industry.value;
            industry.replaceChildren(new Option('全部行业', 'all'));
            [...new Set(rows.map(r => r.industry).filter(Boolean))].sort()
                .forEach(value => industry.add(new Option(value, value)));
            industry.value = [...industry.options].some(o => o.value === previous) ? previous : 'all';
            renderSourceCapsules();
            renderLeadSourceCapsules();
        }

        function switchPipelineMarket(country, elem) {
            if (!['domestic', 'overseas', 'pending_verification'].includes(country)) return;
            marketFilters[activeCountry] = {
                values:Object.fromEntries(pipelineFilterIds.map(id => [id, document.getElementById(id).value])),
                sources:[...selectedSources], leadSources:[...selectedLeadSources],
                sourceMode:activeSourceMode, touch:activeTouch
            };
            activeCountry = country;
            document.getElementById('country-filter').value = country;
            document.getElementById('pipeline-title').textContent =
                {domestic:'国内客户', overseas:'海外客户', pending_verification:'待核验线索'}[country];
            refreshMarketOptions();
            const saved = marketFilters[country];
            resetFilters();
            if (saved) {
                for (const [id, value] of Object.entries(saved.values)) {
                    const select = document.getElementById(id);
                    if ([...select.options].some(option => option.value === value)) select.value = value;
                }
                selectedSources = new Set(saved.sources);
                selectedLeadSources = new Set(saved.leadSources);
                activeSourceMode = saved.sourceMode;
                activeTouch = saved.touch;
                activeTier = document.getElementById('tier-filter').value;
                activeStage = document.getElementById('stage-filter').value;
                renderSourceCapsules();
                renderLeadSourceCapsules();
                renderTable();
            }
            switchNav('pipeline', elem);
            fetchStats();
        }
        let selectedSources = new Set(); // 多选公司来源渠道集合
        let activeSourceMode = 'include'; // 'include' (仅看所选) 或 'exclude' (排除所选/反选)
        let selectedLeadSources = new Set(); // 多选线索来源集合
        let activeTier = 'all';
        let activeTouch = 'all';
        let activeStage = 'all';

        // 当前登录用户由服务端签名会话决定，浏览器不能自行伪造角色。
        let currentUser = null;

        const STATUS_TEXT_MAP = {
            'pending': '⏳ 待触达',
            'sample_sent': '📤 已推送未回复',
            'contacted': '🔗 已建连',
            'negotiation': '💡 有意向',
            'team_trial': '📝 已注册',
            'won': '💰 已充值',
            'lost': '⛔ 不触达'
        };

        const ROLE_NAME_MAP = {
            'admin': '👑 超级管理员',
            'sales_lead': '💼 销售总监',
            'sales_rep': '📞 销售专员',
            'observer': '👀 运营观察员'
        };

        const TIER_NAME_MAP = {
            'S': 'S级 战略客户',
            'A': 'A级 核心腰部',
            'B': 'B级 高意向',
            'C': 'C级 潜力探索'
        };
        const TIER_RULES = {
            S: '战略级 / 特大客户：年流水过亿或 200 人以上；头部品牌、头部 DTC 或巨头代理；预计月生图 50,000+，适合私有化、团队空间大包或高并发 API。',
            A: '核心腰部大客户：约 50–200 人；月商拍约 500–2,000 张；主力品牌或 Shopify Plus 热卖品牌；年营收约 5,000 万–2 亿，适合 10–30 座席及月度算力充值。',
            B: '成长型高意向客户：约 20–50 人；中腰部活跃卖家或创意团队；月摄影外包约 2–5 万，有稳定生图刚需，适合 5–10 座席团队版。'
        };

        function openTierRule(tier) {
            const rule = TIER_RULES[tier];
            if (!rule) return;
            document.getElementById('modal-title').textContent = `${TIER_NAME_MAP[tier]} · 定义规则`;
            const body = document.getElementById('modal-body');
            body.replaceChildren();
            const content = document.createElement('p');
            content.style.cssText = 'white-space:pre-wrap;line-height:1.8;';
            content.textContent = rule;
            body.appendChild(content);
            document.getElementById('modal').style.display = 'flex';
        }

        const EN_ZH_DICTIONARY = {
            // 行业类目
            'Clean Skincare DTC': '纯净护肤 DTC',
            'Everyday Carry / DTC': 'EDC随身装备 / DTC',
            'Apparel & Fashion DTC': '服装与时尚 DTC',
            'Personal Care DTC': '个护美妆 DTC',
            'DTC Performance Agency': 'DTC效果广告代理商',
            'Growth Marketing Agency': '增长营销服务商',
            'E-commerce DTC': '跨境电商 DTC',
            'Consumer Tech': '消费电子 / 科技',
            'Home & Living': '家居生活',
        };

        function getEnZhHint(text) {
            if (!text) return '';
            const trimmed = text.trim();
            return EN_ZH_DICTIONARY[trimmed] || '';
        }

        function updateCurrentUserUI() {
            if (!currentUser) {
                document.getElementById('current-user-name').innerText = '未登录';
                document.getElementById('current-user-avatar').innerText = '访';
                document.getElementById('current-user-role').innerText = '● 请先登录';
                document.getElementById('permissions-nav').style.display = 'none';
                document.getElementById('tab-register-btn').style.display = 'none';
                return;
            }
            document.getElementById('current-user-name').innerText = currentUser.display_name || currentUser.username;
            document.getElementById('current-user-avatar').innerText = (currentUser.display_name || currentUser.username)[0].toUpperCase();
            document.getElementById('current-user-role').innerText = `● ${ROLE_NAME_MAP[currentUser.role] || currentUser.role}`;
            document.getElementById('permissions-nav').style.display = currentUser.role === 'admin' ? '' : 'none';
            document.getElementById('tab-register-btn').style.display = currentUser.role === 'admin' ? '' : 'none';
            document.getElementById('feishu-mail-import-panel').hidden = currentUser.role !== 'admin';
        }

        // 导航切换
        function switchNav(navId, elem) {
            document.querySelectorAll('.nav-list .nav-item').forEach(el => el.classList.remove('active'));
            elem.classList.add('active');
            
            document.querySelectorAll('.view-section').forEach(el => el.classList.remove('active'));
            document.getElementById('view-' + navId).classList.add('active');

            if (navId === 'permissions') {
                fetchUsers();
            } else if (navId === 'email-pool' || navId === 'email-settings') {
                fetchEmailProspects();
                if (navId === 'email-pool' && currentUser?.role === 'admin') loadFeishuMailImportStatus();
            } else if (navId === 'funnel') {
                renderFunnelView();
            } else if (navId === 'strategy') {
                fetchStrategies();
            } else if (navId === 'discord') {
                fetchDiscordCommunities();
            }
        }

        let currentEmailProspects = [];
        let mailSenders = [];
        let emailProviderConfigured = false;
        let mailAutomation = {};
        let packageSelectedIds = new Set();
        async function fetchEmailProspects() {
            const res=await fetch('/api/email-recipients');
            const result=await res.json();
            if(!res.ok) return;
            currentEmailProspects=result.rows||[];
            mailSenders=result.senders||[];
            mailAutomation=result.automation||{};
            mailPackages=result.packages||[];
            for(const id of ['email-sender','mail-edit-sender']) {
                const input=document.getElementById(id), previous=input.value;
                input.innerHTML=mailSenders.map(s=>`<option value="${s.id}">${escapeHtml(s.from_email||'默认邮箱（未配置）')}</option>`).join('');
                if(id==='mail-edit-sender' && currentUser?.role==='admin') input.innerHTML+='<option value="new">新增发件邮箱</option>';
                if([...input.options].some(o=>o.value===previous)) input.value=previous;
            }
            loadMailSender();
            loadMailAutomation();
            updateMailSenderStatus();
            renderEmailRecipients();
            renderEmailPackages();
            document.getElementById('email-history').innerHTML=(result.history||[]).map(row=>`<div class="email-preview">${escapeHtml(row.created_at)} UTC · ${escapeHtml(row.sender)} → ${escapeHtml(row.email)}<br>${escapeHtml(row.company_name)} · ${escapeHtml(row.subject)} · 操作人：${escapeHtml(row.sent_by)}</div>`).join('')||'暂无发送记录';
        }
        let mailPackages = [];
        function renderEmailPackages() {
            const target=document.getElementById('email-package-list');
            if(!target) return;
            target.innerHTML=mailPackages.length ? mailPackages.map(item=>
                `<div class="email-preview"><strong>${escapeHtml(item.title)}</strong> · ${item.recipient_count} 人 · ${item.status==='sent'?'已发送':'草稿'} · ${escapeHtml(item.created_at)}<br>${escapeHtml(item.subject)}<br>${item.status==='draft'?`<button onclick="previewEmailPackage(${item.id})">预览此包</button> <button onclick="sendEmailPackage(${item.id})">发送此包</button>`:''}</div>`
            ).join('') : '暂无推送包草稿';
            renderPackageCandidates();
        }
        function renderPackageCandidates() {
            const body=document.getElementById('package-candidate-body');
            if(!body) return;
            const from=document.getElementById('package-date-from').value;
            const to=document.getElementById('package-date-to').value;
            const rows=currentEmailProspects.filter(row=>isEmailSendable(row)&&
                (!from||String(row.lead_created_at||'').slice(0,10)>=from)&&
                (!to||String(row.lead_created_at||'').slice(0,10)<=to));
            const eligible=new Set(rows.map(row=>row.lead_id));
            packageSelectedIds=new Set([...packageSelectedIds].filter(id=>eligible.has(id)));
            document.getElementById('package-candidate-count').textContent=`筛选到 ${rows.length} 位未发送线索，已选择 ${packageSelectedIds.size} 位`;
            body.innerHTML=rows.map(row=>`<tr><td><input type="checkbox" ${packageSelectedIds.has(row.lead_id)?'checked':''} onchange="togglePackageLead(${row.lead_id},this.checked)"></td><td>${escapeHtml(String(row.lead_created_at||'').slice(0,10)||'-')}</td><td>${escapeHtml(row.contact_name||'-')}</td><td>${escapeHtml(row.company_name||'-')}</td><td>${escapeHtml(row.email)}</td><td>${emailPushBadge(row.status)}</td></tr>`).join('')||'<tr><td colspan="6">当前日期范围内没有可加入推送包的线索</td></tr>';
        }
        function togglePackageLead(id, checked) {
            if(checked) packageSelectedIds.add(id); else packageSelectedIds.delete(id);
            renderPackageCandidates();
        }
        function togglePackageCandidates(checked) {
            const from=document.getElementById('package-date-from').value, to=document.getElementById('package-date-to').value;
            currentEmailProspects.filter(row=>isEmailSendable(row)&&(!from||String(row.lead_created_at||'').slice(0,10)>=from)&&(!to||String(row.lead_created_at||'').slice(0,10)<=to)).forEach(row=>checked?packageSelectedIds.add(row.lead_id):packageSelectedIds.delete(row.lead_id));
            renderPackageCandidates();
        }
        async function createEmailPackage() {
            const ids=[...packageSelectedIds], output=document.getElementById('email-package-result');
            const title=document.getElementById('email-package-title').value.trim();
            if(!ids.length) {output.textContent='请先在下方线索列表中选择收件人';return;}
            if(!title) {output.textContent='请填写推送包名称';return;}
            const sender=mailSenders.find(s=>String(s.id)===document.getElementById('email-sender').value);
            const button=document.getElementById('email-package-create');
            button.disabled=true; output.textContent='正在保存草稿包…';
            try {
                const res=await fetch('/api/email-packages',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
                    title,lead_ids:ids,sender_id:Number(sender?.id||1),
                    date_from:document.getElementById('package-date-from').value,
                    date_to:document.getElementById('package-date-to').value,
                    subject:document.getElementById('email-subject').value,
                    body:document.getElementById('email-body').value
                })});
                const result=await res.json();
                if(!res.ok||!result.success) throw Error(result.error||'创建失败');
                mailPackages=[result.package,...mailPackages.filter(item=>item.id!==result.package.id)];
                packageSelectedIds.clear();
                renderEmailPackages(); output.textContent=`已保存“${result.package.title}”，${result.package.recipient_count} 位联系人；未发送邮件。`;
            } catch(error) { output.textContent=error.message||'创建推送包失败'; }
            finally { button.disabled=false; }
        }
        async function previewEmailPackage(id) {
            const item=mailPackages.find(value=>value.id===id), target=document.getElementById('email-package-preview');
            if(!item) return;
            target.innerHTML=item.recipients.map(row=>`<div class="email-preview"><strong>To: ${escapeHtml(row.email)}</strong><br><strong>Subject: ${escapeHtml(item.subject.replaceAll('{{company}}',row.company_name||'your team').replaceAll('{{name}}',row.contact_name||'there'))}</strong><br><br>${escapeHtml(item.body.replaceAll('{{company}}',row.company_name||'your team').replaceAll('{{name}}',row.contact_name||'there'))}</div>`).join('');
        }
        async function sendEmailPackage(id) {
            const item=mailPackages.find(value=>value.id===id);
            if(!item||item.status!=='draft') return;
            await previewEmailPackage(id);
            if(!confirm(`确认发送推送包“${item.title}”内的 ${item.recipient_count} 封邮件？`)) return;
            const res=await fetch('/api/email-packages/send',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id,confirmation:`SEND PACKAGE ${id}`})});
            const result=await res.json();
            document.getElementById('email-package-result').textContent=res.ok?`推送包发送完成：${result.sent} 封。`:result.error||'发送失败';
            await fetchEmailProspects();
        }
        function updateMailSenderStatus() {
            const settings=mailSenders.find(s=>String(s.id)===document.getElementById('email-sender').value)||{};
            emailProviderConfigured=!!settings.configured;
            const provider=document.getElementById('email-provider-status');
            provider.textContent=settings.configured ? `参数齐全 · ${settings.provider==='smtp'?'自有 SMTP':'Resend'} · 本批发件人：${settings.from_email}` : '所选发件邮箱未配置完整，可以预览，不能发送';
            provider.className=settings.configured ? 'email-status email-configured' : 'email-status email-unconfigured';
            document.getElementById('email-send-btn').disabled=!settings.configured;
            document.getElementById('email-preview-list').textContent='';
        }
        function loadMailSender() {
            const settings=mailSenders.find(s=>String(s.id)===document.getElementById('mail-edit-sender').value)||{
                from_name:'Aibubu',batch_limit:20,encryption_ready:mailSenders[0]?.encryption_ready
            };
            for(const field of ['sending-domain','from-email','from-name','reply-to','batch-limit','smtp-host','smtp-port','smtp-security','smtp-username']) {
                const input=document.getElementById('mail-'+field);
                input.value=settings[field.replaceAll('-','_')]??'';
                input.disabled=currentUser?.role!=='admin';
            }
            document.getElementById('mail-provider').value=settings.provider||'resend';
            document.getElementById('mail-smtp-password').value='';
            document.getElementById('mail-save-settings').hidden=currentUser?.role!=='admin';
            document.getElementById('mail-api-key').disabled=currentUser?.role!=='admin'||!settings.encryption_ready;
            document.getElementById('mail-api-key').value='';
            document.getElementById('email-config-result').textContent=
                `${settings.provider==='smtp'?'SMTP密码':'API Key'}：${settings.key_configured?'已配置（不回显）':'未配置'}；页面加密存储：${settings.encryption_ready?'可用':'未启用，请先设置 MAIL_CONFIG_KEY'}`;
            toggleMailProvider();
        }
        function toggleMailProvider() {
            const smtp=document.getElementById('mail-provider').value==='smtp';
            for(const id of ['mail-smtp-host','mail-smtp-port','mail-smtp-security','mail-smtp-username','mail-smtp-password'])
                document.getElementById(id).disabled=!smtp||currentUser?.role!=='admin';
            document.getElementById('mail-api-key').disabled=smtp||currentUser?.role!=='admin';
        }
        function loadMailAutomation() {
            const a=mailAutomation||{};
            document.getElementById('mail-auto-enabled').checked=!!a.enabled;
            document.getElementById('mail-auto-limit').value=a.daily_limit??20;
            document.getElementById('mail-auto-hour').value=a.send_hour??9;
            document.getElementById('mail-auto-subject').value=a.subject_template||'{{company}} × Aibubu：更高效的 AI 图片与视频工作流';
            document.getElementById('mail-auto-body').value=a.body_template||'Hi {{name}},\\n\\nI’m reaching out from Aibubu.\\n\\nBest,\\nAibubu Team';
            const select=document.getElementById('mail-auto-sender');
            select.innerHTML=mailSenders.map(s=>`<option value="${s.id}">${escapeHtml(s.from_email||'未配置')}</option>`).join('');
            if(a.sender_id) select.value=String(a.sender_id);
        }
        function renderEmailRecipients() {
            const market=document.getElementById('email-market-filter').value;
            const status=document.getElementById('email-status-filter').value;
            const query=document.getElementById('email-search').value.trim().toLowerCase();
            const rows=currentEmailProspects.filter(row=>(!market||row.country_type===market)&&(!status||row.status===status)&&
                (!query||[row.email,row.company_name,row.contact_name].some(value=>String(value||'').toLowerCase().includes(query))));
            document.getElementById('email-select-all').checked=false;
            document.getElementById('email-preview-list').textContent='';
            document.getElementById('email-recipient-count').textContent=`当前筛选 ${rows.length} 位邮箱联系人；重复邮箱发送时会拦截。`;
            document.getElementById('email-prospect-body').innerHTML=rows.map(row=>`<tr>
                <td><input class="email-row-check" type="checkbox" value="${row.lead_id}" ${!isEmailSendable(row)?'disabled':''} onchange="updateEmailSelectionCount()"></td>
                <td>${escapeHtml(row.email)}</td><td>${escapeHtml(row.contact_name||'-')}</td>
                <td>${escapeHtml(row.company_name||'-')} / ${marketLabel(row.country_type)}</td><td>${row.country_type==='pending_verification'?'<span class="email-push-badge email-push-warning">市场待核验，不可发送</span>':emailPushBadge(row.status)}</td>
                <td>${escapeHtml(row.last_sent_at||'-')}</td><td>${escapeHtml(row.last_sender||'-')}</td>
                <td>${row.queue_id?`<button onclick="setEmailProspectStatus(${row.queue_id}, 'unsubscribed')" ${row.status==='sending'?'disabled':''}>标记退订</button>`:'-'}</td>
            </tr>`).join('') || '<tr><td colspan="8">当前筛选下没有邮箱联系人，无需在客户池操作</td></tr>';
            updateEmailSelectionCount();
        }
        function emailPushBadge(status) {
            const labels={pending:'未推送',sending:'发送中 / 待核实',sent:'✓ 已推送',unsubscribed:'已退订',removed:'已移除',email_changed:'邮箱变更待核实'};
            const style=status==='sent'?'email-push-sent':status==='pending'?'email-push-pending':
                status==='sending'||status==='email_changed'?'email-push-warning':'email-push-blocked';
            return `<span class="email-push-badge ${style}">${labels[status]||escapeHtml(status)}</span>`;
        }
        function selectedEmailIds() {
            return [...document.querySelectorAll('.email-row-check:checked')].map(input=>Number(input.value));
        }
        function updateEmailSelectionCount() {
            document.getElementById('email-selection-count').textContent=`已选择 ${selectedEmailIds().length} 人`;
        }
        function toggleAllEmailProspects(checked) {
            document.querySelectorAll('.email-row-check:not(:disabled)').forEach(input=>input.checked=checked);
            updateEmailSelectionCount();
        }
        async function setEmailProspectStatus(id, status) {
            const res=await fetch('/api/email-queue/status',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id,status})});
            const result=await res.json();
            if(!res.ok || !result.success) {alert(result.error||'状态更新失败');return;}
            await fetchEmailProspects();
        }
        async function previewSelectedEmails() {
            const ids=selectedEmailIds(), output=document.getElementById('email-preview-list');
            if(!ids.length) {output.innerHTML='<div class="email-status">请先选择收件人</div>';return;}
            const payload={lead_ids:ids,sender_id:Number(document.getElementById('email-sender').value),subject:document.getElementById('email-subject').value,body:document.getElementById('email-body').value};
            const res=await fetch('/api/email-queue/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
            const result=await res.json();
            if(!res.ok || !result.success) {output.textContent=result.error||'预览失败';return false;}
            output.innerHTML=result.previews.map(item=>`<div class="email-preview"><strong>From: ${escapeHtml(result.sender||'未配置')}</strong>\n<strong>To: ${escapeHtml(item.email)}</strong>\n<strong>Subject: ${escapeHtml(item.subject)}</strong>\n\n${escapeHtml(item.body)}</div>`).join('');
            return true;
        }
        async function sendSelectedEmails() {
            const ids=selectedEmailIds();
            if(!ids.length) {document.getElementById('email-send-result').textContent='请先选择收件人';return;}
            if(ids.length>50) {document.getElementById('email-send-result').textContent='每批最多发送 50 人';return;}
            if(!await previewSelectedEmails()) return;
            const sender=mailSenders.find(s=>String(s.id)===document.getElementById('email-sender').value);
            if(!sender?.configured) {document.getElementById('email-send-result').textContent='请先配置所选发件邮箱';return;}
            if(!confirm(`确认使用 ${sender.from_email} 向 ${ids.length} 位收件人逐封发送邮件？请确认已核实为公开商务邮箱，且适合本次触达。已发送或退订邮箱不会重复发送。`)) return;
            const output=document.getElementById('email-send-result');
            output.textContent='正在发送，请勿重复点击…';
            document.getElementById('email-send-btn').disabled=true;
            try {
            const res=await fetch('/api/email-queue/send',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
                lead_ids:ids,sender_id:sender.id,subject:document.getElementById('email-subject').value,body:document.getElementById('email-body').value,
                confirmation:`SEND ${ids.length}`
            })});
            const result=await res.json();
            output.textContent=res.ok ? `已提交发送：${result.sent}，已保存独立发送记录，未修改客户池` : (result.error||'发送失败');
            } catch(error) {output.textContent='网络中断，请刷新队列并核对发送状态，勿重复提交';}
            finally {
            await fetchEmailProspects();
            }
        }

        let feishuMailImportPreview = null;
        async function loadFeishuMailImportStatus() {
            const status = document.getElementById('feishu-mail-import-status');
            try {
                const response = await fetch('/api/feishu-mail-import/status', {cache:'no-store'});
                const result = await response.json();
                status.textContent = result.configured
                    ? '飞书连接已配置'
                    : '尚未配置飞书 App ID/Secret；配置后即可读取邮件表。';
            } catch (_) {
                status.textContent = '无法检查飞书连接状态';
            }
        }
        async function previewFeishuMailImport() {
            const status = document.getElementById('feishu-mail-import-status');
            const summary = document.getElementById('feishu-mail-import-summary');
            const conflicts = document.getElementById('feishu-mail-import-conflicts');
            const applyButton = document.getElementById('feishu-mail-import-apply');
            applyButton.hidden = true;
            applyButton.disabled = true;
            conflicts.innerHTML = '';
            summary.textContent = '正在读取飞书邮件表并核对公司名、官网域名和邮箱…';
            try {
                const response = await fetch('/api/feishu-mail-import/preview', {method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
                const result = await response.json();
                if (!response.ok || !result.success) throw Error(result.error || '读取预览失败');
                feishuMailImportPreview = result;
                summary.textContent = `表内 ${result.total_rows} 行；可导入联系人 ${result.eligible_contacts}；新建公司 ${result.new_companies}；匹配已有公司 ${result.matched_companies}；需跳过/人工核对 ${result.conflict_count}。已有公司只补 CRM 空字段，不覆盖已有信息。`;
                if (result.conflicts?.length) {
                    conflicts.innerHTML = `<details><summary>查看冲突明细（最多显示100条）</summary><div class="table-container"><table class="email-table"><thead><tr><th>飞书行</th><th>公司</th><th>邮箱</th><th>处理</th></tr></thead><tbody>${result.conflicts.map(row=>`<tr><td>${Number(row.row)||'-'}</td><td>${escapeHtml(row.company||'-')}</td><td>${escapeHtml(row.email||'-')}</td><td>${escapeHtml(row.reason||'需核对')}</td></tr>`).join('')}</tbody></table></div></details>`;
                }
                applyButton.hidden = false;
                applyButton.disabled = result.eligible_contacts === 0;
                status.textContent = result.eligible_contacts ? '预览完成，尚未写入 CRM。' : '没有可导入联系人。';
            } catch (error) {
                feishuMailImportPreview = null;
                summary.textContent = '';
                status.textContent = error.message;
            }
        }
        async function applyFeishuMailImport() {
            if (!feishuMailImportPreview?.eligible_contacts) return;
            const preview = feishuMailImportPreview;
            if (!confirm(`确认导入 ${preview.eligible_contacts} 位联系人、新建 ${preview.new_companies} 家公司？已有公司只补空字段，冲突项跳过；此操作不会发送邮件。`)) return;
            const button = document.getElementById('feishu-mail-import-apply');
            button.disabled = true;
            document.getElementById('feishu-mail-import-status').textContent = '正在导入…';
            try {
                const response = await fetch('/api/feishu-mail-import/apply', {
                    method:'POST', headers:{'Content-Type':'application/json'},
                    body:JSON.stringify({
                        confirmation:'IMPORT FEISHU MAIL SHEET',
                        preview_fingerprint:preview.fingerprint
                    })
                });
                const result = await response.json();
                if (!response.ok || !result.success) throw Error(result.error || '导入失败');
                document.getElementById('feishu-mail-import-summary').textContent =
                    `导入完成：新建公司 ${result.created_companies} 家；补充公司信息 ${result.updated_companies} 家；新建联系人 ${result.created_contacts} 位；冲突/重复跳过 ${result.conflicts} 条；沿用“已推送/退订”状态并防止重复触达 ${result.suppressed_existing_statuses} 位。`;
                document.getElementById('feishu-mail-import-status').textContent = '导入已完成，不会自动发送邮件。';
                document.getElementById('feishu-mail-import-conflicts').innerHTML = '';
                feishuMailImportPreview = null;
                button.hidden = true;
                await fetchPipeline();
                await fetchEmailProspects();
            } catch (error) {
                document.getElementById('feishu-mail-import-status').textContent = error.message;
                button.disabled = false;
            }
        }

        async function saveMailSettings() {
            const data={};
            for(const field of ['provider','sending-domain','from-email','from-name','reply-to','batch-limit','api-key','smtp-host','smtp-port','smtp-security','smtp-username','smtp-password'])
                data[field.replaceAll('-','_')]=document.getElementById('mail-'+field).value;
            const editId=document.getElementById('mail-edit-sender').value;
            if(editId!=='new') data.id=Number(editId);
            try {
                const res=await fetch('/api/email-senders',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
                document.getElementById('mail-api-key').value='';
                const result=await res.json();
                if(!res.ok) throw Error(result.error||'保存失败');
                await fetchEmailProspects();
                document.getElementById('mail-edit-sender').value=String(result.settings.id);
                loadMailSender();
                alert('配置已保存，没有发送任何邮件');
            } catch(error) {document.getElementById('email-config-result').textContent=error.message;}
        }
        async function saveMailAutomation() {
            const data={
                enabled:document.getElementById('mail-auto-enabled').checked,
                sender_id:Number(document.getElementById('mail-auto-sender').value||1),
                daily_limit:Number(document.getElementById('mail-auto-limit').value),
                send_hour:Number(document.getElementById('mail-auto-hour').value),
                send_minute:0, timezone:'Asia/Shanghai',
                subject_template:document.getElementById('mail-auto-subject').value,
                body_template:document.getElementById('mail-auto-body').value
            };
            const res=await fetch('/api/email-automation',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
            const result=await res.json();
            document.getElementById('mail-automation-result').textContent=res.ok?'自动发送规则已保存，不会立即发送':(result.error||'保存失败');
        }

        let currentDiscordCommunities = [];
        async function fetchDiscordCommunities() {
            const params = new URLSearchParams();
            const language = document.getElementById('discord-language-filter').value;
            const fit = document.getElementById('discord-fit-filter').value;
            const status = document.getElementById('discord-status-filter').value;
            if (language) params.set('language', language);
            if (fit) params.set('fit', fit);
            if (status) params.set('status', status);
            const res = await fetch('/api/discord-communities?' + params.toString());
            const rows = await res.json();
            currentDiscordCommunities = Array.isArray(rows) ? rows : [];
            document.getElementById('discord-total').textContent = currentDiscordCommunities.length;
            document.getElementById('discord-joined').textContent =
                currentDiscordCommunities.filter(row => row.join_status === '已加入').length;
            document.getElementById('discord-high-fit').textContent =
                currentDiscordCommunities.filter(row => row.fit_level === '高').length;
            document.getElementById('discord-chinese').textContent =
                currentDiscordCommunities.filter(row => row.language !== '英文').length;
            document.getElementById('discord-community-grid').innerHTML = currentDiscordCommunities.length
                ? currentDiscordCommunities.map(row => `<article class="community-card">
                    <div class="community-card-head">
                        <div><h3><a href="${escapeHtml(row.invite_url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(row.name)}</a></h3>
                        <p>${escapeHtml(row.focus)}</p></div>
                        <span class="community-priority">#${row.priority}</span>
                    </div>
                    <div class="community-meta">
                        <span class="community-tag ${row.fit_level === '高' ? 'community-fit-high' : ''}">${escapeHtml(row.fit_level)}匹配</span>
                        <span class="community-tag">${escapeHtml(row.language)}</span>
                        <span class="community-tag">${escapeHtml(row.join_status)}</span>
                        <span class="community-tag">${escapeHtml(row.account_type)}</span>
                    </div>
                    <p><strong>用户画像：</strong>${escapeHtml(row.audience || '待补充')}</p>
                    <p><strong>推荐频道：</strong>${escapeHtml(row.recommended_channels || '进群后查看规则与频道')}</p>
                    <p><strong>观察重点：</strong>${escapeHtml(row.notes || '待补充')}</p>
                    <div style="display:flex;justify-content:space-between;align-items:center;margin-top:auto">
                        <small style="color:var(--text-muted)">同步：${escapeHtml(row.updated_at)}</small>
                        <button class="btn-pitch" onclick="editDiscordCommunity(${Number(row.id)})">更新属性</button>
                    </div>
                </article>`).join('')
                : '<div class="perm-card">当前筛选范围内暂无社群</div>';
        }

        function editDiscordCommunity(id) {
            const row = currentDiscordCommunities.find(item => Number(item.id) === Number(id));
            if (!row) return;
            const fields = {
                'discord-name':row.name, 'discord-url':row.invite_url, 'discord-focus':row.focus,
                'discord-language':row.language, 'discord-fit':row.fit_level,
                'discord-status':row.join_status, 'discord-account':row.account_type,
                'discord-priority':row.priority, 'discord-audience':row.audience,
                'discord-channels':row.recommended_channels, 'discord-notes':row.notes
            };
            Object.entries(fields).forEach(([id, value]) => document.getElementById(id).value = value || '');
            document.getElementById('discord-name').readOnly = true;
            document.getElementById('discord-form-card').style.display = 'block';
            document.getElementById('discord-form-card').scrollIntoView({behavior:'smooth'});
        }

        async function saveDiscordCommunity(event) {
            event.preventDefault();
            const message = document.getElementById('discord-message');
            message.textContent = '正在保存…';
            const payload = {
                name:document.getElementById('discord-name').value,
                invite_url:document.getElementById('discord-url').value,
                focus:document.getElementById('discord-focus').value,
                language:document.getElementById('discord-language').value,
                fit_level:document.getElementById('discord-fit').value,
                join_status:document.getElementById('discord-status').value,
                account_type:document.getElementById('discord-account').value,
                priority:Number(document.getElementById('discord-priority').value || 99),
                audience:document.getElementById('discord-audience').value,
                recommended_channels:document.getElementById('discord-channels').value,
                notes:document.getElementById('discord-notes').value
            };
            const res = await fetch('/api/discord-communities', {
                method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)
            });
            const result = await res.json();
            if (!res.ok || !result.success) {
                message.textContent = result.error || '保存失败';
                return;
            }
            message.textContent = `已同步 ${result.community.name}`;
            document.getElementById('discord-form').reset();
            document.getElementById('discord-name').readOnly = false;
            await fetchDiscordCommunities();
        }

        function resetDiscordFilters() {
            document.getElementById('discord-language-filter').value = '';
            document.getElementById('discord-fit-filter').value = '';
            document.getElementById('discord-status-filter').value = '';
            fetchDiscordCommunities();
        }

        let currentStrategies = [];
        async function fetchStrategies() {
            const params = new URLSearchParams();
            const version = document.getElementById('strategy-version-filter').value;
            const start = document.getElementById('strategy-start-date').value;
            const end = document.getElementById('strategy-end-date').value;
            if (version) params.set('version', version);
            if (start) params.set('start', start);
            if (end) params.set('end', end);
            const res = await fetch('/api/strategies?' + params.toString());
            const rows = await res.json();
            currentStrategies = rows;
            const versions = [...new Set(rows.map(row => row.version))].sort((a, b) => b - a);
            const select = document.getElementById('strategy-version-filter');
            const selected = select.value;
            if (!version) {
                select.innerHTML = '<option value="">全部版本</option>' +
                    versions.map(value => `<option value="${value}">v${value}</option>`).join('');
                select.value = selected;
            }
            document.getElementById('strategy-table-body').innerHTML = rows.length ? rows.map(row => `<tr>
                <td>${escapeHtml(row.title)}</td>
                <td><span class="status-badge">v${row.version}</span></td>
                <td>${escapeHtml(row.updated_at)}</td>
                <td>${escapeHtml(row.created_by)}</td>
                <td><button class="btn-pitch" onclick="openStrategyDetail(${Number(row.id)})">查看文本</button></td>
            </tr>`).join('') : '<tr><td colspan="5">当前筛选范围内暂无策略存档</td></tr>';
        }

        function openStrategyDetail(strategyId) {
            const row = currentStrategies.find(item => Number(item.id) === Number(strategyId));
            if (!row) return;
            document.getElementById('strategy-detail-title').textContent = `${row.title} · v${row.version}`;
            document.getElementById('strategy-detail-meta').textContent =
                `更新时间：${row.updated_at} · 保存人：${row.created_by}`;
            document.getElementById('strategy-detail-content').textContent = row.content;
            document.getElementById('strategy-detail-modal').style.display = 'flex';
        }

        function closeStrategyDetail() {
            document.getElementById('strategy-detail-modal').style.display = 'none';
        }

        async function saveStrategy(event) {
            event.preventDefault();
            const message = document.getElementById('strategy-message');
            message.textContent = '正在保存…';
            const res = await fetch('/api/strategies', {
                method:'POST', headers:{'Content-Type':'application/json'},
                body:JSON.stringify({
                    title:document.getElementById('strategy-title').value,
                    content:document.getElementById('strategy-content').value
                })
            });
            const result = await res.json();
            if (!res.ok || !result.success) {
                message.textContent = result.error || '保存失败';
                return;
            }
            message.textContent = `已保存 ${result.strategy.title} v${result.strategy.version}`;
            await fetchStrategies();
        }

        function resetStrategyFilters() {
            document.getElementById('strategy-version-filter').value = '';
            document.getElementById('strategy-start-date').value = '';
            document.getElementById('strategy-end-date').value = '';
            fetchStrategies();
        }

        async function fetchPipeline() {
            const res = await fetch('/api/pipeline');
            currentData = await res.json();
            refreshMarketOptions();
            renderTable();
            renderFunnelView();
            await fetchStats();
        }

        async function fetchStats() {
            const country = activeCountry;
            const res = await fetch('/api/stats?country=' + country);
            const data = await res.json();
            if (country !== activeCountry) return;
            document.getElementById('stat-companies').innerText = data.total_companies;
            const byTier = data.companies_by_tier || {};
            document.getElementById('stat-tier-s').innerText = byTier['S'] || 0;
            document.getElementById('stat-tier-a').innerText = byTier['A'] || 0;
            document.getElementById('stat-tier-b').innerText = byTier['B'] || 0;
            const freshness = document.getElementById('data-freshness');
            const updatedAt = data.data_last_updated;
            if (updatedAt) {
                const normalized = updatedAt.includes('T') ? updatedAt : updatedAt.replace(' ', 'T') + 'Z';
                const formatted = new Intl.DateTimeFormat('zh-CN', {
                    timeZone:'Asia/Shanghai', month:'2-digit', day:'2-digit',
                    hour:'2-digit', minute:'2-digit', hour12:false
                }).format(new Date(normalized));
                freshness.textContent = `数据状态：最新 · 最近更新 ${formatted}`;
            } else {
                freshness.textContent = '数据状态：最新 · 暂无客户数据';
            }
            const dailyRes = await fetch('/api/funnel/daily?days=5&country=' + country);
            const daily = await dailyRes.json();
            if (country !== activeCountry) return;
            renderRecentOutreachTable(daily.rows || []);
        }

        function openOutreachDetails() {
            document.getElementById('funnel-country').value = activeCountry;
            document.getElementById('funnel-tier').value = 'all';
            switchNav('funnel', document.getElementById('funnel-nav'));
        }

        function renderRecentOutreachTable(rows) {
            const labels = [['sample_sent', '已推送'], ['contacted', '已建连'], ['negotiation', '有意向'], ['team_trial', '已注册'], ['won', '已充值'], ['lost', '不触达'], ['total', '触达记录']];
            const days = rows.slice(0, 5).sort((a, b) => b.date.localeCompare(a.date));
            document.getElementById('yesterday-outreach-stats').innerHTML =
                `<div class="recent-outreach-header"><h3>最近5天触达数据</h3><button onclick="openOutreachDetails()">查看更多</button></div>` +
                `<p>含今天，日期为北京时间；按每日新增触达记录统计。</p><div class="recent-outreach-scroll"><table class="recent-outreach-table"><thead><tr><th scope="col">日期</th>` +
                labels.map(([, label]) => `<th scope="col">${label}</th>`).join('') +
                `</tr></thead><tbody>` +
                days.map(row => `<tr><th scope="row">${escapeHtml(row.date)}</th>` +
                    labels.map(([key]) => `<td>${key === 'total' ? labels.slice(0, 6).reduce((sum, [status]) => sum + (row[status] || 0), 0) : (row[key] || 0)}</td>`).join('') +
                    `</tr>`).join('') + `</tbody></table></div>`;
        }

        async function fetchSources() {
            refreshMarketOptions();
        }

        async function fetchLeadSources() {
            refreshMarketOptions();
        }

        function renderSourceCapsules() {
            const select = document.getElementById('source-filter');
            if (!select) return;
            const previous = [...selectedSources][0] || 'all';
            select.replaceChildren(new Option('全部来源', 'all'));
            currentSourceChannels.forEach(src => select.add(new Option(src, src)));
            select.value = previous;
            if (select.selectedIndex < 0) {select.value='all';selectedSources.clear();}
        }

        function renderLeadSourceCapsules() {
            const container = document.getElementById('lead-source-capsules-container');
            if (!container) return;
            let html = `<span class="capsule-lead-btn ${selectedLeadSources.size === 0 ? 'active' : ''}" onclick="clearLeadSourceFilter()">全部线索渠道 (${currentLeadSources.length}类)</span>`;
            currentLeadSources.forEach(src => {
                const isSelected = selectedLeadSources.has(src);
                html += `<span class="capsule-lead-btn ${isSelected ? 'active' : ''}" onclick="toggleLeadSource('${escapeHtml(src)}')">${escapeHtml(src)}</span>`;
            });
            container.innerHTML = html;
        }

        function toggleSource(src) {
            if (selectedSources.has(src)) {
                selectedSources.delete(src);
            } else {
                selectedSources.add(src);
            }
            renderSourceCapsules();
            renderTable();
        }

        function clearSourceFilter() {
            selectedSources.clear();
            renderSourceCapsules();
            renderTable();
        }

        function toggleSourceMode() {
            activeSourceMode = (activeSourceMode === 'include') ? 'exclude' : 'include';
            const btn = document.getElementById('source-mode-btn');
            if (activeSourceMode === 'exclude') {
                btn.classList.add('exclude-active');
                btn.innerHTML = '🚫 排除反选模式';
            } else {
                btn.classList.remove('exclude-active');
                btn.innerHTML = '🔍 仅看模式';
            }
            renderSourceCapsules();
            renderTable();
        }

        function toggleLeadSource(src) {
            if (selectedLeadSources.has(src)) {
                selectedLeadSources.delete(src);
            } else {
                selectedLeadSources.add(src);
            }
            renderLeadSourceCapsules();
            renderTable();
        }

        function clearLeadSourceFilter() {
            selectedLeadSources.clear();
            renderLeadSourceCapsules();
            renderTable();
        }

        async function fetchUsers() {
            const res = await fetch('/api/users');
            currentUsers = await res.json();
            renderUsersTable();
        }

        // 筛选逻辑
        function filterCountry(country, btn) {
            switchPipelineMarket(country, document.getElementById(country + '-nav'));
        }

        function filterTier(tier, btn) {
            activeTier = tier;
            btn.parentElement.querySelectorAll('button').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            renderTable();
        }

        function filterTouch(touchMode, btn) {
            activeTouch = touchMode;
            btn.parentElement.querySelectorAll('button').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            renderTable();
        }

        function filterStage(stage) {
            activeStage = stage;
            renderTable();
        }

        function resetFilters() {
            selectedSources.clear();
            activeSourceMode = 'include';
            selectedLeadSources.clear();
            activeTier = 'all';
            activeTouch = 'all';
            activeStage = 'all';

            document.querySelectorAll('.filter-group button').forEach(b => {
                b.classList.remove('active');
                if (b.innerText.includes('全部')) {
                    b.classList.add('active');
                }
            });
            const modeBtn = document.getElementById('source-mode-btn');
            if (modeBtn) {
                modeBtn.classList.remove('exclude-active');
                modeBtn.innerHTML = '🔍 仅看模式';
            }
            document.getElementById('country-filter').value = activeCountry;
            document.getElementById('tier-filter').value = 'all';
            document.getElementById('type-filter').value = 'all';
            document.getElementById('due-filter').value = 'all';
            document.getElementById('industry-filter').value = 'all';
            document.getElementById('spend-filter').value = 'all';
            document.getElementById('stage-filter').value = 'all';
            document.getElementById('exclusion-filter').value = 'active';
            renderSourceCapsules();
            renderLeadSourceCapsules();
            renderTable();
        }

        function renderTable() {
            const tbody = document.getElementById('pipeline-body');
            tbody.innerHTML = '';
            const heading = document.getElementById('company-description-heading');
            if (heading) heading.textContent = activeCountry === 'overseas' ? '企业描述' : '赛道/类目';
            
            const scopedRows = marketRows();
            const filtered = scopedRows.filter(item => {
                const exclusion = document.getElementById('exclusion-filter').value;
                const excluded = item.prospect_status === 'lost';
                if (exclusion === 'active' && excluded || exclusion === 'excluded' && !excluded) return false;
                const type = document.getElementById('type-filter').value;
                if (type !== 'all' && (item.customer_type || 'unknown') !== type) return false;
                const due = document.getElementById('due-filter').value;
                const today = new Intl.DateTimeFormat('sv-SE', {timeZone:'Asia/Shanghai'}).format(new Date());
                if (due === 'due' && (!item.company_follow_up_date || item.company_follow_up_date > today)) return false;
                if (due === 'unclaimed' && item.owner) return false;
                const industry = document.getElementById('industry-filter').value;
                if (industry !== 'all' && item.industry !== industry) return false;
                const spend = item.estimated_monthly_spend;
                const bucket = spend == null ? 'unknown' : spend < 5000 ? 'small' : spend < 20000 ? 'medium' : spend < 50000 ? 'large' : 'enterprise';
                const spendFilter = document.getElementById('spend-filter').value;
                if (spendFilter !== 'all' && bucket !== spendFilter) return false;
                if (activeCountry !== 'all' && item.country_type !== activeCountry) {
                    return false;
                }
                
                // 公司来源筛选 (支持正选'仅看'与反选'排除/不看'，且支持多选集合)
                if (selectedSources.size > 0) {
                    const itemSrc = item.source_channel || '';
                    if (activeSourceMode === 'include') {
                        if (!selectedSources.has(itemSrc)) return false;
                    } else if (activeSourceMode === 'exclude') {
                        if (selectedSources.has(itemSrc)) return false;
                    }
                }

                // 线索来源多选筛选
                if (selectedLeadSources.size > 0) {
                    const itemLeadSrc = item.lead_source || '';
                    if (!selectedLeadSources.has(itemLeadSrc)) return false;
                }

                if (activeTier !== 'all' && (item.client_tier || 'B') !== activeTier) {
                    return false;
                }
                
                const st = item.status || 'pending';
                const isTouched = (st !== 'pending');

                if (activeTouch === 'untouched' && isTouched) {
                    return false;
                }
                if (activeTouch === 'touched' && !isTouched) {
                    return false;
                }

                if (activeStage !== 'all' && st !== activeStage) {
                    return false;
                }

                return true;
            });

            visibleData = filtered;
            renderTouchFunnel('pipeline-touch-funnel', filtered);
            document.getElementById('filter-count').innerText = `筛选出 ${filtered.length} / ${scopedRows.length} 家企业`;

            filtered.forEach(r => {
                const tr = document.createElement('tr');
                const isCn = r.country_type === 'domestic';
                const tagClass = marketTagClass(r.country_type);
                const tagLabel = marketLabel(r.country_type);
                const contactName = r.contact_name || '';
                const xUrl = /^https:\/\/(?:x\.com|twitter\.com)\//i.test(r.x_url || '') ? r.x_url : '';
                const contact = contactName
                    ? `${escapeHtml(contactName)}${xUrl ? ` <a href="${escapeHtml(xUrl)}" onclick="event.stopPropagation()" target="_blank" rel="noopener noreferrer" class="company-link" title="打开创始人 X 主页">X ↗</a>` : ''}`
                    : '<span style="color:#64748b;">创始人待核验</span>';
                const phoneDigits = String(r.phone || '').replace(/\D/g, '').replace(/^86(?=1\d{10}$)/, '');
                const validDomesticMobile = !isCn || /^1[3-9]\d{9}$/.test(phoneDigits);
                const direct = [
                    r.phone && validDomesticMobile ? `手机：${r.phone}` : '',
                    r.wechat ? `微信：${r.wechat}` : '',
                    r.email ? `邮箱：${r.email}` : ''
                ].filter(Boolean).join(' · ') || (r.phone && !validDomesticMobile ? '座机/总机已拦截，创始人手机待核验' : '-');
                const verificationLabels = {
                    sales_ready:'可销售触达', contact_verified:'联系方式已核验',
                    contact_partial:'部分联系方式已核验', identity_verified:'身份已核验',
                    pending_review:'待核验', rejected:'已拒绝'
                };
                const verification = verificationLabels[r.verification_status] || '待核验';
                const currentStatus = r.status || 'pending';
                const statusClass = 'status-' + currentStatus;

                // 等级徽章
                const tier = r.client_tier || 'B';
                const tierRule = TIER_RULES[tier] || TIER_NAME_MAP[tier] || tier;
                const tierBadge = `<span class="tier-badge tier-${tier}" title="${escapeHtml(tierRule)}">${tier}级</span>`;

                // 官网链接
                let companyDisplay = `<strong>${escapeHtml(r.company_name)}</strong>`;
                if (r.website && r.website.trim()) {
                    let url = r.website.trim();
                    if (!url.startsWith('http://') && !url.startsWith('https://')) {
                        url = 'https://' + url;
                    }
                    companyDisplay = `<strong>${escapeHtml(r.company_name)}</strong> <a href="${escapeHtml(url)}" onclick="event.stopPropagation()" target="_blank" rel="noopener noreferrer" class="company-link" title="打开官网">↗</a>`;
                }

                companyDisplay += `<div style="font-size:11px;color:#9ca3af" title="${escapeHtml(r.spend_basis || '暂无可靠消费估算依据')}">${isCn ? '行业：' + escapeHtml(r.industry || '待评估') + ' · ' : ''}月预计消费：${r.estimated_monthly_spend == null ? '待评估' : '约 ¥' + Number(r.estimated_monthly_spend).toLocaleString()}</div>`;

                // 来源标签
                const companySource = r.source_channel ? `<span class="source-tag">${r.source_channel}</span>` : '<span style="color:#64748b;">-</span>';
                const leadSource = r.lead_source ? `<span class="source-lead-tag">${r.lead_source}</span>` : '<span style="color:#64748b;">-</span>';

                // 权限限制
                const canEdit = currentUser && (currentUser.role === 'admin' || currentUser.role === 'sales_lead' || currentUser.role === 'sales_rep');

                const rJson = escapeHtml(JSON.stringify(r));

                const indHint = getEnZhHint(r.industry);
                const industryDisplay = indHint ? `${escapeHtml(r.industry)} <span style="color:#38bdf8;font-size:11px;">(${escapeHtml(indHint)})</span>` : escapeHtml(r.industry || '-');
                const descriptionDisplay = `<details class="company-description"><summary title="点击展开企业描述与来源"><span>${escapeHtml(r.company_description || '企业描述待核实；原行业分类：' + (r.industry || '未分类'))}</span></summary><div class="description-source">${escapeHtml(r.description_source || '暂无已核实来源')}</div></details>`;

                tr.innerHTML = `
                    <td style="text-align:center;">
                        <span class="editable-cell" onclick="${canEdit ? `openEditCompanyModal(${rJson})` : `alert('无权限编辑')`}" title="点击修改等级与企业信息">${tierBadge}</span>
                    </td>
                    <td>
                        <span class="editable-cell" onclick="openProspectDetails(${r.company_id})" title="查看企业详情">
                            ${companyDisplay}
                        </span>
                        ${r.company_notes ? `<details style="margin-top:6px;font-size:12px;"><summary class="company-note-summary">公司业务备注：${escapeHtml(r.company_notes)}</summary><div style="white-space:pre-wrap;overflow-wrap:anywhere;">${escapeHtml(r.company_notes)}</div></details>` : ''}
                    </td>
                    <td>
                        <span class="editable-cell" onclick="${canEdit ? `openEditCompanyModal(${rJson})` : `alert('无权限编辑')`}" title="点击修改公司来源渠道">${companySource}</span>
                    </td>
                    <td>
                        ${isCn ? `<span class="editable-cell" onclick="${canEdit ? `openEditCompanyModal(${rJson})` : `alert('无权限编辑')`}" title="点击修改赛道/类目">${industryDisplay}</span>` : descriptionDisplay}
                    </td>
                    <td>
                        <span class="editable-cell" onclick="${canEdit ? `openEditCompanyModal(${rJson})` : `alert('无权限编辑')`}" title="点击修改市场区域"><span class="tag ${tagClass}">${tagLabel}</span></span>
                    </td>
                    <td>
                        <span class="editable-cell" onclick="${canEdit && r.lead_id ? `openEditLeadModal(${rJson})` : canEdit ? `alert('暂未录入决策人，请在数据库添加')` : `alert('无权限编辑')`}" title="点击修改决策人姓名">${contact}</span>
                    </td>
                    <td>
                        <span class="editable-cell" onclick="${canEdit && r.lead_id ? `openEditLeadModal(${rJson})` : `alert('无权限编辑')`}" title="点击查看字段级证据"><code>${escapeHtml(direct)}</code><small style="display:block;color:${r.verification_status === 'sales_ready' ? '#34d399' : '#fbbf24'}">${escapeHtml(verification)} · ${Number(r.fit_score || 0)}分</small></span>
                    </td>
                    <td>
                        <span class="editable-cell" onclick="${canEdit && r.lead_id ? `openEditLeadModal(${rJson})` : `alert('无权限编辑')`}" title="点击修改线索来源渠道">${leadSource}</span>
                    </td>
                    <td><span class="status-badge ${statusClass}">${STATUS_TEXT_MAP[currentStatus] || currentStatus}</span></td>
                    <td>
                        ${r.exclusion_reason
                            ? `<span title="${escapeHtml(r.exclusion_reason)}">${escapeHtml(r.exclusion_reason)}</span>`
                            : '<span style="color:#64748b;">—</span>'}
                    </td>
                    <td>
                        <div class="action-cell">
                            <button onclick="openProspectDetails(${r.company_id})">客户详情</button>
                            ${r.prospect_status === 'lost' ? `<span title="${escapeHtml(r.exclusion_reason || '历史标记，未填写原因')}">已剔除</span><button onclick="restoreProspect(${r.company_id})" ${!canEdit ? 'disabled' : ''}>恢复</button>` : `<button onclick="excludeProspect(${r.company_id})" ${!canEdit ? 'disabled' : ''}>剔除</button>`}
                            ${r.lead_id ? `
                                <select class="select-status" onchange="updateLeadStatus(${r.lead_id}, this.value, this)" ${!canEdit ? 'disabled' : ''}>
                                    <optgroup label="未触达">
                                        <option value="pending" ${currentStatus === 'pending' ? 'selected' : ''}>⏳ 待触达 (未跟进)</option>
                                    </optgroup>
                                    <optgroup label="已触达推进阶段">
                                        <option value="sample_sent" ${currentStatus === 'sample_sent' ? 'selected' : ''}>📤 已推送未回复</option>
                                        <option value="contacted" ${currentStatus === 'contacted' ? 'selected' : ''}>🔗 已建连</option>
                                        <option value="negotiation" ${currentStatus === 'negotiation' ? 'selected' : ''}>💡 有意向</option>
                                        <option value="team_trial" ${currentStatus === 'team_trial' ? 'selected' : ''}>📝 已注册</option>
                                        <option value="won" ${currentStatus === 'won' ? 'selected' : ''}>💰 已充值</option>
                                        <option value="lost" ${currentStatus === 'lost' ? 'selected' : ''}>⛔ 不触达</option>
                                    </optgroup>
                                </select>
                                <button class="btn-pitch" onclick="openPitch(${r.lead_id})">🎯 直触方案</button>
                            ` : `<button disabled style="opacity:0.4;">待补线索</button>`}
                        </div>
                    </td>
                `;
                const pitchCell = document.createElement('td');
                if (r.pitch_angle) {
                    const preview = document.createElement('button');
                    preview.className = 'pitch-preview';
                    preview.type = 'button';
                    preview.textContent = r.pitch_angle;
                    preview.title = '点击查看完整推送话术';
                    preview.addEventListener('click', () => openSavedPitch(r.company_name, r.pitch_angle));
                    pitchCell.appendChild(preview);
                } else {
                    pitchCell.textContent = '—';
                }
                tr.appendChild(pitchCell);
                tbody.appendChild(tr);
            });
        }

        function renderUsersTable() {
            const tbody = document.getElementById('users-body');
            tbody.innerHTML = '';
            const isAdmin = currentUser && currentUser.role === 'admin';
            currentUsers.forEach(u => {
                const tr = document.createElement('tr');
                tr.innerHTML = `
                    <td><strong>${u.username}</strong></td>
                    <td>${u.display_name}</td>
                    <td><span class="source-tag">${ROLE_NAME_MAP[u.role] || u.role}</span></td>
                    <td><code>${u.email || '-'}</code></td>
                    <td>${u.phone || '-'}</td>
                    <td><span style="color:#10b981; font-weight:600;">● 激活启用</span></td>
                    <td>
                        <select onchange="updateUserRole(${u.id}, this.value)" ${!isAdmin ? 'disabled title="仅超级管理员可调整角色"' : ''}>
                            <option value="admin" ${u.role === 'admin' ? 'selected' : ''}>👑 超级管理员</option>
                            <option value="sales_lead" ${u.role === 'sales_lead' ? 'selected' : ''}>💼 销售总监</option>
                            <option value="sales_rep" ${u.role === 'sales_rep' ? 'selected' : ''}>📞 销售专员</option>
                            <option value="observer" ${u.role === 'observer' ? 'selected' : ''}>👀 运营观察员</option>
                        </select>
                    </td>
                `;
                tbody.appendChild(tr);
            });
        }

        async function updateUserRole(userId, newRole) {
            const res = await fetch('/api/user/role', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ user_id: userId, role: newRole })
            });
            const result = await res.json();
            if (result.success) {
                alert('✅ 权限配置已实时更新生效！');
                fetchUsers();
            } else {
                alert('修改失败: ' + result.error);
            }
        }

        const FUNNEL_STAGES = ['pending', 'sample_sent', 'contacted', 'negotiation', 'team_trial', 'won'];
        function updateReportDate(now = new Date()) {
            const date = document.getElementById('report-date');
            date.textContent = new Intl.DateTimeFormat('zh-CN', {
                timeZone:'Asia/Shanghai', year:'numeric', month:'long', day:'numeric', weekday:'long'
            }).format(now);
            date.dateTime = new Intl.DateTimeFormat('sv-SE', {timeZone:'Asia/Shanghai'}).format(now);
        }
        function buildTouchFunnelData(rows) {
            const unique = new Map(rows.map(r => [r.company_id, r]));
            const counts = Object.fromEntries([...FUNNEL_STAGES, 'lost', 'unknown'].map(s => [s, 0]));
            unique.forEach(r => {
                const status = r.status || 'pending';
                counts[Object.prototype.hasOwnProperty.call(counts, status) ? status : 'unknown']++;
            });
            const reached = FUNNEL_STAGES.map((s, i) =>
                FUNNEL_STAGES.slice(i).reduce((n, stage) => n + counts[stage], 0));
            return {total:unique.size, counts, reached};
        }
        function renderTouchFunnel(target, rows) {
            updateReportDate();
            const data = buildTouchFunnelData(rows);
            if (activeCountry === 'overseas') {
                const overseasCards = [
                    ['海外企业汇总', data.total],
                    ['待触达', data.counts.pending],
                    ['不触达', data.counts.lost],
                    ['已建联', data.counts.contacted],
                    ['有意向', data.counts.negotiation],
                    ['已注册', data.counts.team_trial],
                    ['已充值', data.counts.won],
                ];
                document.getElementById(target).innerHTML = `<h3>海外客户汇总</h3>
                    <div class="touch-funnel-row">${overseasCards.map(([label, count]) =>
                        `<div class="touch-funnel-stage"><span>${label}</span><strong>${count}</strong></div>`
                    ).join('')}</div>
                    <p>企业去重，按当前触达状态统计；已推送未回复 ${data.counts.sample_sent} 家。</p>`;
                return;
            }
            const labels = ['待触达', '已推送未回复', '已建连', '有意向', '已注册', '已充值'];
            const pct = (n, d) => d ? (100 * n / d).toFixed(1) + '%' : '—';
            document.getElementById(target).innerHTML = `<h3>触达动作转化漏斗</h3>
                <p>当前筛选范围：${data.total} 家企业 · 已充值 / 有效客户：${pct(data.counts.won, data.reached[0])}</p>
                <div class="touch-funnel-row">${FUNNEL_STAGES.map((s, i) =>
                    `<div class="touch-funnel-stage"><span>${labels[i]}</span><strong>${data.counts[s]}${activeCountry === 'overseas' ? '' : ' <small>家（当前停留）</small>'}</strong>
                    <small>本阶段及以后：${data.reached[i]} 家</small>
                    <small>${i ? '较上阶段推进：' + pct(data.reached[i], data.reached[i - 1]) : '有效客户基数'}</small></div>`
                ).join('')}<div class="touch-funnel-stage"><span>不触达</span><strong>${data.counts.lost} <small>家</small></strong>
                <small>单列，不计入转化分母</small></div></div>
                <p>企业去重，仅按“触达动作”统计，不合并客户详情阶段。推进比例由当前阶段推算，并非历史转化率。未识别动作：${data.counts.unknown} 家（不计入转化分母）。</p>`;
        }
        function buildFunnelData(rows, country, tier, today) {
            const aliases = {researching:'pending', trial:'team_trial'};
            const unique = new Map();
            rows.forEach(r => {
                if (country !== 'all' && r.country_type !== country) return;
                if (tier !== 'all' && (r.client_tier || 'B') !== tier) return;
                unique.set(r.company_id, r);
            });
            const counts = Object.fromEntries(FUNNEL_STAGES.map(s => [s, 0]));
            const groups = {lost:[], unknown:[], overdue:[], due:[], unscheduled:[], noContact:[]};
            FUNNEL_STAGES.forEach(s => groups[s] = []);
            [...unique.values()].forEach(r => {
                const statuses = [r.prospect_status || 'pending', r.status || 'pending'];
                if (statuses.includes('lost')) { groups.lost.push(r); return; }
                const normalized = statuses.map(s => aliases[s] || s);
                if (normalized.some(s => !FUNNEL_STAGES.includes(s))) { groups.unknown.push(r); return; }
                const stage = FUNNEL_STAGES[Math.max(...normalized.map(s => FUNNEL_STAGES.indexOf(s)))];
                counts[stage]++;
                groups[stage].push(r);
                if (stage === 'won') return;
                const date = [r.company_follow_up_date, r.next_follow_up_date].filter(Boolean).sort()[0];
                if (!date) groups.unscheduled.push(r);
                else if (date < today) groups.overdue.push(r);
                else if (date === today) groups.due.push(r);
                if (![r.phone, r.wechat, r.email].some(v => String(v || '').trim())) groups.noContact.push(r);
            });
            const active = Object.values(counts).reduce((a, b) => a + b, 0);
            const reached = FUNNEL_STAGES.map((s, i) => FUNNEL_STAGES.slice(i).reduce((n, k) => n + counts[k], 0));
            return {total:unique.size, active, counts, groups, reached};
        }
        let funnelGroups = {};
        function showFunnelCustomers(key) {
            const rows = funnelGroups[key] || [];
            document.getElementById('funnel-customers').innerHTML = `<h3>客户明细（${rows.length} 家）</h3>` +
                (rows.length ? rows.map(r => `<div style="padding:8px;border-bottom:1px solid #334155">
                    <button onclick="openProspectDetails(${Number(r.company_id)})">${escapeHtml(r.company_name)}</button>
                    <span>${escapeHtml(r.owner || '未分配')} · 企业跟进：${escapeHtml(r.company_follow_up_date || '未设置')} · 触达跟进：${escapeHtml(r.next_follow_up_date || '未设置')}</span>
                </div>`).join('') : '<p>暂无客户</p>');
        }
        async function renderDailyFunnelTable() {
            const country = document.getElementById('funnel-country').value;
            const tier = document.getElementById('funnel-tier').value;
            const params = new URLSearchParams({days:'30'});
            if (country !== 'all') params.set('country', country);
            if (tier !== 'all') params.set('tier', tier);
            const res = await fetch('/api/funnel/daily?' + params.toString());
            const data = await res.json();
            const pct = (n, d) => d ? (100 * n / d).toFixed(1) + '%' : '—';
            const rows = data.rows || [];
            const body = rows.map(row => `<tr>
                <td>${escapeHtml(row.date)}</td>
                <td>${row.sample_sent}</td><td>${row.contacted}</td><td>${row.negotiation}</td>
                <td>${row.team_trial}</td><td>${row.won}</td><td>${row.lost}</td>
                <td class="rate">${pct(row.contacted, row.sample_sent)}</td>
                <td class="rate">${pct(row.negotiation, row.contacted)}</td>
                <td class="rate">${pct(row.team_trial, row.negotiation)}</td>
                <td class="rate">${pct(row.won, row.team_trial)}</td>
            </tr>`).join('');
            document.getElementById('daily-funnel-table').innerHTML = `<h3>最近30天触达动作</h3>
                <p>日期为北京时间；数字按当日新建触达记录统计。转化率为当日相邻动作数量比，不代表同一批客户的历史 cohort 转化。</p>
                <table><thead><tr><th>日期</th><th>已推送</th><th>已建连</th><th>有意向</th><th>已注册</th><th>已充值</th><th>不触达</th>
                <th>推送→建连</th><th>建连→意向</th><th>意向→注册</th><th>注册→充值</th></tr></thead>
                <tbody>${body}</tbody></table>`;
        }
        function renderFunnelView() {
            const today = new Intl.DateTimeFormat('sv-SE', {timeZone:'Asia/Shanghai'}).format(new Date());
            const data = buildFunnelData(currentData, document.getElementById('funnel-country').value,
                document.getElementById('funnel-tier').value, today);
            funnelGroups = data.groups;
            renderDailyFunnelTable();
            const pct = (n, d) => d ? (100 * n / d).toFixed(1) + '%' : '—';
            const labels = ['待触达 / 背调', '已推送未回复', '已建连', '有意向 / 洽谈', '已注册 / 试用', '已充值 / 成交'];
            let html = `<h3>跟进概览</h3><p>范围内 ${data.total} 家 · 有效阶段 ${data.active} 家 · 已剔除/流失 ${data.groups.lost.length} 家 · 未识别状态 ${data.groups.unknown.length} 家</p>
                <p>已推进 ${data.active - data.counts.pending} 家（${pct(data.active - data.counts.pending, data.active)}） · 已成交 ${data.counts.won} 家（${pct(data.counts.won, data.active)}）</p>
                <p style="color:var(--text-muted)">企业去重；详情阶段与触达状态取较前进阶段，任一为流失则单列。背调归待触达、试用归已注册。阶段占比不是历史转化率，未成交金额不作为收入。</p>
                <h3>阶段存量与推进漏斗</h3><div style="overflow-x:auto"><table class="pipeline-table"><thead><tr><th>阶段</th><th>当前停留</th><th>阶段占比</th><th>本阶段及以后</th><th>相邻阶段推进比例（推算）</th></tr></thead><tbody>`;
            FUNNEL_STAGES.forEach((s, i) => {
                html += `<tr><td><button onclick="showFunnelCustomers('${s}')">${labels[i]}</button></td>
                    <td>${data.counts[s]} 家</td><td>${pct(data.counts[s], data.active)}</td>
                    <td><div style="background:#334155;height:6px"><div style="background:#38bdf8;height:6px;width:${data.active ? data.reached[i] / data.active * 100 : 0}%"></div></div>${data.reached[i]} 家</td>
                    <td>${i ? pct(data.reached[i], data.reached[i - 1]) : '—'}</td></tr>`;
            });
            html += '</tbody></table></div><h3 style="margin-top:20px">优先跟进</h3><p>仅统计未成交且阶段可识别的客户；以两个跟进日期中较早者为准，以下待办可重叠。</p>';
            const actions = {overdue:'已逾期', due:'今天到期', unscheduled:'未设跟进日期', noContact:'缺少电话 / 微信 / 邮箱', lost:'已剔除 / 流失', unknown:'状态待检查'};
            Object.entries(actions).forEach(([key, label]) => {
                html += `<button style="margin:4px" onclick="showFunnelCustomers('${key}')">${label}：${data.groups[key].length} 家</button>`;
            });
            html += '<div id="funnel-customers" style="margin-top:20px">点击阶段或待办查看客户，点击企业名称可编辑详情。</div>';
            document.getElementById('funnel-container').innerHTML = html;
        }

        async function updateLeadStatus(leadId, newStatus, selectElem) {
            try {
                selectElem.disabled = true;
                const lead = currentData.find(row => Number(row.lead_id) === Number(leadId));
                const res = await fetch('/api/touch', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        lead_id: leadId,
                        status: newStatus,
                        channel: lead?.channel || 'wechat_direct',
                        notes: `在 Web 作战台切换状态为: ${newStatus} (操作人: ${currentUser.display_name || currentUser.username})`
                    })
                });
                const result = await res.json();
                if (result.success) {
                    await fetchStats();
                    await fetchPipeline();
                } else {
                    alert('更新失败: ' + (result.error || '未知错误'));
                }
            } catch (err) {
                alert('网络异常: ' + err.message);
            } finally {
                selectElem.disabled = false;
            }
        }

        let currentPitchData = null;
        let currentPitchLangMode = 'bilingual'; // 'bilingual', 'en_only', 'zh_only'

        async function openPitch(leadId) {
            const res = await fetch(`/api/pitch?lead_id=${leadId}`);
            const data = await res.json();
            currentPitchData = data;
            document.getElementById('modal-title').innerText = data.headline;
            renderPitchModalContent();
            document.getElementById('modal').style.display = 'flex';
        }

        function setPitchLangMode(mode) {
            currentPitchLangMode = mode;
            renderPitchModalContent();
        }

        function copyPitchText(text, btnElem) {
            navigator.clipboard.writeText(text).then(() => {
                const orig = btnElem.innerText;
                btnElem.innerText = '✓ 已复制';
                btnElem.style.color = '#34d399';
                setTimeout(() => {
                    btnElem.innerText = orig;
                    btnElem.style.color = '';
                }, 1500);
            }).catch(err => {
                alert('复制失败，请手动选择复制');
            });
        }

        function renderPitchModalContent() {
            const data = currentPitchData;
            if (!data) return;
            let html = `<h4>方案依据与核验提醒（内部参考，不直接发送）</h4><pre>${escapeHtml(data.research_context || '请先确认客户需求')}</pre>`;
            if (!data.phone_script) html += `<h4>试点与跟进建议</h4><pre>${escapeHtml(data.strategy || '')}</pre>`;
            if (data.phone_script) {
                html += `<h4>【微信申请草稿】</h4><pre>${escapeHtml(data.wechat_request)}</pre>`;
                html += `<h4>【电话需求确认草稿】</h4><pre>${escapeHtml(data.phone_script)}</pre>`;
                html += `<h4>【跟进策略】</h4><pre>${escapeHtml(data.strategy)}</pre>`;
            } else {
                // 海外直触 - 支持中英文双语对照与一键复制英文
                html += `
                    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;">
                        <div class="pitch-lang-tabs">
                            <button class="pitch-lang-tab ${currentPitchLangMode === 'bilingual' ? 'active' : ''}" onclick="setPitchLangMode('bilingual')">📖 中英双语对照</button>
                            <button class="pitch-lang-tab ${currentPitchLangMode === 'zh_only' ? 'active' : ''}" onclick="setPitchLangMode('zh_only')">🇨🇳 仅中文翻译</button>
                            <button class="pitch-lang-tab ${currentPitchLangMode === 'en_only' ? 'active' : ''}" onclick="setPitchLangMode('en_only')">🌐 仅英文原文</button>
                        </div>
                        <div style="font-size:12px; color:var(--text-muted);">
                            💡 提示：点击各模块右上角按钮可直接复制英文原版外发
                        </div>
                    </div>
                `;

                // 1. Cold Email 邮件标题
                html += `
                    <div class="pitch-section-header">
                        <h4>📧 Cold Email 邮件标题 (Subject)</h4>
                        <button class="pitch-copy-btn" onclick="copyPitchText(${JSON.stringify(data.email_subject || '')}, this)">📋 复制英文标题</button>
                    </div>
                `;
                if (currentPitchLangMode !== 'zh_only') {
                    html += `<pre style="margin-top:4px;">${escapeHtml(data.email_subject || '')}</pre>`;
                }
                if (currentPitchLangMode !== 'en_only' && data.email_subject_zh) {
                    html += `<div class="pitch-trans-box"><strong>【中文翻译】</strong> ${escapeHtml(data.email_subject_zh)}</div>`;
                }

                // 2. Cold Email 正文
                html += `
                    <div class="pitch-section-header">
                        <h4>📝 Cold Email 邮件正文 (Body)</h4>
                        <button class="pitch-copy-btn" onclick="copyPitchText(${JSON.stringify(data.email_body || '')}, this)">📋 复制英文正文</button>
                    </div>
                `;
                if (currentPitchLangMode !== 'zh_only') {
                    html += `<pre style="margin-top:4px;">${escapeHtml(data.email_body || '')}</pre>`;
                }
                if (currentPitchLangMode !== 'en_only' && data.email_body_zh) {
                    html += `<div class="pitch-trans-box" style="white-space: pre-wrap;"><strong>【中文对照翻译】</strong>
${escapeHtml(data.email_body_zh)}</div>`;
                }

                // 3. LinkedIn 破冰
                html += `
                    <div class="pitch-section-header">
                        <h4>💼 LinkedIn 领英破冰附言 (Connect Note)</h4>
                        <button class="pitch-copy-btn" onclick="copyPitchText(${JSON.stringify(data.linkedin_note || '')}, this)">📋 复制英文附言</button>
                    </div>
                `;
                if (currentPitchLangMode !== 'zh_only') {
                    html += `<pre style="margin-top:4px;">${escapeHtml(data.linkedin_note || '')}</pre>`;
                }
                if (currentPitchLangMode !== 'en_only' && data.linkedin_note_zh) {
                    html += `<div class="pitch-trans-box"><strong>【中文对照翻译】</strong> ${escapeHtml(data.linkedin_note_zh)}</div>`;
                }
            }
            document.getElementById('modal-body').innerHTML = html;
        }

        function closeModal() {
            document.getElementById('modal').style.display = 'none';
        }

        const LEAD_VERIFICATION_TEXT = {
            pending_review:'待核验', identity_verified:'身份已核验',
            contact_partial:'部分联系方式已核验', contact_verified:'联系方式已核验',
            sales_ready:'可销售触达', rejected:'已拒绝'
        };
        function openEditLeadModal(row) {
            document.getElementById('edit-lead-id').value = row.lead_id || '';
            document.getElementById('edit-lead-name').value = row.contact_name || '';
            document.getElementById('edit-lead-title').value = row.job_title || '';
            document.getElementById('edit-lead-identity-url').value = row.identity_source_url || '';
            document.getElementById('edit-lead-identity-evidence').value = row.identity_evidence || '';
            document.getElementById('edit-lead-phone').value = row.phone || '';
            document.getElementById('edit-lead-phone-url').value = row.phone_source_url || '';
            document.getElementById('edit-lead-phone-evidence').value = row.phone_evidence || '';
            document.getElementById('edit-lead-phone-confidence').value = row.phone_confidence || '';
            document.getElementById('edit-lead-wechat').value = row.wechat || '';
            document.getElementById('edit-lead-wechat-url').value = row.wechat_source_url || '';
            document.getElementById('edit-lead-wechat-evidence').value = row.wechat_evidence || '';
            document.getElementById('edit-lead-wechat-confidence').value = row.wechat_confidence || '';
            document.getElementById('edit-lead-same-contact').checked = Boolean(row.wechat_same_as_phone_confirmed);
            document.getElementById('edit-lead-fit-score').value = Number(row.fit_score || 0);
            document.getElementById('edit-lead-verified-at').value = row.verified_at || '';
            document.getElementById('edit-lead-email').value = row.email || '';
            document.getElementById('edit-lead-source').value = row.lead_source || '';
            document.getElementById('edit-lead-linkedin').value = row.linkedin_url || '';
            document.getElementById('edit-lead-x').value = row.x_url || '';
            document.getElementById('edit-lead-verification-status').textContent =
                `核验状态：${LEAD_VERIFICATION_TEXT[row.verification_status] || '待核验'}`;
            document.getElementById('edit-lead-modal').style.display = 'flex';
        }
        function closeEditLeadModal() {
            document.getElementById('edit-lead-modal').style.display = 'none';
        }
        async function handleSaveLead(event) {
            event.preventDefault();
            const leadId = Number(document.getElementById('edit-lead-id').value);
            const updates = {
                contact_name:document.getElementById('edit-lead-name').value.trim(),
                job_title:document.getElementById('edit-lead-title').value.trim(),
                identity_source_url:document.getElementById('edit-lead-identity-url').value.trim(),
                identity_evidence:document.getElementById('edit-lead-identity-evidence').value.trim(),
                phone:document.getElementById('edit-lead-phone').value.trim(),
                phone_source_url:document.getElementById('edit-lead-phone-url').value.trim(),
                phone_evidence:document.getElementById('edit-lead-phone-evidence').value.trim(),
                phone_confidence:document.getElementById('edit-lead-phone-confidence').value,
                wechat:document.getElementById('edit-lead-wechat').value.trim(),
                wechat_source_url:document.getElementById('edit-lead-wechat-url').value.trim(),
                wechat_evidence:document.getElementById('edit-lead-wechat-evidence').value.trim(),
                wechat_confidence:document.getElementById('edit-lead-wechat-confidence').value,
                wechat_same_as_phone_confirmed:document.getElementById('edit-lead-same-contact').checked,
                fit_score:Number(document.getElementById('edit-lead-fit-score').value || 0),
                verified_at:document.getElementById('edit-lead-verified-at').value,
                email:document.getElementById('edit-lead-email').value.trim(),
                source_method:document.getElementById('edit-lead-source').value.trim(),
                linkedin_url:document.getElementById('edit-lead-linkedin').value.trim(),
                x_url:document.getElementById('edit-lead-x').value.trim()
            };
            const response = await fetch('/api/lead/update', {
                method:'POST', headers:{'Content-Type':'application/json'},
                body:JSON.stringify({lead_id:leadId, updates})
            });
            const result = await response.json();
            if (!response.ok || !result.success) {
                alert('保存失败：' + (result.error || '请检查证据字段'));
                return;
            }
            closeEditLeadModal();
            await fetchPipeline();
        }
        function openSavedPitch(companyName, pitchText) {
            document.getElementById('modal-title').textContent = `${companyName} · 推送话术`;
            const body = document.getElementById('modal-body');
            body.replaceChildren();
            const content = document.createElement('pre');
            content.style.cssText = 'white-space:pre-wrap;word-break:break-word;line-height:1.7;font-family:inherit;';
            content.textContent = pitchText;
            body.appendChild(content);
            document.getElementById('modal').style.display = 'flex';
        }

        // 注册登录系统 Modal 与 API 处理
        function openAuthModal(defaultTab = 'login') {
            if (defaultTab === 'register' && currentUser?.role !== 'admin') return;
            document.getElementById('auth-modal').style.display = 'flex';
            document.getElementById('auth-close-btn').style.display =
                currentUser && !currentUser.must_change_password ? '' : 'none';
            switchAuthTab(defaultTab);
        }

        function closeAuthModal() {
            if (!currentUser) return;
            document.getElementById('auth-modal').style.display = 'none';
            document.getElementById('login-msg').innerText = '';
            document.getElementById('reg-msg').innerText = '';
            document.getElementById('password-msg').innerText = '';
        }

        function switchAuthTab(tab) {
            if (tab === 'register' && currentUser?.role !== 'admin') tab = 'login';
            if (tab === 'login') {
                document.getElementById('tab-login-btn').classList.add('active');
                document.getElementById('tab-register-btn').classList.remove('active');
                document.getElementById('login-form').style.display = 'block';
                document.getElementById('register-form').style.display = 'none';
                document.getElementById('password-form').style.display = 'none';
                document.getElementById('auth-modal-title').innerText = '团队账号登录';
            } else if (tab === 'register') {
                document.getElementById('tab-register-btn').classList.add('active');
                document.getElementById('tab-login-btn').classList.remove('active');
                document.getElementById('login-form').style.display = 'none';
                document.getElementById('register-form').style.display = 'block';
                document.getElementById('password-form').style.display = 'none';
                document.getElementById('auth-modal-title').innerText = '管理员创建团队账号';
            } else {
                document.getElementById('tab-register-btn').classList.remove('active');
                document.getElementById('tab-login-btn').classList.remove('active');
                document.getElementById('login-form').style.display = 'none';
                document.getElementById('register-form').style.display = 'none';
                document.getElementById('password-form').style.display = 'block';
                document.getElementById('auth-modal-title').innerText = '修改登录密码';
            }
        }

        async function initializeAuthenticatedApp(user) {
            currentUser = user;
            updateCurrentUserUI();
            if (user.must_change_password) {
                document.body.classList.remove('auth-checking');
                document.body.classList.add('auth-pending');
                openAuthModal('password');
                const msg = document.getElementById('password-msg');
                msg.style.color = '#fbbf24';
                msg.innerText = '首次登录必须修改初始密码后才能进入系统。';
                return;
            }
            const countryFilter = document.getElementById('country-filter');
            countryFilter.replaceChildren(new Option('国内', 'domestic'), new Option('海外', 'overseas'));
            countryFilter.value = activeCountry;
            countryFilter.disabled = true;
            countryFilter.title = '请通过左侧国内客户 / 海外客户菜单切换市场';
            document.body.classList.remove('auth-checking', 'auth-pending');
            document.getElementById('auth-modal').style.display = 'none';
            await fetchPipeline();
        }

        async function initializeApp() {
            try {
                const res = await fetch('/api/session', {cache:'no-store'});
                if (!res.ok) throw Error('not authenticated');
                const result = await res.json();
                await initializeAuthenticatedApp(result.user);
            } catch (_) {
                currentUser = null;
                updateCurrentUserUI();
                document.body.classList.remove('auth-checking');
                document.body.classList.add('auth-pending');
                openAuthModal('login');
            }
        }

        async function handleAuthSwitch() {
            if (!currentUser) return openAuthModal('login');
            await fetch('/api/logout', {method:'POST'});
            currentUser = null;
            currentData = [];
            document.body.classList.add('auth-pending');
            updateCurrentUserUI();
            openAuthModal('login');
        }

        async function handleLogin(e) {
            e.preventDefault();
            const username = document.getElementById('login-username').value;
            const password = document.getElementById('login-password').value;
            const msgEl = document.getElementById('login-msg');
            msgEl.innerText = '正在验证中...';
            msgEl.style.color = '#38bdf8';

            try {
                const res = await fetch('/api/login', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ username, password })
                });
                const result = await res.json();
                if (result.success) {
                    msgEl.style.color = '#10b981';
                    msgEl.innerText = '✅ 登录成功！正在进入作战台...';
                    await initializeAuthenticatedApp(result.user);
                } else {
                    msgEl.style.color = '#ef4444';
                    msgEl.innerText = '❌ 登录失败: ' + (result.error || '用户名或密码错误');
                }
            } catch (err) {
                msgEl.style.color = '#ef4444';
                msgEl.innerText = '网络异常: ' + err.message;
            }
        }

        async function handleRegister(e) {
            e.preventDefault();
            const username = document.getElementById('reg-username').value;
            const password = document.getElementById('reg-password').value;
            const display_name = document.getElementById('reg-name').value;
            const role = document.getElementById('reg-role').value;
            const email = document.getElementById('reg-email').value;
            const phone = document.getElementById('reg-phone').value;
            const msgEl = document.getElementById('reg-msg');
            msgEl.innerText = '正在提交注册...';
            msgEl.style.color = '#38bdf8';

            try {
                const res = await fetch('/api/register', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ username, password, display_name, role, email, phone })
                });
                const result = await res.json();
                if (result.success) {
                    msgEl.style.color = '#10b981';
                    msgEl.innerText = '✅ 团队账号创建成功。';
                    document.getElementById('register-form').reset();
                    await fetchUsers();
                } else {
                    msgEl.style.color = '#ef4444';
                    msgEl.innerText = '❌ 注册失败: ' + (result.error || '未知错误');
                }
            } catch (err) {
                msgEl.style.color = '#ef4444';
                msgEl.innerText = '网络异常: ' + err.message;
            }
        }

        async function handleChangePassword(e) {
            e.preventDefault();
            const current_password = document.getElementById('current-password').value;
            const new_password = document.getElementById('new-password').value;
            const confirm = document.getElementById('confirm-password').value;
            const msg = document.getElementById('password-msg');
            if (new_password !== confirm) {
                msg.style.color = '#ef4444'; msg.innerText = '两次输入的新密码不一致'; return;
            }
            const res = await fetch('/api/password', {
                method:'POST', headers:{'Content-Type':'application/json'},
                body:JSON.stringify({current_password, new_password})
            });
            const result = await res.json();
            msg.style.color = result.success ? '#10b981' : '#ef4444';
            msg.innerText = result.success ? '密码修改成功' : result.error;
            if (result.success) {
                document.getElementById('password-form').reset();
                await initializeApp();
            }
        }

        async function exportData() {
            if (!visibleData.length) {
                alert('当前筛选结果为空，没有可导出的数据。');
                return;
            }
            const columns = [
                ['客户等级', r => r.client_tier || 'B'], ['企业ID', r => r.company_id],
                ['企业名称', r => r.company_name], ['区域', r => marketLabel(r.country_type)],
                ['行业赛道', r => r.industry], ['企业描述', r => r.company_description],
                ['描述来源与核验日期', r => r.description_source], ['客户类型', r => r.customer_type],
                ['每月预计消耗', r => r.estimated_monthly_spend], ['消费估算依据', r => r.spend_basis],
                ['企业官网', r => r.website], ['公司数据来源', r => r.source_channel],
                ['客户情况备注', r => r.company_notes], ['负责人', r => r.owner],
                ['下次跟进日期', r => r.company_follow_up_date], ['决策人姓名', r => r.contact_name],
                ['真实职位', r => r.job_title], ['手机/微信', r => r.phone || r.wechat],
                ['工作邮箱', r => r.email], ['领英主页', r => r.linkedin_url],
                ['创始人X主页', r => r.x_url], ['线索来源', r => r.lead_source],
                ['当前跟进状态', r => r.status || 'pending'], ['触达渠道', r => r.channel],
                ['破冰切入点', r => r.pitch_angle], ['沟通纪要', r => r.interaction_notes]
            ];
            const cell = value => `"${String(value ?? '').replace(/"/g, '""')}"`;
            const csv = [columns.map(([name]) => cell(name)).join(',')]
                .concat(visibleData.map(r => columns.map(([, get]) => cell(get(r))).join(',')))
                .join('\\r\\n');
            const blob = new Blob(['\ufeff', csv], {type: 'text/csv;charset=utf-8'});
            const url = URL.createObjectURL(blob);
            const link = document.createElement('a');
            link.href = url;
            link.download = `Aibubu_${marketLabel(activeCountry)}客户_${new Date().toISOString().slice(0, 10)}.csv`;
            document.body.appendChild(link);
            link.click();
            link.remove();
            URL.revokeObjectURL(url);
            alert(`✅ 已导出当前筛选的 ${visibleData.length} 家企业`);
        }

        function escapeHtml(text) {
            if (!text) return '';
            return text
                .replace(/&/g, "&amp;")
                .replace(/</g, "&lt;")
                .replace(/>/g, "&gt;")
                .replace(/"/g, "&quot;")
                .replace(/'/g, "&#039;");
        }


        async function changeProspectStatus(id, updates) {
            if (!currentUser || !['admin','sales_lead','sales_rep'].includes(currentUser.role)) return;
            try {
                const res = await fetch('/api/company/update', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({company_id:id,updates})});
                const result = await res.json();
                if (!res.ok || !result.success) throw Error(result.error || '保存失败');
                await fetchPipeline();
            } catch (err) { alert(err.message); }
        }
        function excludeProspect(id) {
            const row = currentData.find(r => r.company_id === id);
            const reason = prompt('剔除「' + row.company_name + '」的原因（可在已剔除名单恢复）：');
            if (reason === null) return;
            if (!reason.trim()) { alert('请填写剔除原因'); return; }
            return changeProspectStatus(id, {prospect_status:'lost',exclusion_reason:reason.trim()});
        }
        function restoreProspect(id) {
            const row = currentData.find(r => r.company_id === id);
            if (!confirm('恢复「' + row.company_name + '」到开发名单？')) return;
            return changeProspectStatus(id, {prospect_status:row.previous_prospect_status || 'pending'});
        }
        function closeProspectDetails() {
            document.getElementById('prospect-modal').style.display='none';
        }
        document.addEventListener('keydown', event => {if(event.key === 'Escape') closeProspectDetails();});
        let activeProspect = null;
        let prospectFormBaseline = '';
        const prospectFields = {notes:'notes',company_description:'description',description_source:'description-source',exclusion_reason:'exclusion',customer_type:'type',models_used:'models',audience_scale:'audience',qualification_evidence:'evidence',discovery_url:'source',estimated_monthly_spend:'spend',spend_basis:'basis',owner:'owner',prospect_status:'status',company_follow_up_date:'date'};
        function getProspectFormState() {
            return JSON.stringify(Object.fromEntries(
                Object.values(prospectFields).map(field => [field, document.getElementById('p-'+field).value])
            ));
        }
        function updateProspectEditState() {
            const editable = currentUser && ['admin','sales_lead','sales_rep'].includes(currentUser.role);
            const dirty = editable && getProspectFormState() !== prospectFormBaseline;
            document.getElementById('prospect-save-btn').disabled = !dirty;
            document.getElementById('prospect-reset-btn').disabled = !dirty;
            const msg = document.getElementById('p-message');
            if (dirty) {
                msg.textContent = '有尚未保存的修改';
                msg.className = '';
            } else if (!msg.classList.contains('success') && !msg.classList.contains('error')) {
                msg.textContent = editable ? '修改内容后即可保存' : '当前账号仅可查看';
            }
        }
        function setProspectOwner(owner) {
            document.getElementById('p-owner').value = owner;
            updateProspectEditState();
        }
        function resetProspectForm() {
            if (activeProspect) openProspectDetails(activeProspect.company_id);
        }
        function openProspectDetails(id) {
            activeProspect = currentData.find(r => r.company_id === id);
            const r = activeProspect;
            document.getElementById('prospect-name').textContent = r.company_name;
            document.getElementById('prospect-summary').textContent = `首次入库：${r.first_discovered || '未知'} · 行业：${r.industry} · 联系人：${r.contact_name || '待查找'}`;
            for (const [key, field] of Object.entries(prospectFields)) {
                const value = key === 'notes' ? r.company_notes : r[key];
                document.getElementById('p-'+field).value = value ?? (key === 'prospect_status' ? 'pending' : '');
            }
            const reasons = [];
            if(r.qualification_evidence && r.discovery_url) reasons.push([30,'已记录需求证据和来源（仍需背调核实）']);
            if(r.customer_type === 'API / AI 应用客户') reasons.push([20,'已标记为 API / AI 应用客户']);
            if(r.models_used) reasons.push([10,'已记录模型需求']);
            if(r.estimated_monthly_spend != null && r.spend_basis) reasons.push([20,'消费估算有文字依据']);
            if(r.email || r.phone || r.wechat) reasons.push([10,'已记录联系渠道（未验证可达性）']);
            document.getElementById('p-score').textContent = `资料完整度 ${reasons.reduce((n,x)=>n+x[0],0)}/90（不是成交概率）\n` + (reasons.map(x=>`+${x[0]} ${x[1]}`).join('；') || '暂无足够资料，建议先背调');
            const msg = document.getElementById('p-message');
            msg.textContent = '修改内容后即可保存';
            msg.className = '';
            document.getElementById('prospect-modal').style.display='flex';
            const editable = currentUser && ['admin','sales_lead','sales_rep'].includes(currentUser.role);
            document.querySelectorAll('#prospect-form input,#prospect-form textarea,#prospect-form select').forEach(el=>el.disabled=!editable);
            document.querySelectorAll('#prospect-form button').forEach(el=>el.disabled=!editable);
            prospectFormBaseline = getProspectFormState();
            updateProspectEditState();
        }
        async function saveProspectDetails(e) {
            e.preventDefault();
            const saveButton = document.getElementById('prospect-save-btn');
            if (saveButton.disabled) return;
            const updates = {};
            for (const [key,field] of Object.entries(prospectFields)) updates[key]=document.getElementById('p-'+field).value.trim();
            updates.estimated_monthly_spend=updates.estimated_monthly_spend === '' ? null : Number(updates.estimated_monthly_spend);
            const msg=document.getElementById('p-message');
            if(updates.prospect_status === 'lost' && !updates.exclusion_reason) {msg.textContent='请填写剔除原因';msg.className='error';return;}
            if(updates.estimated_monthly_spend != null && !updates.spend_basis) {msg.textContent='请填写消费估算依据';msg.className='error';return;}
            saveButton.disabled = true;
            saveButton.textContent = '保存中…';
            msg.textContent = '正在保存客户详情';
            msg.className = '';
            try {
                const res=await fetch('/api/company/update',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({company_id:activeProspect.company_id,updates})});
                const result=await res.json();if(!res.ok || !result.success) throw Error(result.error || '保存失败');
                await fetchPipeline();
                activeProspect = currentData.find(r => r.company_id === activeProspect.company_id);
                prospectFormBaseline = getProspectFormState();
                msg.textContent='已保存，修改已生效';
                msg.className='success';
                updateProspectEditState();
            } catch(err) {
                msg.textContent=err.message;
                msg.className='error';
                saveButton.disabled = false;
            } finally {
                saveButton.textContent = '保存编辑';
            }
        }
        async function copyResearchPrompt() {
            const r=activeProspect;
            const prompt=`请对 ${r.company_name} 进行客户背调。官网：${r.website || '待查找'}；来源：${r.discovery_url || '待查找'}。核查运营主体、官方联系方式、是否提供AI图片/视频服务、实际接入模型、收费方案及可验证用户/调用规模。每项给出来源链接和日期，区分事实与推测，不编造月活或联系人。估算采购API或商拍服务的需求，金额仅在有需求量和价格依据时计算。最后提出适合的合作切入点。`;
            try {await navigator.clipboard.writeText(prompt);document.getElementById('p-message').textContent='背调指令已复制';} catch(e){document.getElementById('p-message').textContent=prompt;}
        }
        const prospectForm = document.getElementById('prospect-form');
        if (prospectForm && prospectForm.addEventListener) {
            prospectForm.addEventListener('input', updateProspectEditState);
            prospectForm.addEventListener('change', updateProspectEditState);
        }

        // 初始化加载
        updateReportDate();
        updateCurrentUserUI();
        initializeApp();
    </script>
</body>
</html>
"""

class CustomHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        self.db = DatabaseManager()
        super().__init__(*args, **kwargs)

    def _encode_session(self, user):
        payload = base64.urlsafe_b64encode(json.dumps({
            "uid": user["id"], "exp": int(time.time()) + SESSION_TTL
        }, separators=(",", ":")).encode()).rstrip(b"=")
        signature = hmac.new(SESSION_SECRET, payload, hashlib.sha256).hexdigest().encode()
        return (payload + b"." + signature).decode()

    def _session_user(self):
        try:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            token = cookie[SESSION_COOKIE].value
            payload, signature = token.rsplit(".", 1)
            expected = hmac.new(SESSION_SECRET, payload.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                return None
            raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
            session = json.loads(raw)
            if int(session["exp"]) < int(time.time()):
                return None
            return self.db.get_user(int(session["uid"]))
        except (KeyError, ValueError, TypeError, json.JSONDecodeError):
            return None

    def _require_user(self, roles=None):
        user = self._session_user()
        if not user:
            self.send_json({"success": False, "error": "请先登录"}, status=401)
            return None
        if user.get("must_change_password"):
            self.send_json({"success": False, "error": "请先修改初始密码", "code": "PASSWORD_CHANGE_REQUIRED"}, status=403)
            return None
        if roles and user["role"] not in roles:
            self.send_json({"success": False, "error": "无权执行此操作"}, status=403)
            return None
        return user

    @staticmethod
    def _owner_filter(user):
        return user["display_name"] if user["role"] == "sales_rep" else None

    def _can_access_owner(self, user, owner):
        return user["role"] in ("admin", "sales_lead") or (
            user["role"] == "sales_rep" and owner == user["display_name"]
        )

    def _set_session_cookie(self, user):
        secure = self.headers.get("X-Forwarded-Proto") == "https"
        cookie = (
            f"{SESSION_COOKIE}={self._encode_session(user)}; Path=/; Max-Age={SESSION_TTL}; "
            "HttpOnly; SameSite=Lax"
        )
        if secure:
            cookie += "; Secure"
        return cookie

    def handle_email_queue(self, path, method):
        roles = ("admin",) if path in (
            "/api/email-settings", "/api/email-senders", "/api/email-automation",
            "/api/email-queue/recover", "/api/email-senders/test",
        ) and method == "POST" else (
            ("admin", "sales_lead", "sales_rep") if method == "POST" else None
        )
        user = self._require_user(roles)
        if not user:
            return
        queue = EmailQueue(self.db)
        owner = self._owner_filter(user)
        if user["role"] == "observer":
            self.send_json({"success": False, "error": "观察员不能访问邮件联系方式"}, status=403)
            return
        try:
            if method == "GET" and path in ("/api/email-queue", "/api/email-settings"):
                settings = queue.settings()
                self.send_json({
                    "success": True, "settings": settings,
                    "rows": queue.rows(owner) if path == "/api/email-queue" else [],
                    "automation": queue.automation() if path == "/api/email-settings" else None,
                })
                return
            if method == "GET" and path == "/api/email-recipients":
                self.send_json({"success": True, "rows": queue.recipients(owner),
                                "senders": queue.senders(), "history": queue.history(owner),
                                "automation": queue.automation(), "packages": queue.packages(owner)})
                return
            if path.startswith("/api/email-prospects"):
                self.send_json({"success": False, "error": "独立邮件名单已停用，请从客户池加入邮件队列"}, status=410)
                return
            length = int(self.headers.get("Content-Length", 0))
            if not 0 < length <= 200000:
                raise ValueError("请求内容为空或过大")
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("无效请求")
            actor = user.get("display_name") or user["username"]
            if path == "/api/email-settings":
                self.send_json({"success": True, "settings": queue.save_settings(data)})
                return
            if path == "/api/email-senders":
                sender_id = int(data["id"]) if data.get("id") else None
                self.send_json({"success": True, "settings": queue.save_settings(data, sender_id=sender_id)})
                return
            if path == "/api/email-automation":
                self.send_json({"success": True, "automation": queue.save_automation(data)})
                return
            if path == "/api/email-senders/test":
                sender_id = int(data.get("id", 1))
                settings = queue.settings(private=True, sender_id=sender_id)
                if settings.get("provider") != "smtp":
                    raise ValueError("所选发件邮箱不是 SMTP")
                try:
                    test_smtp_connection(settings)
                except smtplib.SMTPAuthenticationError:
                    self.send_json({"success": False, "error": "SMTP 登录失败，请检查邮箱密码或客户端安全密码"}, status=400)
                    return
                except (TimeoutError, OSError):
                    self.send_json({"success": False, "error": "SMTP 服务器连接失败或超时"}, status=502)
                    return
                except smtplib.SMTPException:
                    self.send_json({"success": False, "error": "SMTP 服务器拒绝连接或登录"}, status=400)
                    return
                self.send_json({"success": True, "message": "SMTP 连接和登录验证成功，未发送邮件"})
                return
            if path == "/api/email-packages":
                if any(not self._can_access_owner(user, self.db.lead_company_owner(int(value)))
                       for value in data.get("lead_ids", [])):
                    self.send_json({"success": False, "error": "推送包包含无权访问的联系人"}, status=403)
                    return
                package = queue.create_package(data, actor, owner)
                self.send_json({"success": True, "package": package})
                return
            if path == "/api/email-packages/send":
                package_id = int(data.get("id"))
                if data.get("confirmation") != f"SEND PACKAGE {package_id}":
                    raise ValueError("推送包发送确认不匹配")
                package, settings, batch, messages = queue.send_package_rows(package_id, owner)
                try:
                    message_ids = send_mail_batch(messages, settings=settings, batch_id=batch)
                    if len(message_ids) != len(messages) or not all(message_ids):
                        raise RuntimeError("邮件服务返回结果不完整")
                    for row, provider_id in zip(messages, message_ids):
                        queue.complete(row, row["subject"], row["body"],
                                       settings["from_email"], provider_id, actor)
                except Exception:
                    self.send_json({
                        "success": False,
                        "error": "发送结果待核实，队列已锁定以防重复；请核对邮件服务器记录。",
                    }, status=502)
                    return
                queue.mark_package_sent(package_id)
                self.send_json({"success": True, "sent": len(messages), "package_id": package_id})
                return
            if path == "/api/email-queue/add":
                ids = list(dict.fromkeys(int(value) for value in data.get("lead_ids", [])))
                if not 1 <= len(ids) <= 200:
                    raise ValueError("每次请选择 1–200 位联系人")
                if any(not self._can_access_owner(user, self.db.lead_company_owner(value)) for value in ids):
                    self.send_json({"success": False, "error": "无权访问所选联系人"}, status=403)
                    return
                added, skipped = 0, []
                for value in ids:
                    try:
                        queue.add(value, actor)
                        added += 1
                    except ValueError as error:
                        skipped.append({"lead_id": value, "reason": str(error)})
                self.send_json({"success": True, "added": added, "skipped": skipped})
                return
            if path == "/api/email-queue/status":
                queue.status(int(data["id"]), str(data.get("status")), owner)
                self.send_json({"success": True})
                return
            if path == "/api/email-queue/recover":
                queue_id = int(data["id"])
                if data.get("confirmation") != f"RECOVER {queue_id}":
                    raise ValueError("恢复确认不匹配")
                queue.recover_unsent(queue_id, owner)
                self.send_json({"success": True, "id": queue_id, "status": "pending"})
                return
            if path not in ("/api/email-queue/preview", "/api/email-queue/send"):
                self.send_json({"success": False, "error": "接口不存在"}, status=404)
                return
            sender_id = int(data.get("sender_id", 1))
            settings = queue.settings(sender_id=sender_id)
            lead_ids = list(dict.fromkeys(int(value) for value in data.get("lead_ids", [])))
            ids = list(dict.fromkeys(int(value) for value in data.get("ids", [])))
            count = len(lead_ids) if "lead_ids" in data else len(ids)
            if not 1 <= count <= settings["batch_limit"]:
                raise ValueError(f'每批请选择 1–{settings["batch_limit"]} 位收件人')
            subject, body = str(data.get("subject") or "").strip(), str(data.get("body") or "").strip()
            if not subject or not body or len(subject) > 300 or len(body) > 50000:
                raise ValueError("请填写标题和正文（标题最多 300 字，正文最多 50000 字）")
            if "lead_ids" in data:
                ids = queue.prepare(lead_ids, actor, owner)
            rows = queue.select(ids, owner, sendable=True)
            previews = [{
                **row, "subject": render_email_template(subject, row),
                "body": render_email_template(body, row),
            } for row in rows]
            if path.endswith("/preview"):
                self.send_json({"success": True, "previews": previews, "count": len(rows),
                                "sender": settings["from_email"]})
                return
            if data.get("confirmation") != f"SEND {len(ids)}":
                raise ValueError("发送确认不匹配，请重新预览")
            settings = queue.settings(private=True, sender_id=sender_id)
            if not settings["configured"]:
                raise ValueError("邮件服务尚未配置，请联系管理员")
            batch = queue.claim(ids)
            try:
                message_ids = send_mail_batch(previews, settings=settings, batch_id=batch)
                if len(message_ids) != len(rows) or not all(message_ids):
                    raise RuntimeError("邮件服务返回结果不完整")
                for row, provider_id in zip(previews, message_ids):
                    queue.complete(row, row["subject"], row["body"], settings["from_email"], provider_id, actor)
            except Exception:
                # A timeout can occur after acceptance. Never reset to pending automatically.
                self.send_json({
                    "success": False,
                    "error": "发送结果待核实，已锁定发送中记录以防重复。请核对 SMTP/邮件服务器发送日志，勿重复提交。",
                }, status=502)
                return
            self.send_json({"success": True, "sent": len(rows), "failed": 0})
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
            self.send_json({"success": False, "error": str(error)}, status=400)

    def handle_feishu_mail_import(self, path, method):
        user = self._require_user(("admin",))
        if not user:
            return
        from core.feishu_import import FeishuImportError, FeishuMailSheetImporter

        importer = FeishuMailSheetImporter(self.db)
        if method == "GET" and path == "/api/feishu-mail-import/status":
            self.send_json({
                "success": True,
                "configured": importer.configured(),
                "source": "飞书邮件表",
                "spreadsheet_token_configured": bool(importer.spreadsheet_token),
                "sheet_id_configured": bool(importer.sheet_id),
            })
            return
        try:
            if method != "POST" or path not in (
                "/api/feishu-mail-import/preview", "/api/feishu-mail-import/apply"
            ):
                self.send_json({"success": False, "error": "接口不存在"}, status=404)
                return
            length = int(self.headers.get("Content-Length", 0))
            if not 0 < length <= 10000:
                raise ValueError("请求内容为空或过大")
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("无效请求")
            if path.endswith("/preview"):
                plan = importer.preview()
                self.send_json({
                    "success": True,
                    **{key: value for key, value in plan.items() if key != "entries"},
                })
                return
            if data.get("confirmation") != "IMPORT FEISHU MAIL SHEET":
                raise ValueError("导入确认不匹配，请先预览飞书邮件表")
            fingerprint = str(data.get("preview_fingerprint") or "").strip()
            if not fingerprint:
                raise ValueError("请先生成导入预览，再确认导入")
            actor = user.get("display_name") or user["username"]
            self.send_json({
                "success": True,
                **importer.apply(actor, expected_fingerprint=fingerprint),
            })
        except FeishuImportError as error:
            self.send_json({"success": False, "error": str(error)}, status=400)
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
            self.send_json({"success": False, "error": str(error)}, status=400)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/api/email-automation/run":
            secret = os.environ.get("CRON_SECRET", "").strip()
            supplied = urllib.parse.parse_qs(parsed.query).get("token", [""])[0]
            auth = self.headers.get("Authorization", "")
            if not secret or not hmac.compare_digest(supplied or auth.removeprefix("Bearer ").strip(), secret):
                self.send_json({"success": False, "error": "未授权"}, status=401)
                return
            try:
                self.send_json({"success": True, **run_email_automation(self.db)})
            except Exception as error:
                self.send_json({"success": False, "error": str(error)}, status=502)
            return
        if path.startswith("/api/email-"):
            self.handle_email_queue(path, "GET")
            return
        if path.startswith("/api/feishu-mail-import/"):
            self.handle_feishu_mail_import(path, "GET")
            return

        if path == "/" or path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_TEMPLATE.encode("utf-8"))

        elif path == "/api/session":
            user = self._session_user()
            if user:
                self.send_json(
                    {"success": True, "user": user},
                    headers={"Set-Cookie": self._set_session_cookie(user)},
                )
            else:
                self.send_json({"success": False, "error": "请先登录"}, status=401)

        elif path == "/api/stats":
            user = self._require_user()
            if not user:
                return
            owner = self._owner_filter(user)
            params = urllib.parse.parse_qs(parsed.query)
            country = params.get("country", [None])[0]
            if country not in (None, "domestic", "overseas", "pending_verification"):
                self.send_json({"success": False, "error": "无效的市场区域"}, status=400)
                return
            if owner is None and country is None:
                stats = self.db.get_stats()
            else:
                rows = self.db.query_pipeline(owner=owner, country_type=country)
                stats = {
                    "total_companies": len(rows),
                    "total_leads": sum(1 for row in rows if row.get("lead_id")),
                    "companies_by_tier": {},
                    "outreach_funnel": {},
                }
                for row in rows:
                    tier = row.get("client_tier") or "B"
                    status = row.get("status") or "pending"
                    stats["companies_by_tier"][tier] = stats["companies_by_tier"].get(tier, 0) + 1
                    stats["outreach_funnel"][status] = stats["outreach_funnel"].get(status, 0) + 1
            beijing = ZoneInfo("Asia/Shanghai")
            today_beijing = datetime.now(beijing).date()
            yesterday = today_beijing - timedelta(days=1)
            start_utc = datetime.combine(yesterday, datetime.min.time(), tzinfo=beijing).astimezone(timezone.utc)
            end_utc = datetime.combine(today_beijing, datetime.min.time(), tzinfo=beijing).astimezone(timezone.utc)
            stats["yesterday_date"] = yesterday.isoformat()
            stats["yesterday_outreach"] = self.db.get_yesterday_outreach_stats(
                start_utc.strftime("%Y-%m-%d %H:%M:%S"),
                end_utc.strftime("%Y-%m-%d %H:%M:%S"),
                owner=owner,
                country_type=country,
            )
            stats["data_last_updated"] = self.db.get_data_last_updated(
                country_type=country, owner=owner
            )
            self.send_json(stats)

        elif path == "/api/sources":
            user = self._require_user()
            if not user:
                return
            owner = self._owner_filter(user)
            sources = self.db.get_source_channels() if owner is None else sorted({
                row["source_channel"] for row in self.db.query_pipeline(owner=owner)
                if row.get("source_channel")
            })
            self.send_json(sources)

        elif path == "/api/lead_sources":
            user = self._require_user()
            if not user:
                return
            owner = self._owner_filter(user)
            lead_sources = self.db.get_lead_sources() if owner is None else sorted({
                row["lead_source"] for row in self.db.query_pipeline(owner=owner)
                if row.get("lead_source")
            })
            self.send_json(lead_sources)

        elif path == "/api/pipeline":
            user = self._require_user()
            if not user:
                return
            params = urllib.parse.parse_qs(parsed.query)
            country = params.get("country", [None])[0]
            status = params.get("status", [None])[0]
            source = params.get("source", [None])[0]
            tier = params.get("tier", [None])[0]
            rows = self.db.query_pipeline(
                country_type=country, status=status, source_channel=source,
                client_tier=tier, owner=self._owner_filter(user)
            )
            self.send_json(rows)

        elif path == "/api/funnel/daily":
            user = self._require_user()
            if not user:
                return
            params = urllib.parse.parse_qs(parsed.query)
            days = min(max(int(params.get("days", ["30"])[0]), 1), 90)
            country = params.get("country", [None])[0]
            tier = params.get("tier", [None])[0]
            beijing = ZoneInfo("Asia/Shanghai")
            today = datetime.now(beijing).date()
            first_day = today - timedelta(days=days - 1)
            day_after_last = today + timedelta(days=1)
            start_utc = datetime.combine(first_day, datetime.min.time(), tzinfo=beijing).astimezone(timezone.utc)
            end_utc = datetime.combine(day_after_last, datetime.min.time(), tzinfo=beijing).astimezone(timezone.utc)
            activity = self.db.get_daily_outreach_stats(
                start_utc.strftime("%Y-%m-%d %H:%M:%S"),
                end_utc.strftime("%Y-%m-%d %H:%M:%S"),
                owner=self._owner_filter(user),
                country_type=country,
                client_tier=tier,
            )
            statuses = ("pending", "sample_sent", "contacted", "negotiation", "team_trial", "won", "lost")
            by_day = {
                (first_day + timedelta(days=offset)).isoformat(): {status: 0 for status in statuses}
                for offset in range(days)
            }
            for item in activity:
                if item["activity_date"] in by_day and item["status"] in statuses:
                    by_day[item["activity_date"]][item["status"]] = item["count"]
            rows = [
                {"date": day, **counts}
                for day, counts in sorted(by_day.items(), reverse=True)
            ]
            self.send_json({"days": days, "timezone": "Asia/Shanghai", "rows": rows})

        elif path == "/api/strategies":
            user = self._require_user()
            if not user:
                return
            params = urllib.parse.parse_qs(parsed.query)
            start_date = params.get("start", [None])[0]
            end_date = params.get("end", [None])[0]
            version_value = params.get("version", [None])[0]
            try:
                version = int(version_value) if version_value else None
                rows = self.db.list_filter_strategies(start_date, end_date, version)
                self.send_json(rows)
            except (ValueError, TypeError) as error:
                self.send_json({"success": False, "error": str(error)}, status=400)

        elif path == "/api/discord-communities":
            user = self._require_user()
            if not user:
                return
            params = urllib.parse.parse_qs(parsed.query)
            rows = self.db.list_discord_communities(
                language=params.get("language", [None])[0],
                fit_level=params.get("fit", [None])[0],
                join_status=params.get("status", [None])[0],
            )
            self.send_json(rows)

        elif path == "/api/users":
            user = self._require_user(("admin",))
            if not user:
                return
            users = self.db.get_users()
            self.send_json(users)

        elif path == "/api/pitch":
            user = self._require_user()
            if not user:
                return
            from outreach.outreach_engine import OutreachEngine
            params = urllib.parse.parse_qs(parsed.query)
            lead_id = int(params.get("lead_id", [0])[0])
            if user["role"] == "sales_rep" and not self._can_access_owner(
                user, self.db.lead_company_owner(lead_id)
            ):
                self.send_json({"error": "无权查看该客户"}, status=403)
                return
            engine = OutreachEngine(self.db)
            try:
                pitch = engine.generate_pitch(lead_id)
                self.send_json(pitch)
            except Exception as e:
                self.send_json({"error": str(e)}, status=400)

        elif path == "/api/export":
            user = self._require_user(("admin", "sales_lead"))
            if not user:
                return
            import csv
            out_path = os.path.expanduser("~/Desktop/Aibubu_销售作战跟进表.csv")
            rows = self.db.query_pipeline()
            with open(out_path, mode="w", encoding="utf-8-sig", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "客户等级", "企业ID", "企业名称", "区域", "行业赛道", "企业描述", "描述来源与核验日期", "规模", "企业官网", "公司数据来源", "客户情况备注",
                    "线索ID", "决策人姓名", "真实职位", "手机/微信", "工作邮箱", "领英主页", "创始人X主页", "线索来源",
                    "当前跟进状态", "触达渠道", "破冰切入点", "沟通纪要"
                ])
                for r in rows:
                    writer.writerow([
                        r.get('client_tier') or 'B', r['company_id'], r['company_name'], r['country_type'], r['industry'], r.get('company_description') or '', r.get('description_source') or '', r['scale'], r['website'], r['source_channel'], r.get('company_notes') or '',
                        r['lead_id'], r['contact_name'], r['job_title'], r['phone'] or r['wechat'], r['email'], r['linkedin_url'], r.get('x_url') or '', r.get('lead_source') or '',
                        r['status'], r['channel'], r['pitch_angle'], r['interaction_notes']
                    ])
            self.send_json({"message": f"成功导出至桌面: {out_path}"})

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path.startswith("/api/feishu-mail-import/"):
            self.handle_feishu_mail_import(path, "POST")
            return
        if path.startswith("/api/email-"):
            self.handle_email_queue(path, "POST")
            return

        if path == "/api/login":
            try:
                content_length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(content_length).decode('utf-8')
                data = json.loads(body)
                username = data.get('username', '')
                password = data.get('password', '')
                user = self.db.authenticate_user(username, password)
                if user:
                    self.send_json(
                        {"success": True, "user": user},
                        headers={"Set-Cookie": self._set_session_cookie(user)},
                    )
                else:
                    self.send_json({"success": False, "error": "用户名或密码不正确"}, status=401)
            except Exception as e:
                self.send_json({"success": False, "error": str(e)}, status=400)

        elif path == "/api/logout":
            self.send_json(
                {"success": True},
                headers={"Set-Cookie": f"{SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"},
            )

        elif path == "/api/register":
            user = self._require_user(("admin",))
            if not user:
                return
            try:
                content_length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(content_length).decode('utf-8')
                data = json.loads(body)
                res = self.db.register_user(
                    username=data.get('username', ''),
                    password=data.get('password', ''),
                    display_name=data.get('display_name', ''),
                    role=data.get('role', 'sales_rep'),
                    email=data.get('email', ''),
                    phone=data.get('phone', '')
                )
                self.send_json(res)
            except Exception as e:
                self.send_json({"success": False, "error": str(e)}, status=400)

        elif path == "/api/password":
            user = self._session_user()
            if not user:
                self.send_json({"success": False, "error": "请先登录"}, status=401)
                return
            try:
                content_length = int(self.headers.get('Content-Length', 0))
                data = json.loads(self.rfile.read(content_length).decode('utf-8'))
                self.send_json(self.db.change_password(
                    user["id"], data.get("current_password", ""), data.get("new_password", "")
                ))
            except Exception as e:
                self.send_json({"success": False, "error": str(e)}, status=400)

        elif path == "/api/touch":
            user = self._require_user(("admin", "sales_lead", "sales_rep"))
            if not user:
                return
            try:
                content_length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(content_length).decode('utf-8')
                data = json.loads(body)
                lead_id = int(data.get('lead_id'))
                if not self._can_access_owner(user, self.db.lead_company_owner(lead_id)):
                    self.send_json({"success": False, "error": "只能修改自己领取的客户"}, status=403)
                    return
                new_status = data.get('status', 'pending')
                notes = data.get('notes', 'Web端状态快速切换')
                channel = data.get('channel', 'wechat_direct')
                if channel not in {
                    'wechat_direct', 'phone_call', 'cold_email',
                    'linkedin_inmail', 'x_dm', 'in_person',
                }:
                    self.send_json({"success": False, "error": "不支持的触达渠道"}, status=400)
                    return

                outreach_id = data.get('outreach_id')
                if outreach_id is not None:
                    if not self.db.update_outreach(int(outreach_id), {
                        "channel": channel,
                        "status": new_status,
                        "interaction_notes": notes,
                        "pitch_angle": data.get('pitch_angle'),
                    }):
                        self.send_json({"success": False, "error": "触达记录不存在"}, status=404)
                        return
                    self.send_json({
                        "success": True, "lead_id": lead_id,
                        "outreach_id": int(outreach_id), "status": new_status,
                        "channel": channel,
                    })
                    return

                from outreach.outreach_engine import OutreachEngine
                engine = OutreachEngine(self.db)
                engine.record_touch(
                    lead_id=lead_id,
                    channel=channel,
                    status=new_status,
                    pitch_angle=data.get('pitch_angle') or 'Web控制台触达更新',
                    notes=notes
                )
                self.send_json({
                    "success": True, "lead_id": lead_id, "status": new_status,
                    "channel": channel,
                })
            except Exception as e:
                self.send_json({"success": False, "error": str(e)}, status=400)

        elif path == "/api/strategies":
            user = self._require_user()
            if not user:
                return
            try:
                content_length = int(self.headers.get('Content-Length', 0))
                data = json.loads(self.rfile.read(content_length).decode('utf-8'))
                strategy = self.db.save_filter_strategy(
                    data.get("title", ""), data.get("content", ""),
                    user.get("display_name") or user.get("username"),
                )
                self.send_json({"success": True, "strategy": strategy})
            except (ValueError, TypeError) as error:
                self.send_json({"success": False, "error": str(error)}, status=400)

        elif path == "/api/discord-communities":
            user = self._require_user()
            if not user:
                return
            try:
                content_length = int(self.headers.get('Content-Length', 0))
                data = json.loads(self.rfile.read(content_length).decode('utf-8'))
                community = self.db.save_discord_community(
                    data, user.get("display_name") or user.get("username")
                )
                self.send_json({"success": True, "community": community})
            except (ValueError, TypeError) as error:
                self.send_json({"success": False, "error": str(error)}, status=400)

        elif path == "/api/prospect/upsert":
            user = self._require_user(("admin", "sales_lead"))
            if not user:
                return
            try:
                content_length = int(self.headers.get('Content-Length', 0))
                data = json.loads(self.rfile.read(content_length).decode('utf-8'))
                company = data.get("company") or {}
                lead = data.get("lead") or {}
                touch = data.get("touch")
                if not company.get("name") or not lead.get("contact_name"):
                    self.send_json({"success": False, "error": "企业名称和联系人不能为空"}, status=400)
                    return
                company_id = self.db.insert_company(company)
                existing = self.db.find_lead(
                    company_id, lead.get("contact_name", ""), lead.get("x_url", "")
                )
                if existing:
                    lead_id = existing["id"]
                    self.db.update_lead(lead_id, {
                        key: value for key, value in lead.items()
                        if key in {"contact_name", "job_title", "contact_role_category", "phone", "wechat",
                                   "email", "linkedin_url", "x_url", "source_method",
                                   "identity_source_url", "identity_evidence",
                                   "phone_source_url", "phone_evidence", "phone_confidence",
                                   "wechat_source_url", "wechat_evidence", "wechat_confidence",
                                   "verified_at", "fit_score", "wechat_same_as_phone_confirmed"}
                    })
                else:
                    lead["company_id"] = company_id
                    lead_id = self.db.insert_lead(lead)
                outreach_id = None
                if touch:
                    touch["lead_id"] = lead_id
                    outreach_id = self.db.record_outreach(touch)
                self.send_json({
                    "success": True, "company_id": company_id,
                    "lead_id": lead_id, "outreach_id": outreach_id,
                })
            except Exception as e:
                self.send_json({"success": False, "error": str(e)}, status=400)

        elif path == "/api/company/notes":
            user = self._require_user(("admin", "sales_lead", "sales_rep"))
            if not user:
                return
            try:
                content_length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(content_length).decode('utf-8')
                data = json.loads(body)
                company_id = int(data.get('company_id'))
                if not self._can_access_owner(user, self.db.company_owner(company_id)):
                    self.send_json({"success": False, "error": "只能修改自己领取的客户"}, status=403)
                    return
                notes = data.get('notes', '')
                self.db.update_company_notes(company_id, notes)
                self.send_json({"success": True, "company_id": company_id})
            except Exception as e:
                self.send_json({"success": False, "error": str(e)}, status=400)

        elif path == "/api/company/tier":
            user = self._require_user(("admin", "sales_lead", "sales_rep"))
            if not user:
                return
            try:
                content_length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(content_length).decode('utf-8')
                data = json.loads(body)
                company_id = int(data.get('company_id'))
                if not self._can_access_owner(user, self.db.company_owner(company_id)):
                    self.send_json({"success": False, "error": "只能修改自己领取的客户"}, status=403)
                    return
                tier = data.get('tier', 'B')
                self.db.update_company_tier(company_id, tier)
                self.send_json({"success": True, "company_id": company_id, "tier": tier})
            except Exception as e:
                self.send_json({"success": False, "error": str(e)}, status=400)

        elif path == "/api/company/update":
            user = self._require_user(("admin", "sales_lead", "sales_rep"))
            if not user:
                return
            try:
                content_length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(content_length).decode('utf-8')
                data = json.loads(body)
                company_id = int(data.get('company_id'))
                updates = data.get('updates', {})
                if not self._can_access_owner(user, self.db.company_owner(company_id)):
                    self.send_json({"success": False, "error": "只能修改自己领取的客户"}, status=403)
                    return
                if user["role"] == "sales_rep":
                    updates.pop("owner", None)
                success = self.db.update_company(company_id, updates)
                self.send_json({"success": success, "company_id": company_id})
            except Exception as e:
                self.send_json({"success": False, "error": str(e)}, status=400)

        elif path == "/api/lead/update":
            user = self._require_user(("admin", "sales_lead", "sales_rep"))
            if not user:
                return
            try:
                content_length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(content_length).decode('utf-8')
                data = json.loads(body)
                lead_id = int(data.get('lead_id'))
                if not self._can_access_owner(user, self.db.lead_company_owner(lead_id)):
                    self.send_json({"success": False, "error": "只能修改自己领取的客户"}, status=403)
                    return
                updates = data.get('updates', {})
                success = self.db.update_lead(lead_id, updates)
                self.send_json({"success": success, "lead_id": lead_id})
            except Exception as e:
                self.send_json({"success": False, "error": str(e)}, status=400)

        elif path == "/api/user/role":
            user = self._require_user(("admin",))
            if not user:
                return
            try:
                content_length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(content_length).decode('utf-8')
                data = json.loads(body)
                user_id = int(data.get('user_id'))
                new_role = data.get('role', 'sales_rep')
                self.db.update_user_role(user_id, new_role)
                self.send_json({"success": True, "user_id": user_id, "role": new_role})
            except Exception as e:
                self.send_json({"success": False, "error": str(e)}, status=400)

        else:
            self.send_response(404)
            self.end_headers()

    def send_json(self, data: Any, status: int = 200, headers=None):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

def start_server():
    with socketserver.TCPServer(("", PORT), CustomHandler) as httpd:
        print(f"🚀 Aibubu 销售作战中台已在本地启动: http://localhost:{PORT}")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass

if __name__ == "__main__":
    start_server()
