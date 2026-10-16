"""Import researched prospects from JSON; atomic, additive, name/domain deduplicated."""
import argparse, json, sqlite3, sys
from pathlib import Path
from urllib.parse import urlparse
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.database import DatabaseManager

def domain(value):
    return urlparse(value if '://' in value else 'https://' + value).netloc.lower().removeprefix('www.')

def ingest(path, db_path=None):
    rows = json.loads(Path(path).read_text())
    if not isinstance(rows, list): raise ValueError('Expected JSON array')
    db = DatabaseManager(db_path) if db_path else DatabaseManager()
    added = skipped = 0
    with sqlite3.connect(db.db_path) as conn:
        conn.execute('BEGIN IMMEDIATE')
        existing = conn.execute('SELECT name, website FROM companies').fetchall()
        names = {n.strip().casefold() for n,w in existing}
        domains = {domain(w) for n,w in existing if w}
        for row in rows:
            name = row['name'].strip()
            website = row['website'].strip()
            source = row['discovery_url'].strip()
            if not name or not domain(website) or not source.startswith(('https://','http://')):
                raise ValueError('Name, website and public evidence URL required')
            if name.casefold() in names or domain(website) in domains:
                skipped += 1; continue
            spend = row.get('estimated_monthly_spend')
            basis = row.get('spend_basis', '')
            if spend is not None and (not isinstance(spend, (float,int)) or isinstance(spend,bool) or spend < 0 or not basis.strip()):
                raise ValueError('Spend must be nonnegative CNY/month with evidence basis')
            conn.execute('''INSERT INTO companies
                (name,country_type,industry,website,source_channel,notes,client_tier,estimated_monthly_spend,spend_basis,discovery_url)
                VALUES (?,?,?,?,?,?,?,?,?,?)''',
                (name,row.get('country_type','domestic'),row['industry'],website,'每日公开来源发现',row.get('notes',''),'B',spend,basis,source))
            names.add(name.casefold()); domains.add(domain(website)); added += 1
    return {'added':added,'skipped':skipped}

if __name__ == '__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('json_file'); args=parser.parse_args()
    print(json.dumps(ingest(args.json_file),ensure_ascii=False))
