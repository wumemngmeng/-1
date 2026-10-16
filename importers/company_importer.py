"""
Stage 1: Company Sourcing Importers
Clean separation: solely finds companies with image-heavy workloads and strong payment capacity.
Sources:
1. 蝉妈妈/飞瓜等巨量千川授权电商投流榜单
2. 企查查/天眼查工商腰部高价值企业筛选 (参保50-300人)
3. 行业展商与供应链协会名录 (美博会/跨交会/广交会)
4. 海外 Store Leads / BuiltWith Shopify Plus 消费品牌
5. 支持任意 CSV / JSON 文件标准化导入
"""
import csv
import json
import os
from typing import List, Dict, Any
from core.database import DatabaseManager

class CompanyImporter:
    def __init__(self, db: DatabaseManager):
        self.db = db

    def import_from_csv(self, file_path: str, country_type: str = "domestic", default_source: str = "CSV导入") -> int:
        """从标准 CSV 导入企业数据"""
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"文件不存在: {file_path}")
        
        imported_count = 0
        with open(file_path, mode="r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                # 兼容常见表头
                name = row.get("企业名称") or row.get("公司名称") or row.get("Company Name") or row.get("name")
                if not name:
                    continue
                industry = row.get("行业赛道") or row.get("所属行业") or row.get("Industry") or row.get("industry") or "电商/广告"
                scale = row.get("人员规模") or row.get("企业规模") or row.get("Scale") or "50-200人"
                website = row.get("官网地址") or row.get("公司官网") or row.get("Website") or ""
                city = row.get("所在城市") or row.get("地区") or row.get("City") or ""
                source = row.get("数据来源") or row.get("Source") or default_source
                notes = row.get("业务背景") or row.get("备注") or row.get("Notes") or ""

                self.db.insert_company({
                    "name": name.strip(),
                    "country_type": country_type,
                    "industry": industry.strip(),
                    "scale": scale.strip(),
                    "website": website.strip(),
                    "city_or_region": city.strip(),
                    "source_channel": source.strip(),
                    "notes": notes.strip()
                })
                imported_count += 1
        return imported_count

    def seed_verified_samples(self) -> Dict[str, int]:
        """预置第一批精细筛选后的国内与海外典型企业库 (剔除巨头，专注高消耗腰部实体)"""
        domestic_samples = [
            # 1. 巨量千川/电商素材月耗巨头代投机构 (成百上千套做图排期痛点)
            {"name": "青木数字技术股份有限公司", "country_type": "domestic", "industry": "电商代运营/素材代投", "scale": "300-500人", "website": "https://www.qiyun.com", "city_or_region": "广州", "source_channel": "千川核心服务商榜单", "notes": "月消耗数百万素材，主营大牌美妆天猫/抖音代运营"},
            {"name": "若羽臣科技股份有限公司", "country_type": "domestic", "industry": "电商全域代运营", "scale": "200-400人", "website": "https://www.rouchen.com", "city_or_region": "广州", "source_channel": "千川核心服务商榜单", "notes": "大健康/美妆保健类目头部代运营，主图与KV制作需求极大"},
            {"name": "宝尊电商（上海）数字创意中心", "country_type": "domestic", "industry": "品牌数字创意代投", "scale": "300-500人", "website": "https://www.baozun.com", "city_or_region": "上海", "source_channel": "阿里妈妈超级服务商", "notes": "国际品牌服饰与3C商拍设计大户"},
            {"name": "杭州光云科技数字营销部", "country_type": "domestic", "industry": "电商广告服务", "scale": "150-300人", "website": "https://www.kuaimai.com", "city_or_region": "杭州", "source_channel": "天猫服务商名录", "notes": "服务数万商家主图与详情页批量换装与换景"},
            {"name": "深圳前海帕拓逊网络技术有限公司", "country_type": "domestic", "industry": "跨境3C/大卖", "scale": "200-400人", "website": "https://www.pateck.com", "city_or_region": "深圳", "source_channel": "跨交会头部展商", "notes": "海外亚马逊与自建站3C爆款，渲染与精修图月需求超千张"},
            
            # 2. 垂直赛道自主品牌 (美妆护肤/日化个护 - 商拍外模成本最高)
            {"name": "珀莱雅化妆品股份有限公司（新锐品牌线）", "country_type": "domestic", "industry": "美妆个护", "scale": "100-300人", "website": "https://www.proya.com", "city_or_region": "杭州", "source_channel": "美博会重点品牌", "notes": "子品牌推新速度极快，每月大量静物与场景瓶身渲染需求"},
            {"name": "广州逸仙电子商务（完子心选事业部）", "country_type": "domestic", "industry": "新锐美妆彩妆", "scale": "150-300人", "website": "https://www.yatsenglobal.com", "city_or_region": "广州", "source_channel": "天猫美妆Top榜", "notes": "色彩还原度要求高，传统摄影棚排期长"},
            {"name": "卡姿兰数字创意营销中心", "country_type": "domestic", "industry": "彩妆护肤", "scale": "100-200人", "website": "https://www.carslan.com.cn", "city_or_region": "广州", "source_channel": "美妆供应链名录", "notes": "电商渠道千川跑量素材日更需求旺盛"},
            {"name": "上海家化联合股份有限公司（玉泽品牌部）", "country_type": "domestic", "industry": "功能护肤", "scale": "100-200人", "website": "https://www.jahwa.com.cn", "city_or_region": "上海", "source_channel": "天猫大药房美妆榜", "notes": "严谨功效图与场景试用图需求，外景实拍成本高"}
        ]

        overseas_samples = [
            # 1. 北美 DTC 快速增长消费品 (Shopify Plus，痛点：欧美实拍单天 $3000+)
            {"name": "KraveBeauty", "country_type": "overseas", "industry": "Clean Skincare DTC", "scale": "50-100人", "website": "https://kravebeauty.com", "city_or_region": "New York, USA", "source_channel": "Store Leads Shopify Plus", "notes": "极简护肤品牌，社媒和官网更新频繁，大量极简场景渲染图需求"},
            {"name": "Ridge Wallet", "country_type": "overseas", "industry": "Everyday Carry / DTC", "scale": "80-150人", "website": "https://ridge.com", "city_or_region": "California, USA", "source_channel": "Store Leads Top 500", "notes": "投流大户，依靠海量变体渲染和场景图在 Meta/TikTok 跑量"},
            {"name": "Cuts Clothing", "country_type": "overseas", "industry": "Apparel & Fashion DTC", "scale": "50-120人", "website": "https://cutsclothing.com", "city_or_region": "Los Angeles, USA", "source_channel": "BuiltWith DTC Brands", "notes": "男士服饰，模特排期贵，迫切需要高质量AI模特上身免实拍"},
            {"name": "Native Deodorant (P&G DTC Subsidiary)", "country_type": "overseas", "industry": "Personal Care DTC", "scale": "100-200人", "website": "https://nativecos.com", "city_or_region": "San Francisco, USA", "source_channel": "Store Leads", "notes": "大量香型与季节限定包装，需要极速出图"},
            
            # 2. 欧美效果类广告代理商 (Performance Creative Agencies)
            {"name": "MuteSix (iProspect Agency)", "country_type": "overseas", "industry": "DTC Performance Agency", "scale": "150-300人", "website": "https://mutesix.com", "city_or_region": "Los Angeles, USA", "source_channel": "Clutch Top Ad Agency", "notes": "月消耗数万条广告静态与动态素材，痛点在创意制作产能上限"},
            {"name": "Disruptor School / Pilothouse", "country_type": "overseas", "industry": "Growth Marketing Agency", "scale": "100-200人", "website": "https://pilothouse.co", "city_or_region": "Victoria, Canada", "source_channel": "Meta Authorized Agency", "notes": "专门帮DTC品牌跑海量测图测款，重度依赖高效创意生成工作流"}
        ]

        d_count = 0
        o_count = 0
        for item in domestic_samples:
            self.db.insert_company(item)
            d_count += 1
        for item in overseas_samples:
            self.db.insert_company(item)
            o_count += 1
        return {"domestic": d_count, "overseas": o_count}
