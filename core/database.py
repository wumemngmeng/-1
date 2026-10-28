"""
Database models and schema definitions for Aibubu Outbound Engine.
Pure SQLite, zero external cloud dependencies, ACID compliant.
"""
import sqlite3
import os
import hashlib
import hmac
import secrets
from datetime import datetime
from typing import List, Dict, Any, Optional

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "aibubu_crm.db")

class RemoteCursor:
    def __init__(self, client):
        self.client = client
        self.rows = []
        self.index = 0
        self.lastrowid = None

    def execute(self, sql, params=()):
        result = self.client.execute(sql, tuple(params) if params else None)
        self.rows = [dict(zip(result.columns, row)) for row in result.rows]
        self.index = 0
        self.lastrowid = result.last_insert_rowid
        return self

    def executemany(self, sql, params):
        statements = [(sql, tuple(values)) for values in params]
        results = self.client.batch(statements) if statements else []
        self.rows = []
        self.index = 0
        self.lastrowid = results[-1].last_insert_rowid if results else None
        return self

    def executescript(self, script):
        statements, buffer = [], ""
        for line in script.splitlines(keepends=True):
            buffer += line
            if sqlite3.complete_statement(buffer):
                statement = buffer.strip()
                if statement:
                    statements.append(statement)
                buffer = ""
        if buffer.strip():
            statements.append(buffer.strip())
        if statements:
            self.client.batch(statements)
        self.rows = []
        self.index = 0
        return self

    def fetchone(self):
        if self.index >= len(self.rows):
            return None
        row = self.rows[self.index]
        self.index += 1
        return row

    def fetchall(self):
        rows = self.rows[self.index:]
        self.index = len(self.rows)
        return rows


class RemoteConnection:
    def __init__(self, url, auth_token):
        from libsql_client import create_client_sync
        http_url = url.replace("libsql://", "https://", 1)
        self.client = create_client_sync(http_url, auth_token=auth_token or None)
        self.row_factory = None

    def cursor(self):
        return RemoteCursor(self.client)

    def execute(self, sql, params=()):
        return self.cursor().execute(sql, params)

    def commit(self):
        # HTTPS execute autocommits; batch commits its statements atomically.
        return None

    def close(self):
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()


def hash_password(password: str) -> str:
    """Hash a password with a random salt and PBKDF2."""
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 310_000).hex()
    return f"pbkdf2_sha256$310000${salt}${digest}"


def verify_password(password: str, encoded: str) -> bool:
    if encoded.startswith("pbkdf2_sha256$"):
        _, iterations, salt, digest = encoded.split("$", 3)
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt), int(iterations)
        ).hex()
        return hmac.compare_digest(actual, digest)
    legacy = hashlib.sha256(("aibubu_salt_2026" + password).encode("utf-8")).hexdigest()
    return hmac.compare_digest(legacy, encoded)

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

-- 1. 企业主体库 (Company Pool) - 只存企业基础信息与画像
CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    country_type TEXT CHECK(country_type IN ('domestic', 'overseas')) NOT NULL,
    industry TEXT NOT NULL,
    scale TEXT,
    website TEXT,
    city_or_region TEXT,
    source_channel TEXT NOT NULL,
    annual_revenue_range TEXT,
    client_tier TEXT CHECK(client_tier IN ('S', 'A', 'B', 'C')) DEFAULT 'B',
    has_image_heavy_demand INTEGER DEFAULT 1,
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 2. 真实决策人线索库 (Lead & Contact Pool)
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL,
    contact_name TEXT NOT NULL,
    job_title TEXT NOT NULL,
    contact_role_category TEXT,
    phone TEXT,
    wechat TEXT,
    email TEXT,
    linkedin_url TEXT,
    x_url TEXT,
    confidence_score INTEGER DEFAULT 80,
    source_method TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE CASCADE
);

-- 3. 销售触达与推进记录 (Outreach & Pipeline)
CREATE TABLE IF NOT EXISTS outreach_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER NOT NULL,
    channel TEXT CHECK(channel IN ('wechat_direct', 'phone_call', 'cold_email', 'linkedin_inmail', 'in_person')) NOT NULL,
    status TEXT CHECK(status IN ('pending', 'contacted', 'sample_sent', 'team_trial', 'negotiation', 'won', 'lost')) DEFAULT 'pending',
    pitch_angle TEXT,
    interaction_notes TEXT,
    next_follow_up_date DATE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (lead_id) REFERENCES leads(id) ON DELETE CASCADE
);

-- 4. 系统用户与角色权限库 (Users & Permissions)
CREATE TABLE IF NOT EXISTS sys_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL DEFAULT '',
    display_name TEXT NOT NULL,
    role TEXT CHECK(role IN ('admin', 'sales_lead', 'sales_rep', 'observer')) NOT NULL DEFAULT 'sales_rep',
    email TEXT,
    phone TEXT,
    status TEXT DEFAULT 'active',
    must_change_password INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS filter_strategies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    version INTEGER NOT NULL,
    content TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(title, version)
);
CREATE INDEX IF NOT EXISTS idx_filter_strategies_updated
    ON filter_strategies(created_at DESC);

CREATE TABLE IF NOT EXISTS discord_communities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    invite_url TEXT NOT NULL,
    focus TEXT NOT NULL,
    language TEXT CHECK(language IN ('中文', '英文', '中英双语')) NOT NULL DEFAULT '英文',
    audience TEXT,
    fit_level TEXT CHECK(fit_level IN ('高', '中', '低')) NOT NULL DEFAULT '中',
    priority INTEGER NOT NULL DEFAULT 99,
    join_status TEXT CHECK(join_status IN ('已加入', '待加入', '观察中', '暂停')) NOT NULL DEFAULT '已加入',
    account_type TEXT CHECK(account_type IN ('个人号', '官方号')) NOT NULL DEFAULT '个人号',
    recommended_channels TEXT,
    notes TEXT,
    added_by TEXT NOT NULL DEFAULT '系统导入',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_discord_communities_priority
    ON discord_communities(priority, name);

CREATE INDEX IF NOT EXISTS idx_companies_country ON companies(country_type);
CREATE INDEX IF NOT EXISTS idx_companies_industry ON companies(industry);
CREATE INDEX IF NOT EXISTS idx_leads_company ON leads(company_id);
CREATE INDEX IF NOT EXISTS idx_outreach_status ON outreach_records(status);
"""

def get_connection(db_path: str = DB_PATH) -> sqlite3.Connection:
    remote_url = os.environ.get("TURSO_DATABASE_URL", "").strip()
    remote_token = os.environ.get("TURSO_AUTH_TOKEN", "").strip()
    if remote_url:
        return RemoteConnection(remote_url, remote_token)
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def bootstrap_remote_from_sqlite(conn: sqlite3.Connection, source_path: str = DB_PATH):
    """Seed a new Turso database from the tracked CRM snapshot exactly once."""
    if not os.environ.get("TURSO_DATABASE_URL") or not os.path.exists(source_path):
        return
    cur = conn.cursor()
    cur.execute("SELECT count(*) AS count FROM companies")
    if cur.fetchone()["count"]:
        return
    source = sqlite3.connect(source_path)
    source.row_factory = sqlite3.Row
    try:
        statements = []
        for table in ("companies", "leads", "outreach_records", "sys_users"):
            columns = [r["name"] for r in source.execute(f"PRAGMA table_info({table})")]
            rows = source.execute(
                f"SELECT {', '.join(columns)} FROM {table} ORDER BY id"
            ).fetchall()
            if not rows:
                continue
            placeholders = ", ".join("?" for _ in columns)
            sql = (
                f"INSERT OR IGNORE INTO {table} ({', '.join(columns)}) "
                f"VALUES ({placeholders})"
            )
            statements.extend(
                (sql, tuple(row[column] for column in columns)) for row in rows
            )
        if isinstance(conn, RemoteConnection):
            if statements:
                conn.client.batch(statements)
        else:
            for sql, params in statements:
                cur.execute(sql, params)
        conn.commit()
    finally:
        source.close()

def init_db(db_path: str = DB_PATH):
    conn = get_connection(db_path)
    cursor = conn.cursor()
    cursor.executescript(SCHEMA_SQL)
    
    # Auto migrate sys_users table if password_hash column is missing
    cursor.execute("PRAGMA table_info(sys_users)")
    cols = [r['name'] for r in cursor.fetchall()]
    if 'password_hash' not in cols:
        cursor.execute("ALTER TABLE sys_users ADD COLUMN password_hash TEXT NOT NULL DEFAULT ''")
    if 'must_change_password' not in cols:
        cursor.execute("ALTER TABLE sys_users ADD COLUMN must_change_password INTEGER DEFAULT 1")
    
    # Auto migrate companies table if client_tier column is missing
    cursor.execute("PRAGMA table_info(companies)")
    c_cols = [r['name'] for r in cursor.fetchall()]
    if 'client_tier' not in c_cols:
        cursor.execute("ALTER TABLE companies ADD COLUMN client_tier TEXT CHECK(client_tier IN ('S', 'A', 'B', 'C')) DEFAULT 'B'")

    cursor.execute("PRAGMA table_info(leads)")
    l_cols = [r['name'] for r in cursor.fetchall()]
    if 'x_url' not in l_cols:
        cursor.execute("ALTER TABLE leads ADD COLUMN x_url TEXT")
    
    # Estimated monthly Aibubu spend, CNY; unknown unless supported by evidence.
    for field, definition in [('company_description', 'TEXT'), ('description_source', 'TEXT'), ('estimated_monthly_spend', 'REAL'), ('spend_basis', 'TEXT'), ('discovery_url', 'TEXT'), ('customer_type', 'TEXT'), ('models_used', 'TEXT'), ('qualification_evidence', 'TEXT'), ('owner', 'TEXT'), ('company_follow_up_date', 'TEXT'), ('prospect_status', 'TEXT'), ('audience_scale', 'TEXT'), ('exclusion_reason', 'TEXT'), ('previous_prospect_status', 'TEXT')]:
        if field not in c_cols:
            cursor.execute(f"ALTER TABLE companies ADD COLUMN {field} {definition}")

    bootstrap_remote_from_sqlite(conn)

    # Seed default users
    cursor.execute("SELECT count(*) as count FROM sys_users")
    if cursor.fetchone()['count'] == 0:
        cursor.execute("""
            INSERT INTO sys_users (username, password_hash, display_name, role, email, phone)
            VALUES 
                ('admin', ?, '中台超级管理员', 'admin', 'admin@aibubu.com', '18888888888'),
                ('sales_leader', ?, '销售总监 (华东/华南)', 'sales_lead', 'leader@aibubu.com', '13900000001'),
                ('sales_01', ?, '国内客户专员-小张', 'sales_rep', 'zhang@aibubu.com', '13900000002'),
                ('sales_global', ?, '海外拓展专员-Alex', 'sales_rep', 'alex@aibubu.com', '13900000003'),
                ('observer', ?, '运营/只读访客', 'observer', 'guest@aibubu.com', '13900000004')
        """, (
            hash_password("admin123"),
            hash_password("123456"),
            hash_password("123456"),
            hash_password("123456"),
            hash_password("123456")
        ))
    else:
        # Ensure default users have passwords
        cursor.execute("SELECT id, username, password_hash FROM sys_users")
        for u in cursor.fetchall():
            if not u['password_hash']:
                pwd = "admin123" if u['username'] == 'admin' else "123456"
                cursor.execute("UPDATE sys_users SET password_hash = ? WHERE id = ?", (hash_password(pwd), u['id']))

    cursor.execute("SELECT count(*) AS count FROM discord_communities")
    if cursor.fetchone()["count"] == 0:
        communities = [
            ("Leonardo.Ai", "https://discord.gg/leonardo-ai", "AI 图像 / 视频", "英文", "营销、设计与 AI 创作者", "高", 1, "#showcase / #feedback / #tools", "与 Aibubu 产品和用户画像最接近"),
            ("Higgsfield AI", "https://discord.gg/higgsfield", "AI 图像 / 视频 / 广告", "英文", "广告营销与多模型创作者", "高", 2, "#showcase / Marketing Studio", "重点观察广告生产工作流"),
            ("Runway", "https://discord.gg/runwayml", "AI 视频", "英文", "专业视频创作者", "高", 3, "#showcase / #feedback", "适合收集视频工作流和痛点"),
            ("Pika", "https://discord.com/invite/pika", "AI 短视频", "英文", "短视频与社媒创作者", "高", 4, "#showcase / #feedback", "偏短视频和社媒内容"),
            ("invideo AI", "https://discord.com/invite/invideo-ai", "营销视频", "英文", "营销视频生产者", "高", 5, "#showcase / #tools", "接近 Aibubu 付费用户画像"),
            ("Kling AI", "https://discord.com/invite/8tj8YjSzKr", "AI 视频", "中英双语", "视频生成用户", "中", 6, "#showcase / #feedback", "观察视频生成需求"),
            ("Vidu AI", "https://discord.com/invite/vidu-ai-1257891059510280232", "AI 视频", "中英双语", "视频生成用户", "中", 7, "#showcase / #feedback", "官方视频生成社区"),
            ("Stable Diffusion", "https://discord.gg/stablediffusion", "AI 图像 / 模型", "英文", "硬核出图与模型用户", "中", 8, "#showcase / #help", "适合听需求，不适合直接推销"),
            ("Midjourney", "https://discord.gg/midjourney", "AI 图像", "英文", "大规模 AI 图像创作者", "中", 9, "#showcase", "以观察为主，避免产品推广"),
            ("Midjourney Creators Hub", "https://discord.gg/YaYbhVZjCN", "AI 图像", "英文", "AI 创作者互助群", "中", 10, "#showcase / #feedback", "社区较小，互动门槛较低"),
            ("Civitai", "https://discord.gg/civitai", "AI 图像 / 模型", "英文", "模型与出图用户", "中", 11, "#showcase / #tools", "适合了解真实工作流"),
            ("OpenArt AI", "https://discord.gg/yTMNvk5z97", "综合 AI 创作", "英文", "综合 AI 创作者", "中", 12, "#showcase / #feedback", "大型综合创作平台社区"),
            ("Luma AI", "https://discord.gg/ASbS3EykXm", "AI 视频", "英文", "视频创作者", "中", 13, "#showcase / #feedback", "AI 视频方向"),
            ("PixAI", "https://discord.gg/jJtE3gNnxm", "AI 图像 / 二次元", "中英双语", "二次元出图用户", "中", 14, "#showcase", "偏二次元创作"),
            ("AIGC", "https://discord.gg/AIGC", "综合 AI 创作", "中文", "中文 AIGC 创作者", "高", 15, "#作品展示 / #工具", "中文社区优先观察"),
            ("Ai 绘画中文组", "https://discord.gg/vhsArSSA6K", "AI 图像", "中文", "中文 AI 绘画用户", "高", 16, "#作品展示 / #交流", "中文绘画向社区"),
            ("鳥巢 AI 藝術學園", "https://discord.gg/YKddUZnwan", "AI 艺术", "中文", "中文 AI 艺术创作者", "高", 17, "#作品展示 / #交流", "中文 AI 艺术社区"),
            ("Indie Hackers", "https://discord.gg/d3R5ynFMgr", "独立开发 / 增长", "英文", "独立开发者与创业者", "低", 18, "#growth / #marketing", "不直接是客户，适合学习获客"),
        ]
        cursor.executemany(
            """INSERT OR IGNORE INTO discord_communities
               (name, invite_url, focus, language, audience, fit_level, priority,
                recommended_channels, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            communities,
        )

    conn.commit()
    conn.close()

class DatabaseManager:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        init_db(self.db_path)

    def insert_company(self, data: Dict[str, Any]) -> int:
        conn = get_connection(self.db_path)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO companies (name, country_type, industry, scale, website, city_or_region, source_channel, annual_revenue_range, client_tier, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                country_type=excluded.country_type,
                industry=excluded.industry,
                scale=excluded.scale,
                website=coalesce(excluded.website, companies.website),
                city_or_region=coalesce(excluded.city_or_region, companies.city_or_region),
                source_channel=excluded.source_channel,
                client_tier=coalesce(excluded.client_tier, companies.client_tier),
                updated_at=CURRENT_TIMESTAMP
        """, (
            data['name'],
            data.get('country_type', 'domestic'),
            data.get('industry', '未知赛道'),
            data.get('scale', '50-150人'),
            data.get('website', ''),
            data.get('city_or_region', ''),
            data.get('source_channel', '手动录入/榜单抓取'),
            data.get('annual_revenue_range', ''),
            data.get('client_tier', 'B'),
            data.get('notes', '')
        ))
        conn.commit()
        cur.execute("SELECT id FROM companies WHERE name = ?", (data['name'],))
        row = cur.fetchone()
        company_id = row['id']
        conn.close()
        return company_id

    def insert_lead(self, data: Dict[str, Any]) -> int:
        conn = get_connection(self.db_path)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO leads (company_id, contact_name, job_title, contact_role_category, phone, wechat, email, linkedin_url, x_url, confidence_score, source_method)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            data['company_id'],
            data['contact_name'],
            data.get('job_title', '业务决策人'),
            data.get('contact_role_category', '业务决策人'),
            data.get('phone', ''),
            data.get('wechat', ''),
            data.get('email', ''),
            data.get('linkedin_url', ''),
            data.get('x_url', ''),
            data.get('confidence_score', 85),
            data.get('source_method', '商务渠道')
        ))
        conn.commit()
        lead_id = cur.lastrowid
        conn.close()
        return lead_id

    def find_lead(self, company_id: int, contact_name: str = "", x_url: str = "") -> Optional[Dict[str, Any]]:
        conn = get_connection(self.db_path)
        cur = conn.cursor()
        if x_url:
            cur.execute(
                "SELECT * FROM leads WHERE company_id = ? AND lower(x_url) = lower(?) ORDER BY id LIMIT 1",
                (company_id, x_url),
            )
        else:
            cur.execute(
                "SELECT * FROM leads WHERE company_id = ? AND lower(contact_name) = lower(?) ORDER BY id LIMIT 1",
                (company_id, contact_name),
            )
        row = cur.fetchone()
        conn.close()
        return dict(row) if row else None

    def record_outreach(self, data: Dict[str, Any]) -> int:
        conn = get_connection(self.db_path)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO outreach_records (lead_id, channel, status, pitch_angle, interaction_notes, next_follow_up_date)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            data['lead_id'],
            data.get('channel', 'wechat_direct'),
            data.get('status', 'pending'),
            data.get('pitch_angle', ''),
            data.get('interaction_notes', ''),
            data.get('next_follow_up_date', None)
        ))
        conn.commit()
        record_id = cur.lastrowid
        conn.close()
        return record_id

    def update_company_notes(self, company_id: int, notes: str) -> bool:
        conn = get_connection(self.db_path)
        cur = conn.cursor()
        cur.execute("UPDATE companies SET notes = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (notes, company_id))
        conn.commit()
        conn.close()
        return True

    def update_company_tier(self, company_id: int, tier: str) -> bool:
        if tier not in ('S', 'A', 'B', 'C'):
            return False
        conn = get_connection(self.db_path)
        cur = conn.cursor()
        cur.execute("UPDATE companies SET client_tier = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (tier, company_id))
        conn.commit()
        conn.close()
        return True

    def get_data_last_updated(
        self, country_type: Optional[str] = None, owner: Optional[str] = None
    ) -> Optional[str]:
        """Return the latest CRM write timestamp within the visible customer scope."""
        conn = get_connection(self.db_path)
        cur = conn.cursor()
        conditions = []
        params: List[Any] = []
        if country_type:
            conditions.append("c.country_type = ?")
            params.append(country_type)
        if owner is not None:
            conditions.append("coalesce(c.owner, '') = ?")
            params.append(owner)
        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        query = f"""
            SELECT MAX(updated_at) AS last_updated FROM (
                SELECT c.updated_at AS updated_at FROM companies c{where}
                UNION ALL
                SELECT l.updated_at AS updated_at
                FROM leads l JOIN companies c ON c.id = l.company_id{where}
                UNION ALL
                SELECT r.updated_at AS updated_at
                FROM outreach_records r
                JOIN leads l ON l.id = r.lead_id
                JOIN companies c ON c.id = l.company_id{where}
            )
        """
        cur.execute(query, params * 3)
        row = cur.fetchone()
        conn.close()
        return row["last_updated"] if row else None

    def query_pipeline(self, country_type: Optional[str] = None, status: Optional[str] = None, source_channel: Optional[str] = None, client_tier: Optional[str] = None, owner: Optional[str] = None) -> List[Dict[str, Any]]:
        conn = get_connection(self.db_path)
        cur = conn.cursor()
        query = """
            SELECT 
                c.id as company_id,
                c.name as company_name,
                c.estimated_monthly_spend, c.spend_basis, c.discovery_url,
                c.customer_type, c.models_used, c.qualification_evidence, c.owner, c.company_follow_up_date, c.prospect_status, c.audience_scale, c.exclusion_reason, c.previous_prospect_status, c.created_at as first_discovered,
                c.country_type,
                c.industry,
                c.scale,
                c.website,
                c.city_or_region,
                c.source_channel,
                c.annual_revenue_range,
                coalesce(c.client_tier, 'B') as client_tier,
                c.notes as company_notes,
                c.company_description,
                c.description_source,
                l.id as lead_id,
                l.contact_name,
                l.job_title,
                l.phone,
                l.wechat,
                l.email,
                l.linkedin_url,
                l.x_url,
                l.source_method as lead_source,
                o.id as outreach_id,
                coalesce(o.status, 'pending') as status,
                o.channel,
                o.pitch_angle,
                o.interaction_notes,
                o.next_follow_up_date,
                max(
                    c.updated_at,
                    coalesce((SELECT MAX(l3.updated_at) FROM leads l3 WHERE l3.company_id = c.id), c.updated_at),
                    coalesce((
                        SELECT MAX(r3.updated_at)
                        FROM outreach_records r3
                        JOIN leads l4 ON l4.id = r3.lead_id
                        WHERE l4.company_id = c.id
                    ), c.updated_at)
                ) as data_updated_at
            FROM companies c
            LEFT JOIN leads l ON l.id = (
                SELECT l2.id
                FROM leads l2
                WHERE l2.company_id = c.id
                ORDER BY
                    CASE WHEN l2.x_url IS NOT NULL AND l2.x_url != '' THEN 0 ELSE 1 END,
                    l2.id DESC
                LIMIT 1
            )
            LEFT JOIN (
                SELECT r.* FROM outreach_records r
                INNER JOIN (
                    SELECT lead_id, MAX(id) as max_id FROM outreach_records GROUP BY lead_id
                ) m ON r.id = m.max_id
            ) o ON l.id = o.lead_id
            WHERE 1=1
        """
        params = []
        if country_type:
            query += " AND c.country_type = ?"
            params.append(country_type)
        if status:
            query += " AND coalesce(o.status, 'pending') = ?"
            params.append(status)
        if source_channel:
            query += " AND c.source_channel = ?"
            params.append(source_channel)
        if client_tier:
            query += " AND coalesce(c.client_tier, 'B') = ?"
            params.append(client_tier)
        if owner is not None:
            query += " AND coalesce(c.owner, '') = ?"
            params.append(owner)
        query += " ORDER BY data_updated_at DESC, c.id DESC, l.id DESC"
        
        cur.execute(query, params)
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows

    def get_users(self) -> List[Dict[str, Any]]:
        conn = get_connection(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT id, username, display_name, role, email, phone, status, created_at FROM sys_users ORDER BY id ASC")
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows

    def get_user(self, user_id: int) -> Optional[Dict[str, Any]]:
        conn = get_connection(self.db_path)
        row = conn.execute(
            "SELECT id, username, display_name, role, email, phone, status, must_change_password, created_at "
            "FROM sys_users WHERE id = ? AND status = 'active'",
            (user_id,),
        ).fetchone()
        conn.close()
        return dict(row) if row else None

    def authenticate_user(self, username: str, password: str) -> Optional[Dict[str, Any]]:
        conn = get_connection(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT * FROM sys_users WHERE username = ? AND status = 'active'", (username.strip(),))
        row = cur.fetchone()
        if row and verify_password(password, row["password_hash"]):
            if not row["password_hash"].startswith("pbkdf2_sha256$"):
                cur.execute("UPDATE sys_users SET password_hash = ? WHERE id = ?", (hash_password(password), row["id"]))
                conn.commit()
            user = {key: row[key] for key in (
                "id", "username", "display_name", "role", "email", "phone",
                "status", "must_change_password", "created_at"
            )}
        else:
            user = None
        conn.close()
        return user

    def register_user(self, username: str, password: str, display_name: str, role: str = 'sales_rep', email: str = '', phone: str = '') -> Dict[str, Any]:
        username = username.strip()
        if not username or not password:
            return {"success": False, "error": "用户名和密码不能为空"}
        if len(password) < 8:
            return {"success": False, "error": "密码至少需要 8 位"}
        if role not in ("admin", "sales_lead", "sales_rep", "observer"):
            return {"success": False, "error": "无效的账号角色"}
        conn = get_connection(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT id FROM sys_users WHERE username = ?", (username,))
        if cur.fetchone():
            conn.close()
            return {"success": False, "error": f"用户名 '{username}' 已被注册"}
        
        pwd_hash = hash_password(password)
        cur.execute("""
            INSERT INTO sys_users (username, password_hash, display_name, role, email, phone)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (username, pwd_hash, display_name.strip() or username, role, email.strip(), phone.strip()))
        conn.commit()
        new_id = cur.lastrowid
        cur.execute("SELECT id, username, display_name, role, email, phone, status, created_at FROM sys_users WHERE id = ?", (new_id,))
        user = dict(cur.fetchone())
        conn.close()
        return {"success": True, "user": user}

    def update_user_role(self, user_id: int, role: str) -> bool:
        if role not in ("admin", "sales_lead", "sales_rep", "observer"):
            return False
        conn = get_connection(self.db_path)
        cur = conn.cursor()
        cur.execute("UPDATE sys_users SET role = ? WHERE id = ?", (role, user_id))
        conn.commit()
        conn.close()
        return True

    def change_password(self, user_id: int, current_password: str, new_password: str) -> Dict[str, Any]:
        if len(new_password) < 10:
            return {"success": False, "error": "新密码至少需要 10 位"}
        conn = get_connection(self.db_path)
        row = conn.execute(
            "SELECT password_hash FROM sys_users WHERE id = ? AND status = 'active'",
            (user_id,),
        ).fetchone()
        if not row or not verify_password(current_password, row["password_hash"]):
            conn.close()
            return {"success": False, "error": "当前密码不正确"}
        conn.execute(
            "UPDATE sys_users SET password_hash = ?, must_change_password = 0 WHERE id = ?",
            (hash_password(new_password), user_id),
        )
        conn.commit()
        conn.close()
        return {"success": True}

    def company_owner(self, company_id: int) -> Optional[str]:
        conn = get_connection(self.db_path)
        row = conn.execute("SELECT owner FROM companies WHERE id = ?", (company_id,)).fetchone()
        conn.close()
        return None if row is None else (row["owner"] or "")

    def lead_company_owner(self, lead_id: int) -> Optional[str]:
        conn = get_connection(self.db_path)
        row = conn.execute(
            "SELECT c.owner FROM leads l JOIN companies c ON c.id = l.company_id WHERE l.id = ?",
            (lead_id,),
        ).fetchone()
        conn.close()
        return None if row is None else (row["owner"] or "")

    def get_source_channels(self) -> List[str]:
        conn = get_connection(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT source_channel FROM companies WHERE source_channel IS NOT NULL AND source_channel != '' ORDER BY source_channel ASC")
        rows = [r['source_channel'] for r in cur.fetchall()]
        conn.close()
        return rows

    def get_lead_sources(self) -> List[str]:
        conn = get_connection(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT source_method FROM leads WHERE source_method IS NOT NULL AND source_method != '' ORDER BY source_method ASC")
        rows = [r['source_method'] for r in cur.fetchall()]
        conn.close()
        return rows

    def update_company(self, company_id: int, updates: Dict[str, Any]) -> bool:
        allowed = {
            'name', 'country_type', 'industry', 'scale', 'website',
            'city_or_region', 'source_channel', 'annual_revenue_range',
            'client_tier', 'has_image_heavy_demand', 'notes',
            'company_description', 'description_source',
            'estimated_monthly_spend', 'spend_basis', 'discovery_url',
            'customer_type', 'models_used', 'qualification_evidence', 'owner', 'company_follow_up_date', 'prospect_status', 'audience_scale', 'exclusion_reason', 'previous_prospect_status'
        }
        valid_updates = {k: v for k, v in updates.items() if k in allowed}
        if not valid_updates:
            return False
        
        conn = get_connection(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT prospect_status FROM companies WHERE id = ?", (company_id,))
        existing = cur.fetchone()
        if existing is None:
            conn.close()
            return False
        if valid_updates.get('prospect_status') == 'lost' and existing['prospect_status'] != 'lost':
            reason = str(valid_updates.get('exclusion_reason') or '').strip()
            if not reason:
                conn.close()
                raise ValueError('请填写剔除原因')
            valid_updates['exclusion_reason'] = reason
            valid_updates['previous_prospect_status'] = existing['prospect_status'] or 'pending'
        clauses = [f"{k} = ?" for k in valid_updates.keys()]
        clauses.append("updated_at = CURRENT_TIMESTAMP")
        values = list(valid_updates.values())
        values.append(company_id)
        sql = f"UPDATE companies SET {', '.join(clauses)} WHERE id = ?"
        cur.execute(sql, tuple(values))
        conn.commit()
        conn.close()
        return True

    def update_lead(self, lead_id: int, updates: Dict[str, Any]) -> bool:
        allowed = {
            'contact_name', 'job_title', 'contact_role_category',
            'phone', 'wechat', 'email', 'linkedin_url',
            'x_url',
            'confidence_score', 'source_method'
        }
        valid_updates = {k: v for k, v in updates.items() if k in allowed}
        if not valid_updates:
            return False
        
        conn = get_connection(self.db_path)
        cur = conn.cursor()
        clauses = [f"{k} = ?" for k in valid_updates.keys()]
        clauses.append("updated_at = CURRENT_TIMESTAMP")
        values = list(valid_updates.values())
        values.append(lead_id)
        sql = f"UPDATE leads SET {', '.join(clauses)} WHERE id = ?"
        cur.execute(sql, tuple(values))
        conn.commit()
        conn.close()
        return True

    def get_stats(self, country_type: Optional[str] = None) -> Dict[str, Any]:
        conn = get_connection(self.db_path)
        cur = conn.cursor()
        stats = {}
        
        country_filter_co = " WHERE country_type = ?" if country_type else ""
        country_param_co = (country_type,) if country_type else ()
        
        cur.execute(f"SELECT count(*) as total_companies FROM companies{country_filter_co}", country_param_co)
        stats['total_companies'] = cur.fetchone()['total_companies']
        
        cur.execute("SELECT country_type, count(*) as count FROM companies GROUP BY country_type")
        stats['companies_by_region'] = {r['country_type']: r['count'] for r in cur.fetchall()}

        if country_type:
            cur.execute("""
                SELECT count(l.id) as total_leads 
                FROM leads l 
                JOIN companies c ON l.company_id = c.id 
                WHERE c.country_type = ?
            """, (country_type,))
        else:
            cur.execute("SELECT count(*) as total_leads FROM leads")
        stats['total_leads'] = cur.fetchone()['total_leads']

        cur.execute(f"SELECT coalesce(client_tier, 'B') as tier, count(*) as count FROM companies{country_filter_co} GROUP BY tier", country_param_co)
        stats['companies_by_tier'] = {r['tier']: r['count'] for r in cur.fetchall()}

        if country_type:
            cur.execute("""
                SELECT coalesce(o.status, 'pending') as status, count(*) as count 
                FROM leads l 
                JOIN companies c ON l.company_id = c.id
                LEFT JOIN (
                    SELECT lead_id, status FROM outreach_records GROUP BY lead_id HAVING id=MAX(id)
                ) o ON l.id=o.lead_id 
                WHERE c.country_type = ?
                GROUP BY coalesce(o.status, 'pending')
            """, (country_type,))
        else:
            cur.execute("""
                SELECT coalesce(status, 'pending') as status, count(*) as count 
                FROM leads l 
                LEFT JOIN (
                    SELECT lead_id, status FROM outreach_records GROUP BY lead_id HAVING id=MAX(id)
                ) o ON l.id=o.lead_id 
                GROUP BY coalesce(status, 'pending')
            """)
        stats['outreach_funnel'] = {r['status']: r['count'] for r in cur.fetchall()}
        conn.close()
        return stats

    def get_yesterday_outreach_stats(
        self, start_utc: str, end_utc: str, owner: Optional[str] = None,
        country_type: Optional[str] = None,
    ) -> Dict[str, int]:
        """Count outreach actions created during yesterday in the requested timezone."""
        conn = get_connection(self.db_path)
        cur = conn.cursor()
        query = """
            SELECT r.status, COUNT(*) AS count
            FROM outreach_records r
            JOIN leads l ON l.id = r.lead_id
            JOIN companies c ON c.id = l.company_id
            WHERE r.created_at >= ? AND r.created_at < ?
        """
        params = [start_utc, end_utc]
        if owner is not None:
            query += " AND coalesce(c.owner, '') = ?"
            params.append(owner)
        if country_type:
            query += " AND c.country_type = ?"
            params.append(country_type)
        query += " GROUP BY r.status"
        cur.execute(query, params)
        result = {row["status"]: row["count"] for row in cur.fetchall()}
        conn.close()
        return result

    def get_daily_outreach_stats(
        self, start_utc: str, end_utc: str, owner: Optional[str] = None,
        country_type: Optional[str] = None, client_tier: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return Beijing-calendar-day outreach action counts for a UTC interval."""
        conn = get_connection(self.db_path)
        cur = conn.cursor()
        query = """
            SELECT date(datetime(r.created_at, '+8 hours')) AS activity_date,
                   r.status, COUNT(*) AS count
            FROM outreach_records r
            JOIN leads l ON l.id = r.lead_id
            JOIN companies c ON c.id = l.company_id
            WHERE r.created_at >= ? AND r.created_at < ?
        """
        params = [start_utc, end_utc]
        if owner is not None:
            query += " AND coalesce(c.owner, '') = ?"
            params.append(owner)
        if country_type:
            query += " AND c.country_type = ?"
            params.append(country_type)
        if client_tier:
            query += " AND coalesce(c.client_tier, 'B') = ?"
            params.append(client_tier)
        query += " GROUP BY activity_date, r.status ORDER BY activity_date DESC"
        cur.execute(query, params)
        rows = [dict(row) for row in cur.fetchall()]
        conn.close()
        return rows

    def list_filter_strategies(
        self, start_date: Optional[str] = None, end_date: Optional[str] = None,
        version: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        conn = get_connection(self.db_path)
        query = "SELECT id, title, version, content, created_by, created_at FROM filter_strategies WHERE 1=1"
        params = []
        if start_date:
            query += " AND date(created_at) >= date(?)"
            params.append(start_date)
        if end_date:
            query += " AND date(created_at) <= date(?)"
            params.append(end_date)
        if version is not None:
            query += " AND version = ?"
            params.append(version)
        query += " ORDER BY created_at DESC, title COLLATE NOCASE, version DESC"
        rows = [dict(row) for row in conn.execute(query, params).fetchall()]
        conn.close()
        return rows

    def save_filter_strategy(self, title: str, content: str, created_by: str) -> Dict[str, Any]:
        title = title.strip()
        if not title or not content.strip():
            raise ValueError("策略名称和文本内容不能为空")
        conn = get_connection(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT coalesce(MAX(version), 0) + 1 AS next_version FROM filter_strategies WHERE title = ?", (title,))
        version = cur.fetchone()["next_version"]
        cur.execute(
            "INSERT INTO filter_strategies (title, version, content, created_by) VALUES (?, ?, ?, ?)",
            (title, version, content, created_by),
        )
        strategy_id = cur.lastrowid
        row = cur.execute(
            "SELECT id, title, version, content, created_by, created_at FROM filter_strategies WHERE id = ?",
            (strategy_id,),
        ).fetchone()
        conn.commit()
        result = dict(row)
        conn.close()
        return result

    def list_discord_communities(
        self, language: Optional[str] = None, fit_level: Optional[str] = None,
        join_status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        conn = get_connection(self.db_path)
        query = "SELECT * FROM discord_communities WHERE 1=1"
        params = []
        for field, value in (
            ("language", language), ("fit_level", fit_level), ("join_status", join_status)
        ):
            if value:
                query += f" AND {field} = ?"
                params.append(value)
        query += " ORDER BY priority, name COLLATE NOCASE"
        rows = [dict(row) for row in conn.execute(query, params).fetchall()]
        conn.close()
        return rows

    def save_discord_community(self, data: Dict[str, Any], updated_by: str) -> Dict[str, Any]:
        name = (data.get("name") or "").strip()
        invite_url = (data.get("invite_url") or "").strip()
        if not name or not invite_url:
            raise ValueError("社群名称和邀请链接不能为空")
        conn = get_connection(self.db_path)
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO discord_communities
               (name, invite_url, focus, language, audience, fit_level, priority,
                join_status, account_type, recommended_channels, notes, added_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET
                 invite_url=excluded.invite_url, focus=excluded.focus,
                 language=excluded.language, audience=excluded.audience,
                 fit_level=excluded.fit_level, priority=excluded.priority,
                 join_status=excluded.join_status, account_type=excluded.account_type,
                 recommended_channels=excluded.recommended_channels,
                 notes=excluded.notes, added_by=excluded.added_by,
                 updated_at=CURRENT_TIMESTAMP""",
            (
                name, invite_url, (data.get("focus") or "综合 AI 创作").strip(),
                data.get("language") or "英文", (data.get("audience") or "").strip(),
                data.get("fit_level") or "中", int(data.get("priority") or 99),
                data.get("join_status") or "已加入", data.get("account_type") or "个人号",
                (data.get("recommended_channels") or "").strip(),
                (data.get("notes") or "").strip(), updated_by,
            ),
        )
        row = cur.execute("SELECT * FROM discord_communities WHERE name = ?", (name,)).fetchone()
        conn.commit()
        result = dict(row)
        conn.close()
        return result
