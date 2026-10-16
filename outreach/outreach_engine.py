"""
Stage 3: Direct Outreach Module
Clean separation: handles actual sales touchpoints.
- Domestic: Phone script, WeChat invite templates, custom POC angle
- Overseas: Cold email templates, Loom video scripts, LinkedIn connection notes
- Records interaction history and advances sales pipeline
"""
import os
import sqlite3
from typing import Dict, Any, List
from core.database import DatabaseManager

class OutreachEngine:
    def __init__(self, db: DatabaseManager):
        self.db = db

    def generate_pitch(self, lead_id: int) -> Dict[str, str]:
        """针对该决策人及其所在企业，自动生成针对性直达话术（国内微信/电话，海外邮件）"""
        conn = sqlite3.connect(self.db.db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("""
            SELECT l.*, c.name as company_name, c.country_type, c.industry, c.notes as company_notes
            FROM leads l
            JOIN companies c ON l.company_id = c.id
            WHERE l.id = ?
        """, (lead_id,))
        row = cur.fetchone()
        conn.close()

        if not row:
            raise ValueError(f"未找到线索 ID: {lead_id}")

        lead = dict(row)
        country = lead['country_type']
        comp_name = lead['company_name']
        contact_name = lead['contact_name']
        title = lead['job_title']
        industry = lead['industry']

        if country == 'domestic':
            wechat_add_note = f"【{comp_name}】{contact_name}您好，关注到贵司近期在推新品，我们做了一套单组节省75%成本的AI商拍方案与样张，给您呈阅参考。"
            phone_lines = [
                "【30秒电销直通】",
                f"“{contact_name}您好，我是 Aibubu 商业化负责人。知道您平时负责{title}这块工作极其忙碌，我直奔主题：",
                f"我们监测到很多做{industry}的头部团队目前最大的痛点是外模实拍排期长、千川跑量图消耗极快。",
                "我们团队专门针对贵司的主力产品做了一组无实拍AI生图的商业级成片对比。不占用您太多时间，我加您微信给您发过去您审阅下？”"
            ]
            phone_script = chr(10).join(phone_lines)
            follow_up_memo = "若添加成功，立即在微信发送 3 张带水印高清对比图 + 10 秒画布工作流演示 GIF，并邀请开通团队空间 14 天专业版试用。"
            return {
                "channel": "wechat_direct",
                "headline": f"国内直连触达: {comp_name} - {contact_name}({title})",
                "wechat_request": wechat_add_note,
                "phone_script": phone_script,
                "strategy": follow_up_memo
            }
        else:
            cold_email_subject = f"AI product photoshoot workflows for {comp_name} (sample inside)"
            cold_email_subject_zh = f"为 {comp_name} 定制的AI商拍工作流（附样张）"
            first_name = contact_name.split()[0] if contact_name else "there"
            body_paras = [
                f"Hi {first_name},",
                f"Noticed {comp_name}'s recent creative rollouts in the {industry} space. Most creative teams we work with currently spend upwards of $3,500/day on studio shoots and model licensing, only for ad fatigue to kick in after 2 weeks.",
                "We created a quick 90-second workflow demo using Aibubu's infinite canvas to render 4K studio-grade scenes directly from your existing packshots without physical reshoots.",
                "Would you be open to a 3-minute Loom video walking through how your design team can test this out?",
                "Best regards,",
                "Aibubu Enterprise Team"
            ]
            cold_email_body = (chr(10) + chr(10)).join(body_paras)
            
            body_paras_zh = [
                f"您好 {first_name}，",
                f"关注到 {comp_name} 近期在 {industry} 领域的优质创意推广。我们合作的大多数创意团队目前在影棚拍摄和模特版权上的单日开销往往超过 3,500 美元，但素材跑量两周后就会产生审美疲劳。",
                "我们制作了一个90秒的工作流演示：利用 Aibubu 的无限画布能力，无需实地补拍，直接基于贵司现有白底/商品图生成 4K 影棚级商业场景。",
                "您是否方便观看一段3分钟的演示视频，了解贵司设计团队如何快速测试这一方案？",
                "祝好，",
                "Aibubu 企业商务团队"
            ]
            cold_email_body_zh = (chr(10) + chr(10)).join(body_paras_zh)

            linkedin_note = f"Hi {first_name}, loved {comp_name}'s recent campaign. Built an AI canvas workflow that cuts e-commerce photoshoot cycles by 70%. Would love to connect and share sample renders!"
            linkedin_note_zh = f"您好 {first_name}，非常喜欢 {comp_name} 最近的营销推广。我们打造了一套AI画布工作流，能将电商商拍周期缩短70%，希望能与您建立联系并分享效果样张！"

            return {
                "channel": "cold_email",
                "headline": f"Global Outreach (海外直触): {comp_name} - {contact_name} ({title})",
                "email_subject": cold_email_subject,
                "email_subject_zh": cold_email_subject_zh,
                "email_body": cold_email_body,
                "email_body_zh": cold_email_body_zh,
                "linkedin_note": linkedin_note,
                "linkedin_note_zh": linkedin_note_zh,
                "is_overseas": True
            }

    def record_touch(self, lead_id: int, channel: str, status: str, pitch_angle: str, notes: str, next_date: str = None) -> int:
        return self.db.record_outreach({
            "lead_id": lead_id,
            "channel": channel,
            "status": status,
            "pitch_angle": pitch_angle,
            "interaction_notes": notes,
            "next_follow_up_date": next_date
        })
