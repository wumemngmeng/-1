"""
Database models and schema definitions for Aibubu Outbound Engine.
Pure SQLite, zero external cloud dependencies, ACID compliant.
"""
import sqlite3
import os
import hashlib
from datetime import datetime
from typing import List, Dict, Any, Optional

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "aibubu_crm.db")

def hash_password(password: str) -> str:
    """Hash password with SHA256 and salt for secure local auth."""
    salt = "aibubu_salt_2026"
    return hashlib.sha256((salt + password).encode('utf-8')).hexdigest()

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
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_companies_country ON companies(country_type);
CREATE INDEX IF NOT EXISTS idx_companies_industry ON companies(industry);
CREATE INDEX IF NOT EXISTS idx_leads_company ON leads(company_id);
CREATE INDEX IF NOT EXISTS idx_outreach_status ON outreach_records(status);
"""

def get_connection(db_path: str = DB_PATH) -> sqlite3.Connection:
    remote_url = os.environ.get("TURSO_DATABASE_URL", "").strip()
    remote_token = os.environ.get("TURSO_AUTH_TOKEN", "").strip()
    if remote_url:
        from libsql_client import dbapi2 as libsql_dbapi
        conn = libsql_dbapi.connect(
            remote_url,
            uri=True,
            auth_token=remote_token or None,
            check_same_thread=False,
        )
        conn.row_factory = libsql_dbapi.Row
        return conn
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
        for table in ("companies", "leads", "outreach_records", "sys_users"):
            columns = [r["name"] for r in source.execute(f"PRAGMA table_info({table})")]
            rows = source.execute(
                f"SELECT {', '.join(columns)} FROM {table} ORDER BY id"
            ).fetchall()
            if not rows:
                continue
            placeholders = ", ".join("?" for _ in columns)
            cur.executemany(
                f"INSERT OR IGNORE INTO {table} ({', '.join(columns)}) "
                f"VALUES ({placeholders})",
                [tuple(row[column] for column in columns) for row in rows],
            )
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
    for field, definition in [('estimated_monthly_spend', 'REAL'), ('spend_basis', 'TEXT'), ('discovery_url', 'TEXT'), ('customer_type', 'TEXT'), ('models_used', 'TEXT'), ('qualification_evidence', 'TEXT'), ('owner', 'TEXT'), ('company_follow_up_date', 'TEXT'), ('prospect_status', 'TEXT'), ('audience_scale', 'TEXT'), ('exclusion_reason', 'TEXT'), ('previous_prospect_status', 'TEXT')]:
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

    def query_pipeline(self, country_type: Optional[str] = None, status: Optional[str] = None, source_channel: Optional[str] = None, client_tier: Optional[str] = None) -> List[Dict[str, Any]]:
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
                o.next_follow_up_date
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
        query += " ORDER BY CASE coalesce(c.client_tier, 'B') WHEN 'S' THEN 1 WHEN 'A' THEN 2 WHEN 'B' THEN 3 ELSE 4 END, c.id DESC, l.id DESC"
        
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

    def authenticate_user(self, username: str, password: str) -> Optional[Dict[str, Any]]:
        conn = get_connection(self.db_path)
        cur = conn.cursor()
        pwd_hash = hash_password(password)
        cur.execute("SELECT id, username, display_name, role, email, phone, status, created_at FROM sys_users WHERE username = ? AND password_hash = ?", (username.strip(), pwd_hash))
        row = cur.fetchone()
        conn.close()
        if row:
            return dict(row)
        return None

    def register_user(self, username: str, password: str, display_name: str, role: str = 'sales_rep', email: str = '', phone: str = '') -> Dict[str, Any]:
        username = username.strip()
        if not username or not password:
            return {"success": False, "error": "用户名和密码不能为空"}
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
        conn = get_connection(self.db_path)
        cur = conn.cursor()
        cur.execute("UPDATE sys_users SET role = ? WHERE id = ?", (role, user_id))
        conn.commit()
        conn.close()
        return True

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
