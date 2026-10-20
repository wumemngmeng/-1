"""Generate reviewable outreach drafts from recorded customer profiles."""
from typing import Dict

from core.database import DatabaseManager, get_connection


PROFILES = {
    "commerce": {
        "label": "电商商拍客户", "role": "电商视觉 / 品牌内容负责人",
        "topic": "商品图片与场景素材的小范围试点",
        "question": "贵司是否需要从现有商品图扩展多场景素材？目前更关注产品还原、出图效率还是审核流程？",
        "pilot": "先选一个 SKU，经授权使用现有商品图，对比产品还原、品牌一致性、人工修改时间与单张成本，再判断是否扩大使用。",
        "topic_en": "a small product-imagery workflow pilot",
        "question_en": "Does your team need more scene variations from existing product images, and how do you review product accuracy and brand consistency?",
        "pilot_en": "We could scope a one-SKU pilot using authorized assets and compare accuracy, review effort and per-image cost before deciding whether to expand.",
    },
    "api": {
        "label": "API / AI 应用客户", "role": "产品 / 技术 / 模型采购负责人",
        "topic": "图片 / 视频生成工作流的技术适配评估",
        "question": "贵司是否在评估图片或视频生成能力？当前更关注模型效果、并发稳定性、调用成本还是接入方式？",
        "pilot": "先确认所需模型、接口协议、数据合规与服务边界；由双方技术人员核实 Aibubu 是否支持，再用授权测试集对比质量、延迟、成功率和成本，不预先承诺 API 或 SLA。",
        "topic_en": "a technical fit assessment for image/video generation",
        "question_en": "Are you evaluating image or video generation, and which matters most: model quality, reliability, integration requirements or cost?",
        "pilot_en": "Our technical teams would first verify model and interface support, data requirements and service boundaries, then agree on an authorized test set to compare quality, latency, success rate and cost. API availability and SLAs require confirmation.",
    },
    "agency": {
        "label": "广告 / 代运营机构", "role": "创意制作 / 客户交付负责人",
        "topic": "多客户素材制作与审核流程试点",
        "question": "贵司多客户交付中，是否有素材版本制作、品牌隔离或客户审核反复的问题？哪一环最值得先验证？",
        "pilot": "选一个客户的一份已授权 brief，测试少量创意版本，记录交付时长、修改轮次与审核结果；先确认客户素材授权及品牌要求。",
        "topic_en": "a multi-client creative production pilot",
        "question_en": "In your client delivery workflow, are creative variations, brand separation or review cycles areas worth improving?",
        "pilot_en": "We could test a few variations for one authorized client brief and compare turnaround, revision rounds and approval outcomes, subject to asset permissions and brand requirements.",
    },
    "unknown": {
        "label": "待分类 / 需确认", "role": "业务 / 产品负责人",
        "topic": "图片 / 视频业务需求确认",
        "question": "想先确认贵司是否有图片或视频制作需求，以及由哪个团队负责；如果不相关，我们就不继续打扰。",
        "pilot": "先确认客户类型、实际场景、负责部门与合作意愿，再决定是否演示；不要直接推商拍或 API 方案。",
        "topic_en": "a brief image/video workflow discovery conversation",
        "question_en": "Does your team have image or video production needs, and who would be the appropriate person to speak with? If this is not relevant, we will not follow up.",
        "pilot_en": "First confirm the use case, responsible team and interest before selecting a demo or discussing any technical integration.",
    },
}


def select_profile(customer_type, industry):
    """Explicit customer types win; only use narrow industry hints as fallback."""
    value = (customer_type or "").strip()
    if value:
        if "API" in value.upper() or value == "工具→API":
            return "api", "已记录客户类型"
        if any(word in value for word in ("广告", "代运营", "机构")):
            return "agency", "已记录客户类型"
        if any(word in value for word in ("商拍", "电商", "品牌")):
            return "commerce", "已记录客户类型"
        return "unknown", "客户类型尚未识别"
    text = (industry or "").lower()
    if any(word in text for word in ("agency", "广告", "代运营", "营销服务")):
        return "agency", "按行业推测，发送前需确认"
    if any(word in text for word in ("ai", "saas", "api", "人工智能")):
        return "api", "按行业推测，发送前需确认"
    if any(word in text for word in ("电商", "品牌自营", "dtc", "e-commerce")):
        return "commerce", "按行业推测，发送前需确认"
    return "unknown", "资料不足，先确认需求"


class OutreachEngine:
    def __init__(self, db: DatabaseManager):
        self.db = db

    def generate_pitch(self, lead_id: int) -> Dict[str, str]:
        with get_connection(self.db.db_path) as conn:
            row = conn.execute("""
                SELECT l.*, c.name AS company_name, c.country_type, c.industry,
                       c.customer_type, c.models_used, c.qualification_evidence,
                       c.discovery_url
                FROM leads l JOIN companies c ON l.company_id = c.id WHERE l.id = ?
            """, (lead_id,)).fetchone()
        if not row:
            raise ValueError(f"未找到线索 ID: {lead_id}")
        lead = dict(row)
        key, basis = select_profile(lead["customer_type"], lead["industry"])
        profile = PROFILES[key]
        company = lead["company_name"]
        contact = lead["contact_name"] or "相关负责人"
        evidence = lead["qualification_evidence"] or "暂无需求证据，先核实需求"
        source = lead["discovery_url"] or "未记录来源"
        models = lead["models_used"] or "未记录，需确认"
        context = (
            f"客户类型：{profile['label']}（{basis}）\n行业：{lead['industry'] or '待确认'}\n"
            f"建议对接：{profile['role']}；现有联系人身份需核验。\n"
            f"CRM 需求记录（未经本次核验）：{evidence}\n来源：{source}\n模型记录：{models}\n"
            "以下为待人工确认的草稿，不代表已制作样张、已验证预算或已承诺能力。"
        )
        strategy = (
            f"{profile['pilot']}\n"
            "发送前核对官网、联系人职责和上述需求记录；获得回复后再提供经确认的演示或报价。"
            "未回复时可在 3–5 个工作日后礼貌跟进一次；拒绝或要求停止联系后不再触达。"
        )
        common = {
            "headline": f"{profile['label']} · {company} · {contact}",
            "customer_type": profile["label"], "profile_key": key,
            "research_context": context, "strategy": strategy,
        }
        if lead["country_type"] == "domestic":
            return dict(common, channel="wechat_direct",
                wechat_request=f"{contact}您好，我是 Aibubu 团队，想与{company}负责{profile['role']}的同事探讨{profile['topic']}，方便沟通吗？",
                phone_script=f"{contact}您好，我来自 Aibubu。想向{company}负责{profile['role']}的同事确认一个需求。\n"
                    f"{profile['question']}\n如果相关，能否约一次简短沟通，先确认{profile['topic']}是否适合？如非您负责，方便指引官方合作入口吗？")
        return dict(common, channel="cold_email", is_overseas=True,
            email_subject=f"{company}: {profile['topic_en']}",
            email_subject_zh=f"{company}：{profile['topic']}",
            email_body=f"Hello,\n\nI'm with Aibubu and would like to discuss {profile['topic_en']} with the relevant team at {company}.\n\n"
                f"{profile['question_en']}\n\n{profile['pilot_en']}\n\n"
                "Would a short discovery call be useful? If this is not relevant, let me know and I will not follow up.\n\nAibubu Team",
            email_body_zh=f"您好，\n\n我是 Aibubu 团队，想与 {company} 探讨{profile['topic']}。\n\n"
                f"{profile['question']}\n\n{profile['pilot']}\n\n方便先做一次简短需求沟通吗？若不相关，请告知，我们不会继续跟进。\n\nAibubu 团队",
            linkedin_note=f"Hello, I'm with Aibubu. Would the relevant team at {company} be open to {profile['topic_en']}?",
            linkedin_note_zh=f"您好，我来自 Aibubu，想请教 {company} 是否愿意探讨{profile['topic']}？")

    def record_touch(self, lead_id: int, channel: str, status: str, pitch_angle: str, notes: str, next_date: str = None) -> int:
        return self.db.record_outreach({
            "lead_id": lead_id, "channel": channel, "status": status,
            "pitch_angle": pitch_angle, "interaction_notes": notes,
            "next_follow_up_date": next_date,
        })
