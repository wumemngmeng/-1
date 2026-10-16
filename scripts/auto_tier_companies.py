"""
Assign tier (S, A, B, C) to all companies in aibubu_crm.db
Tier Evaluation Criteria for Aibubu (AI Commercial Photography, Infinite Canvas, Workflow & API Bulk Consumption):

[S级 - 战略级/特大客户 (流水王与头部服务商)]:
- 年流水过亿或规模>200人，千川月耗数十万到数百万的代投/服务商（如微盟、引力传媒、天下秀、蚊子会等）
- 头部美妆护肤品牌（如韩束、珀莱雅、自然堂、谷雨、花西子、溪木源）
- 海外头部 DTC 品牌或巨头广告代理（如 Shein生态、Gymshark、Anker、Ogilvy、WPP）
- 月生图需求在 50,000+ 张，需要私有化/团队空间大包充值或高并发 API。

[A级 - 核心腰部大客户 (高频生产，自建电商视觉设计团队)]:
- 规模 50-200人，月拍摄商拍需求 500-2000张模特/白底/场景图
- 天猫美妆Top榜、跨交会/美博会主力品牌、Shopify Plus 海外热卖品牌
- 年营收 5000万-2亿，能够极快采购 10-30 个座席并按月充值算力。

[B级 - 成长型高意向客户 (有稳定生图刚需，预算适中)]:
- 规模 20-50人，淘宝/抖音/亚马逊中腰部活跃卖家
- 摄影外包成本高企（月花费 2-5 万），急迫需要 AI 降本增效
- 适合推广 5-10 座席团队版。

[C级 - 长尾/潜力客户 (小微或探索阶段)]:
- 规模 <20人，初创品牌或试水期小团队
- 适合标准版自助充值与线上模板转化。
"""
import sys
import os
import sqlite3

sys.path.insert(0, '/Users/wuqiong/aibubu_outbound_engine')
from core.database import DatabaseManager, get_connection

def classify_tier(company: dict) -> str:
    name = company['name']
    scale = company.get('scale', '') or ''
    industry = company.get('industry', '') or ''
    source = company.get('source_channel', '') or ''
    notes = company.get('notes', '') or ''

    # 1. S 级 - 战略级/特大客户 (流水王、上市公司/行业巨头、预算10万+、头部千川代理商)
    s_names = [
        '安克', 'Anker', '傲基', '赛维', '通拓', '有棵树', '三态', '易佰',
        '珀莱雅', '韩束', '上海家化', '自然堂', '谷雨', '溪木源', '花西子',
        '微盟', '引力传媒', '天下秀', '壹网壹创', '若羽臣', '宝尊', '丽人丽妆',
        'Gymshark', 'SHEIN', 'Shopify Plus', 'Clutch Top', 'Meta Authorized',
        '百罗电商', '无忧传媒', '交个朋友', '美ONE', '遥望', '三只羊', '世纪华通',
        '三七互娱', '莉莉丝', '米哈游', '冰川网络', '吉比特'
    ]
    if any(k.lower() in name.lower() for k in s_names):
        return 'S'

    if '¥100,000+' in notes or '300-500人' in scale or '200-400人' in scale:
        return 'S'

    if '千川核心' in source or '阿里妈妈' in source:
        return 'S'

    # 2. A 级 - 核心腰部大客户 (预算5万+ 或 强商拍刚需的服饰/美妆/代投/3C出海，规模在100人左右)
    if '¥50,000+' in notes:
        return 'A'

    if any(s in scale for s in ['100-300人', '100-200人', '150-300人', '80-150人']):
        return 'A'

    if industry in ['效果广告代投', '跨境出海电商', 'Apparel & Fashion DTC', '彩妆护肤', '功能护肤'] and ('¥30,000+' in notes or 'Top' in source):
        return 'A'

    # 3. B 级 - 成长型高意向客户 (预算2万-3万，新消费/短剧/电商代运营，50人左右团队)
    if '¥30,000+' in notes or '¥20,000' in notes:
        return 'B'

    if industry in ['新消费/品牌自营', '短剧/游戏/动漫', '电商代运营/素材代投', '美妆供应链名录']:
        return 'B'

    # 4. C 级 - 长尾/潜力探索客户
    return 'C'

def run():
    db = DatabaseManager()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM companies")
    companies = [dict(r) for r in cur.fetchall()]
    
    tier_counts = {'S': 0, 'A': 0, 'B': 0, 'C': 0}
    for c in companies:
        tier = classify_tier(c)
        cur.execute("UPDATE companies SET client_tier = ? WHERE id = ?", (tier, c['id']))
        tier_counts[tier] += 1
    
    conn.commit()
    conn.close()
    print(f"✅ 企业客户等级打标完成！总企业数: {len(companies)}")
    print(f"📊 评级结果分布: S级(超大客户/战略级): {tier_counts['S']} 家 | A级(核心腰部大客户): {tier_counts['A']} 家 | B级(高意向成长客户): {tier_counts['B']} 家 | C级(长尾潜力客户): {tier_counts['C']} 家")

if __name__ == '__main__':
    run()
