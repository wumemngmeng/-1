"""
Stage 2: Lead Enrichment Module
Clean separation: for a given company, enriches verified decision-makers:
- Domestic: 真实手机/微信, 视觉总监/营销总监/电商负责人 (企查查客查查、脉脉直通、展商通讯录)
- Overseas: Work Email, LinkedIn Profile, Creative Director / VP Marketing (Apollo / Clay / Hunter)
- Supports manual import, API enrichment, or CSV enrichment.
"""
import os
import csv
from typing import List, Dict, Any, Optional
from core.database import DatabaseManager

class LeadEnricher:
    def __init__(self, db: DatabaseManager):
        self.db = db

    def enrich_from_csv(self, file_path: str) -> int:
        """从外部导出的关键人名单 (如脉脉导出、企查查客查查导出、Apollo导出) 批量入库关联"""
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"文件不存在: {file_path}")

        count = 0
        with open(file_path, mode="r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                company_name = row.get("公司名称") or row.get("Company Name") or row.get("company_name")
                contact_name = row.get("姓名") or row.get("联系人") or row.get("Name") or row.get("contact_name")
                job_title = row.get("职位") or row.get("头衔") or row.get("Title") or row.get("job_title") or "业务负责人"
                phone = row.get("手机号") or row.get("电话") or row.get("Phone") or ""
                wechat = row.get("微信号") or row.get("微信") or row.get("Wechat") or ""
                email = row.get("邮箱") or row.get("工作邮箱") or row.get("Email") or ""
                linkedin = row.get("领英主页") or row.get("LinkedIn") or ""
                source_method = row.get("来源渠道") or row.get("Source") or "高管联系人库"

                if not company_name or not contact_name:
                    continue

                # 查找关联的公司
                conn = self.db.get_connection() if hasattr(self.db, 'get_connection') else None
                # 通过现有查询获取公司ID
                import sqlite3
                conn = sqlite3.connect(self.db.db_path)
                cur = conn.cursor()
                cur.execute("SELECT id FROM companies WHERE name LIKE ?", (f"%{company_name.strip()}%",))
                comp = cur.fetchone()
                
                if comp:
                    comp_id = comp[0]
                else:
                    # 如果企业不存在，先自动创建企业记录
                    cur.execute("INSERT INTO companies (name, country_type, industry, source_channel) VALUES (?, ?, ?, ?)", 
                                (company_name.strip(), "domestic" if not email.endswith(".com") and not linkedin else "overseas", "待补充", "联系人反向建档"))
                    comp_id = cur.lastrowid
                    conn.commit()
                conn.close()

                self.db.insert_lead({
                    "company_id": comp_id,
                    "contact_name": contact_name.strip(),
                    "job_title": job_title.strip(),
                    "contact_role_category": "业务决策人" if any(k in job_title for k in ["总监", "VP", "Head", "负责人", "经理"]) else "核心执行人",
                    "phone": phone.strip(),
                    "wechat": wechat.strip(),
                    "email": email.strip(),
                    "linkedin_url": linkedin.strip(),
                    "source_method": source_method.strip(),
                    "confidence_score": 90 if (phone or email) else 70,
                    "identity_source_url": (row.get("身份来源URL") or row.get("Identity Source URL") or "").strip(),
                    "identity_evidence": (row.get("身份原文") or row.get("Identity Evidence") or "").strip(),
                    "phone_source_url": (row.get("手机号来源URL") or row.get("Phone Source URL") or "").strip(),
                    "phone_evidence": (row.get("手机号证据原文") or row.get("Phone Evidence") or "").strip(),
                    "phone_confidence": (row.get("手机号可信等级") or row.get("Phone Confidence") or "").strip(),
                    "wechat_source_url": (row.get("微信来源URL") or row.get("Wechat Source URL") or "").strip(),
                    "wechat_evidence": (row.get("微信证据原文") or row.get("Wechat Evidence") or "").strip(),
                    "wechat_confidence": (row.get("微信可信等级") or row.get("Wechat Confidence") or "").strip(),
                    "verified_at": (row.get("核验日期") or row.get("Verified At") or "").strip(),
                    "fit_score": row.get("适配评分") or row.get("Fit Score") or 0,
                    "wechat_same_as_phone_confirmed": str(
                        row.get("明确手机号同微信") or row.get("Wechat Same As Phone") or ""
                    ).strip().lower() in ("1", "true", "yes", "是"),
                })
                count += 1
        return count

    def seed_verified_leads(self) -> int:
        """Deprecated: synthetic contact templates must never enter the CRM."""
        raise RuntimeError("已禁用模板联系人导入；请仅导入带公开证据的真实联系人")

        # Kept below only for backward source compatibility; this code is unreachable.
        import sqlite3
        conn = sqlite3.connect(self.db.db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT id, name, country_type, industry FROM companies")
        companies = cur.fetchall()
        conn.close()

        count = 0
        lead_templates = {
            "青木数字技术股份有限公司": [
                {"name": "林总", "title": "视觉创意中心总监", "role": "业务决策人", "phone": "1380291**** (企查查商务直连)", "wechat": "同手机号", "email": "lin.creative@qiyun.com", "method": "企查查高管联系方式"},
                {"name": "陈经理", "title": "千川投放素材制作负责人", "role": "核心执行人", "phone": "1371108**** (脉脉直联)", "wechat": "chen_ad_design", "email": "chen.ad@qiyun.com", "method": "脉脉直通"}
            ],
            "若羽臣科技股份有限公司": [
                {"name": "张总监", "title": "全域内容营销事业部总监", "role": "预算把关人", "phone": "1392210**** (商务公开通道)", "wechat": "同手机号", "email": "zhang.mkt@rouchen.com", "method": "年报商务联络人"}
            ],
            "宝尊电商（上海）数字创意中心": [
                {"name": "王总", "title": "创意技术VP / Creative Tech VP", "role": "终审决策人", "phone": "1861633**** (脉脉认证高管)", "wechat": "wang_bz_creativelab", "email": "alex.wang@baozun.com", "method": "脉脉高管库"}
            ],
            "深圳前海帕拓逊网络技术有限公司": [
                {"name": "刘总", "title": "海外品牌视觉总监", "role": "业务决策人", "phone": "1368254**** (跨交会展商通讯录)", "wechat": "同手机号", "email": "liu.brand@pateck.com", "method": "跨交会高管通讯录"}
            ],
            "珀莱雅化妆品股份有限公司（新锐品牌线）": [
                {"name": "赵总监", "title": "新锐品牌市场部总监", "role": "预算把关人", "phone": "1375821**** (美博会商务注册)", "wechat": "proya_zhao_mkt", "email": "zhao.xy@proya.com", "method": "CIBE美博会名录"}
            ],
            "KraveBeauty": [
                {"name": "Sarah Jenkins", "title": "Head of Creative & Brand Design", "role": "业务决策人", "phone": "+1 (212) 555-0192", "wechat": "", "email": "sarah.jenkins@kravebeauty.com", "linkedin_url": "https://linkedin.com/in/sarahjenkins-creative", "method": "Apollo.io Verified Email"}
            ],
            "Ridge Wallet": [
                {"name": "Marcus Vance", "title": "VP of Growth & Performance Marketing", "role": "预算把关人", "phone": "+1 (310) 555-0144", "wechat": "", "email": "marcus.vance@ridge.com", "linkedin_url": "https://linkedin.com/in/marcusvance-growth", "method": "Clay.com Work Email"}
            ],
            "Cuts Clothing": [
                {"name": "Emily Carter", "title": "Art & E-commerce Visual Director", "role": "业务决策人", "phone": "", "wechat": "", "email": "emily@cutsclothing.com", "linkedin_url": "https://linkedin.com/in/emilycarter-art", "method": "Hunter.io Verified"}
            ],
            "MuteSix (iProspect Agency)": [
                {"name": "David Miller", "title": "SVP of Performance Creative", "role": "核心采购决策人", "phone": "+1 (323) 555-0188", "wechat": "", "email": "david.miller@mutesix.com", "linkedin_url": "https://linkedin.com/in/davidmiller-mutesix", "method": "Apollo.io Verified Email"}
            ]
        }

        for comp in companies:
            c_name = comp["name"]
            if c_name in lead_templates:
                for lead in lead_templates[c_name]:
                    self.db.insert_lead({
                        "company_id": comp["id"],
                        "contact_name": lead["name"],
                        "job_title": lead["title"],
                        "contact_role_category": lead["role"],
                        "phone": lead.get("phone", ""),
                        "wechat": lead.get("wechat", ""),
                        "email": lead.get("email", ""),
                        "linkedin_url": lead.get("linkedin_url", ""),
                        "source_method": lead.get("method", "商务直通"),
                        "confidence_score": 95
                    })
                    count += 1
        return count
