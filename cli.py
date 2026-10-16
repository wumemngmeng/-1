#!/usr/bin/env python3
"""
Aibubu B2B Enterprise Outbound Engine CLI & Exporter
Usage:
    python cli.py stats               -- 查看当前企业池、线索池与触达漏斗数据
    python cli.py seed                -- 初始化写入精选企业与决策人样本库
    python cli.py list [--country cn|global] [--status STATUS]
    python cli.py pitch <lead_id>     -- 生成针对该线索的定制化破冰话术(微信/电话/邮件)
    python cli.py touch <lead_id> --status <STATUS> --notes <NOTES>
    python cli.py export-excel        -- 导出当前完整的作战跟进表为 Excel (.csv)
"""
import sys
import os
import argparse
import csv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.database import DatabaseManager
from importers.company_importer import CompanyImporter
from enrichers.lead_enricher import LeadEnricher
from outreach.outreach_engine import OutreachEngine

def main():
    parser = argparse.ArgumentParser(description="Aibubu 工业级 ToB 销售作战中台")
    subparsers = parser.add_subparsers(dest="command", help="子命令")

    # stats
    subparsers.add_parser("stats", help="查看数据库汇总统计")

    # seed
    subparsers.add_parser("seed", help="预置经过严格筛选的高价值腰部企业与关键人样本")

    # list
    list_parser = subparsers.add_parser("list", help="查看销售管线中的企业与线索")
    list_parser.add_argument("--country", choices=["cn", "global", "all"], default="all", help="区域筛选")
    list_parser.add_argument("--status", default=None, help="跟进状态筛选 (pending, contacted, won, etc.)")

    # pitch
    pitch_parser = subparsers.add_parser("pitch", help="生成直达决策人的定制话术")
    pitch_parser.add_argument("lead_id", type=int, help="决策人线索ID")

    # touch
    touch_parser = subparsers.add_parser("touch", help="记录一次销售触达与漏斗推进")
    touch_parser.add_argument("lead_id", type=int, help="决策人线索ID")
    touch_parser.add_argument("--channel", default="wechat_direct", choices=["wechat_direct", "phone_call", "cold_email", "linkedin_inmail"], help="触达渠道")
    touch_parser.add_argument("--status", default="contacted", choices=["pending", "contacted", "sample_sent", "team_trial", "negotiation", "won", "lost"], help="最新状态")
    touch_parser.add_argument("--notes", default="", help="沟通反馈原话/纪要")
    touch_parser.add_argument("--pitch", default="", help="使用的切入角度")

    # export-excel
    export_parser = subparsers.add_parser("export-excel", help="导出当前完整管线为 Excel/CSV 文件")
    export_parser.add_argument("--output", default=None, help="导出文件路径")

    # import-company
    imp_parser = subparsers.add_parser("import-companies", help="从外部 CSV 导入企业")
    imp_parser.add_argument("file", help="CSV文件路径")
    imp_parser.add_argument("--country", choices=["domestic", "overseas"], default="domestic")

    # import-leads
    lead_imp_parser = subparsers.add_parser("import-leads", help="从外部 CSV 导入关键人线索")
    lead_imp_parser.add_argument("file", help="CSV文件路径")

    args = parser.parse_args()

    db = DatabaseManager()
    importer = CompanyImporter(db)
    enricher = LeadEnricher(db)
    engine = OutreachEngine(db)

    if args.command == "seed":
        c_res = importer.seed_verified_samples()
        l_res = enricher.seed_verified_leads()
        print("✅ 成功初始化底层数据！")
        print(f"   • 入库企业: 国内 {c_res['domestic']} 家, 海外 {c_res['overseas']} 家")
        print(f"   • 直联决策人: 录入 {l_res} 位关键总监/VP")

    elif args.command == "stats":
        stats = db.get_stats()
        print("")
        print("================= 📊 Aibubu 销售作战中台当前概况 =================")
        print(f" 🏢 企业总数: {stats['total_companies']} 家")
        print(f"    - 国内企业: {stats['companies_by_region'].get('domestic', 0)} 家")
        print(f"    - 海外企业: {stats['companies_by_region'].get('overseas', 0)} 家")
        print(f" 👤 直连决策人: {stats['total_leads']} 位")
        print(" 🚀 触达漏斗分布:")
        for st, cnt in stats['outreach_funnel'].items():
            print(f"    - {st:12s}: {cnt} 位")
        print("=================================================================")
        print("")

    elif args.command == "list":
        c_filter = "domestic" if args.country == "cn" else ("overseas" if args.country == "global" else None)
        rows = db.query_pipeline(country_type=c_filter, status=args.status)
        print("")
        print(f"📋 查询结果 (共 {len(rows)} 条线索记录):")
        print("-" * 120)
        print(f"{'线索ID':<6} | {'企业名称':<25} | {'赛道':<14} | {'决策人':<10} | {'职位':<18} | {'直联方式':<20} | {'状态'}")
        print("-" * 120)
        for r in rows:
            contact = r['contact_name'] or "【暂缺关键人】"
            title = r['job_title'] or "-"
            direct = r['phone'] or r['email'] or r['wechat'] or "-"
            if len(direct) > 20:
                direct = direct[:17] + "..."
            status = r['status'] or "pending"
            c_name = r['company_name']
            if len(c_name) > 22:
                c_name = c_name[:20] + ".."
            print(f"{r['lead_id'] or '-':<6} | {c_name:<25} | {r['industry']:<14} | {contact:<10} | {title:<18} | {direct:<20} | {status}")
        print("-" * 120)

    elif args.command == "pitch":
        res = engine.generate_pitch(args.lead_id)
        print("")
        print(f"🎯 {res['headline']}")
        print("=" * 60)
        if "phone_script" in res:
            print("【微信好友申请备注 (直击利益)】:")
            print(f"  {res['wechat_request']}")
            print("")
            print("【30秒电话切入脚本 (不废话，只谈ROI)】:")
            print(f"  {res['phone_script']}")
            print("")
            print("【后续跟进动作】:")
            print(f"  {res['strategy']}")
        else:
            print("【Cold Email 邮件标题】:")
            print(f"  {res['email_subject']}")
            print("")
            print("【Cold Email 正文】:")
            print(res['email_body'])
            print("")
            print("【LinkedIn 领英破冰留言】:")
            print(f"  {res['linkedin_note']}")
        print("=" * 60)

    elif args.command == "touch":
        engine.record_touch(
            lead_id=args.lead_id,
            channel=args.channel,
            status=args.status,
            pitch_angle=args.pitch,
            notes=args.notes
        )
        print(f"✅ 成功记录触达！线索 #{args.lead_id} 状态已更新为: [{args.status}]")

    elif args.command == "export-excel":
        out_path = args.output or os.path.expanduser("~/Desktop/Aibubu_销售作战跟进表.csv")
        rows = db.query_pipeline()
        with open(out_path, mode="w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "企业ID", "企业名称", "区域", "行业赛道", "规模", "企业官网", "公司数据来源",
                "线索ID", "决策人姓名", "真实职位", "手机/微信", "工作邮箱", "领英主页", "线索来源",
                "当前跟进状态", "触达渠道", "破冰切入点", "沟通纪要"
            ])
            for r in rows:
                writer.writerow([
                    r['company_id'], r['company_name'], r['country_type'], r['industry'], r['scale'], r['website'], r['source_channel'],
                    r['lead_id'], r['contact_name'], r['job_title'], r['phone'] or r['wechat'], r['email'], r['linkedin_url'], r.get('lead_source') or '',
                    r['status'], r['channel'], r['pitch_angle'], r['interaction_notes']
                ])
        print(f"✅ 成功导出销售作战表至桌面: {out_path}")

    elif args.command == "import-companies":
        cnt = importer.import_from_csv(args.file, country_type=args.country)
        print(f"✅ 成功导入 {cnt} 家企业！")

    elif args.command == "import-leads":
        cnt = enricher.enrich_from_csv(args.file)
        print(f"✅ 成功导入并关联 {cnt} 条关键人线索！")

    else:
        parser.print_help()

if __name__ == "__main__":
    main()
