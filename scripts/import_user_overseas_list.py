"""Import the overseas ecommerce/AI prospect list supplied by the user.

The importer is idempotent by normalized website domain. It enriches matching
companies instead of creating duplicate records and only writes founder leads
when a public profile has been verified.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.database import DatabaseManager, get_connection


SOURCE_CHANNEL = "用户提供海外电商 AI 企业名单（2026-10-15）"
VERIFIED_ON = "2026-10-16"
DOMAIN_ALIASES = {
    # Recharge moved its primary domain but is the same company.
    "getrecharge.com": "rechargepayments.com",
}

# English name, Chinese display name, website, industry, region, public product
# signal, evidence URL, sales note.
COMPANIES = [
    ("Gorgias", "戈尔吉亚斯（Gorgias）", "https://www.gorgias.com", "电商客户支持 SaaS", "北美", "AI Agent 调用 OpenAI、Anthropic 及内部微调模型", "https://docs.gorgias.com/en-US/security-and-privacy-faq-for-gorgias-ai-agent-1997987", "核心 Shopify 商户客户体验技术栈"),
    ("Triple Whale", "三鲸分析（Triple Whale）", "https://www.triplewhale.com", "电商分析 / 归因", "北美", "Claude 驱动 MOI 与 Deep Dive 智能体，公开案例披露日均约 3,000 次分析请求", "https://claude.com/customers/triple-whale", "电商分析方向的 Claude Platform 采购方"),
    ("Tidio", "蒂迪奥（Tidio）", "https://www.tidio.com", "电商在线客服 / 支持", "欧洲", "Lyro AI 基于 Claude，并通过 Anthropic API 与 Google Vertex AI 路由", "https://claude.com/customers/tidio", "波兰起源，聚焦电商客服与商品推荐"),
    ("Reversia", "逆向本地化（Reversia）", "https://reversia.tech", "电商本地化 / Shopify", "欧洲", "Claude Sonnet 用于翻译，Haiku 用于质量检查，覆盖 110 多种语言", "https://claude.com/customers/reversia", "巴黎 Shopify 应用，适合 API 与批量内容场景"),
    ("Klaviyo", "克拉维约（Klaviyo）", "https://www.klaviyo.com", "电商营销 / CDP", "北美", "生成式 AI 使用 OpenAI、Anthropic、AWS Bedrock 与 Gemini", "https://www.klaviyo.com/legal/ai-faqs", "中大型电商营销平台，多模型采购方"),
    ("Loop Returns", "循环退货（Loop Returns）", "https://www.loopreturns.com", "电商退货 / 购后", "北美", "Loop MCP 可让 Claude、ChatGPT、Cursor 接入退货运营", "https://docs.loopreturns.com/mcp/overview", "产品级 MCP 集成明确，模型 API 采购强度待核验"),
    ("Attentive", "专注消息（Attentive）", "https://www.attentive.com", "电商短信 / 消息", "北美", "Attentive AI 用于短信文案和对话，并提供面向 Claude 与 ChatGPT 的 MCP", "https://help.attentive.com/hc/en-us/articles/14826254934804-Get-started-with-Attentive-AI", "电商消息中上规模企业，底层模型供应商待确认"),
    ("Postscript", "后记短信（Postscript）", "https://postscript.io", "电商短信 / Shopify", "北美", "官方只读 MCP 对接 Claude、ChatGPT、Claude Code 与 Codex", "https://help.postscript.io/en/articles/17455878-postscript-mcp", "Shopify 短信平台，MCP 集成信号强"),
    ("ShipBob", "船舶鲍勃（ShipBob）", "https://www.shipbob.com", "电商履约 / 3PL", "北美", "Anthropic 认证履约连接器，可读写订单、库存和物流", "https://www.prnewswire.com/news-releases/shipbob-launches-first-anthropic-verified-fulfillment-connector-anchoring-its-ai-suite-302842134.html", "中大型履约平台，Claude 连接器已上线"),
    ("Kodif", "科迪夫（Kodif）", "https://kodif.ai", "电商客服 AI 平台", "欧洲", "Claude on Amazon Bedrock 驱动客服智能体", "https://claude.com/customers/kodif", "面向电商的行动型客户体验平台"),
    ("Stay AI", "留存智能（Stay AI）", "https://stay.ai", "电商订阅留存", "北美", "订阅留存与取消挽回 AI，公开子处理方包含 OpenAI 与 Anthropic", "https://synergetic.solutions/trust-index/stay/", "Shopify 订阅留存平台"),
    ("Recharge", "锐充订阅（Recharge）", "https://getrecharge.com", "电商订阅平台", "北美", "官方 Analytics MCP 早期采用者计划对接 AI 助手", "https://support.getrecharge.com/hc/en-us/articles/43339934359319-Recharge-Analytics-MCP-Early-Adopter-Program", "Shopify 订阅头部平台，MCP 信号明确"),
    ("Chatbase", "聊天基地（Chatbase）", "https://www.chatbase.co", "电商聊天机器人 SaaS", "北美", "Claude Platform 驱动电商智能体，可搜索目录、推荐商品和查询订单", "https://claude.com/customers/chatbase", "面向电商的聊天机器人构建层"),
    ("BigCommerce", "大商业（BigCommerce）", "https://www.bigcommerce.com", "中腰部电商平台", "北美", "Merchant AI 支持自带 OpenAI、Anthropic Claude、Gemini，并提供 MCP Server", "https://docs.bigcommerce.com/developer/api-reference/mcp/overview", "多渠道电商平台，BYO LLM 与 MCP"),
    ("Commerce Layer", "商务层（Commerce Layer）", "https://commercelayer.io", "无头电商 / API 商务", "欧洲 / 全球", "Core 与 Metrics MCP 对接 Claude、ChatGPT、Cursor", "https://docs.commercelayer.io/ai/mcp/servers/core", "开发者向无头电商平台"),
    ("Nosto", "诺斯托（Nosto）", "https://www.nosto.com", "电商个性化 / 推荐", "欧洲 / 北美", "生成式同义词与推荐文案使用 OpenAI，Huginn 智能体可经 MCP 接入 Claude", "https://www.nosto.com/commerce-experience-platform/artificial-intelligence/generative-ai/", "Shopify 与 Magento 个性化平台"),
    ("Yotpo", "约特波（Yotpo）", "https://www.yotpo.com", "电商评论 / 忠诚", "北美 / 以色列", "评论 AI 摘要、回复与面向 ChatGPT、Claude、Gemini 的 LLM 可见性能力", "https://www.yotpo.com/platform/reviews/llm-discoverability/", "电商留存与评论平台，第三方 API 采购细节待核验"),
    ("Okendo", "奥肯多（Okendo）", "https://okendo.io", "电商评论 / UGC", "亚太 / 北美", "Shopify 评论平台提供 AI 摘要、结构化属性与 LLM 可见性工具", "https://okendo.io/resources/blog/okendo-reviews-vs-yotpo-reviews-key-feature-differences-for-shopify-brands/", "Shopify 评论平台，模型供应商待核验"),
    ("Junip", "杜松评论（Junip）", "https://juniphq.com", "电商评论 / AI 销售", "北美", "评论平台与 Premium AI 销售智能体", "https://juniphq.com", "中型评论平台，模型供应商待核验"),
    ("Rebuy", "再次购买（Rebuy）", "https://www.rebuyengine.com", "Shopify 个性化 / 加购", "北美", "AI 推荐与个性化引擎，工程团队公开使用 Claude Code", "https://developers.rebuyengine.com/guides/ai-integration/", "Shopify 转化层，产品默认模型待确认"),
    ("AfterShip", "爱客科技（AfterShip）", "https://www.aftership.com", "购后追踪 / 退货", "亚太 / 全球", "提供 Agentic 购后体验与 AI 签收、异常处理能力", "https://www.aftership.com/en/ai", "全球购后平台，底层模型供应商待确认"),
    ("Narvar", "纳瓦尔（Narvar）", "https://corp.narvar.com", "购后体验 / 退货", "北美", "NAVI 与 IRIS 智能体覆盖配送、退货和理赔", "https://corp.narvar.com/agentic-ai-post-purchase", "中上规模购后体验平台"),
    ("Happy Returns", "快乐退货（Happy Returns）", "https://happyreturns.com", "退货网络 / 购后", "北美", "Agentic Returns MCP 可由 AI 助手查询订单并提交退货", "https://developer.happyreturns.com/guides/agentic-returns/", "PayPal 旗下退货网络"),
    ("parcelLab", "包裹实验室（parcelLab）", "https://parcellab.com", "购后沟通 / AI 智能体", "欧洲", "发布面向订单问询、退货与异常处理的零售购后 AI 智能体", "https://parcellab.com/press/parcellab-debuts-industry-defining-ai-agents-at-shoptalk-spring-2025/", "欧洲购后体验平台，模型供应商待核验"),
    ("Pacvue", "帕克维（Pacvue）", "https://pacvue.com", "零售媒体 / 电商广告", "北美", "Pacvue MCP 向 LLM 暴露跨零售商的零售媒体数据", "https://pacvue.com/platform/mcp-data-centralization/", "电商广告运营与数据平台"),
    ("Malomo", "马洛莫（Malomo）", "https://gomalomo.com", "履约体验 / 包裹追踪", "北美", "可经 MCP 集成层被 AI 助手读写订单、客户和物流事件", "https://truto.one/blog/connect-malomo-to-chatgpt-manage-orders-customers-events/", "Shopify 履约体验平台，MCP 多由集成商提供"),
    ("Zipchat", "快捷聊天（Zipchat）", "https://www.zipchat.ai", "电商 AI 聊天 / 全渠道", "欧洲 / 全球", "电商智能体使用 OpenAI 与 Anthropic 等第三方模型", "https://www.zipchat.ai/", "Shopify 聊天平台，多模型"),
    ("Ada", "艾达客服（Ada）", "https://www.ada.cx", "客服 AI 平台（含零售 / DTC）", "北美", "基于 OpenAI API 重建 AI Agent，服务零售与 DTC 客户", "https://openai.com/index/ada/", "面向中大型企业销售的客户体验平台"),
    ("eesel AI", "伊泽尔 AI（eesel AI）", "https://www.eesel.ai", "电商客服 AI（Gorgias）", "全球英文", "Gorgias 与 Shopify 智能体，子处理方包含 OpenAI 与 Anthropic", "https://www.eesel.ai/customers/question-beauty", "电商客户体验工具，模型子处理方明确"),
    ("Permut", "佩穆特（Permut）", "https://www.permut.com", "电商客服 AI 智能体", "欧洲", "Shopify 与 Gorgias 自主客服智能体，子处理方包含 OpenAI", "https://www.permut.com/en/subprocessors", "西班牙电商客户体验平台"),
    ("FERMÀT", "费马商务（FERMÀT）", "https://www.fermatcommerce.com", "AI 原生商务体验", "北美", "AI 商务平台及 Pierre 智能体，服务多个消费品牌", "https://www.prnewswire.com/news-releases/fermat-raises-45m-series-b-to-define-the-future-of-ai-powered-commerce-experiences-302478623.html", "电商体验 SaaS，模型供应商待核验"),
    ("Respond.io", "响应云（Respond.io）", "https://respond.io", "全渠道消息 / AI 智能体", "亚太", "面向 WhatsApp、Instagram、TikTok 的 B2C AI 智能体", "https://respond.io", "马来西亚起源的全渠道消息平台"),
    ("Connectly", "连接商务（Connectly）", "https://connectly.ai", "对话式商务", "北美", "面向零售与电商的 WhatsApp、Instagram、RCS 对话式商务", "https://connectly.ai", "商务消息平台，第三方模型待核验"),
    ("Vambe", "万贝（Vambe）", "https://www.vambe.ai", "对话式商务", "拉美", "Claude 为拉美企业驱动 WhatsApp 与 Instagram 销售智能体", "https://claude.com/customers/vambe", "智利起源，英语产品资料可用"),
    ("Clerk.io", "克拉克电商（Clerk.io）", "https://www.clerk.io", "电商搜索 / 推荐", "欧洲", "电商搜索、推荐与聊天，主打自有 AI 商品发现", "https://www.clerk.io", "欧洲电商搜索平台，外部模型采购待核验"),
    ("Klevu", "克莱武（Klevu）", "https://www.klevu.com", "电商搜索 / 选品", "欧洲", "面向 Shopify 与 BigCommerce 的 AI 搜索与商品发现", "https://www.klevu.com", "欧洲电商搜索平台，生成式模型供应商待核验"),
    ("Faire", "费尔批发（Faire）", "https://www.faire.com", "批发电商市场", "北美", "工程团队公开使用 OpenAI Assistants 构建代码审查与 RAG", "https://craft.faire.com/automated-code-reviews-with-llms-cf2cc51bb6d3", "工程侧 LLM 采购信号强"),
    ("Pepper", "佩珀内衣（Pepper）", "https://www.wearpepper.com", "DTC 内衣 / 贴身衣物", "北美", "Gorgias AI Agent Penelope 自动化约 54% 支持会话", "https://www.gorgias.com/customers/pepper", "通过 Gorgias 间接使用多模型能力"),
    ("Cocorico", "可可里科（Cocorico）", "https://www.cocorico.store", "DTC 服饰（法国制造）", "欧洲", "Gorgias AI Agent 在约六个月内自动化近半工单", "https://www.gorgias.com/customers/cocorico", "法国制造服饰品牌，Shopify 与 Gorgias AI 用户"),
    ("Question Beauty", "问号美妆（Question Beauty）", "https://questionbeauty.com", "DTC 美妆", "北美", "eesel AI 接入 Gorgias 与 Shopify，处理自动回复、查单和产品检索", "https://www.eesel.ai/customers/question-beauty", "通过 eesel 使用 LLM 的美妆品牌"),
    ("Snag", "斯纳格袜装（Snag）", "https://snagtights.com", "DTC 袜类 / 服饰", "欧洲", "使用 Claude 与 Patchworks 跨多个 Shopify 店和业务系统做自然语言运营", "https://patchworks.io/case-studies/snag/", "中型 DTC，Claude 运营智能体案例"),
    ("Redmond Life", "雷德蒙生活（Redmond Life）", "https://living.redmond.com", "DTC 食品 / 健康", "北美", "基于 Shopify Storefront MCP 与 Anthropic Claude 自建商务智能体", "https://www.shopify.com/case-studies/redmond", "自建 Claude API 商务智能体"),
    ("Naked Wardrobe", "裸衣橱（Naked Wardrobe）", "https://nakedwardrobe.com", "DTC 塑形 / 街头服饰", "北美", "Klaviyo Customer Agent 在约 90 天内自主解决大部分客户问询与推荐请求", "https://www.klaviyo.com/customers/case-studies/naked-wardrobe", "通过 Klaviyo 多模型栈使用 AI"),
    ("Scuffers", "斯卡弗斯（Scuffers）", "https://scuffers.com", "DTC 街头服饰", "欧洲", "Permut AI 处理数万次客服会话并节省支持工时", "https://www.permut.com/en/customers/scuffers", "西班牙 DTC，通过 Permut 使用 OpenAI"),
    ("TRAMAS+", "特拉玛斯家纺（TRAMAS+）", "https://www.tramasmas.com", "DTC 家纺 / 睡衣", "欧洲", "Permut AI 解决约 74% 客服会话", "https://www.permut.com/en/customers/tramas-mas", "西班牙家纺 DTC，使用生成式 AI 助手"),
    ("Dollar Shave Club", "美元剃须俱乐部（Dollar Shave Club）", "https://www.dollarshaveclub.com", "DTC 个护订阅", "北美", "Kodif 的 Claude on Bedrock 案例披露工单自动化", "https://kodif.ai/case-studies/dollar-shave-club/", "经 Kodif 采购 Claude 能力"),
    ("Glossier", "光泽美妆（Glossier）", "https://www.glossier.com", "DTC 美妆", "北美", "Yuma AI 与 Gorgias 自动化购后查询并提升响应速度", "https://yuma.ai/case-studies/a-glossier-touch-elevating-customer-experience-with-yuma-ai", "中上规模 DTC 美妆，Yuma 底层模型待核验"),
    ("Adore Me", "爱慕我（Adore Me）", "https://www.adoreme.com", "DTC 内衣", "北美", "Writer AI Studio 用于商品描述、市场 listing 与翻译", "https://writer.com/blog/adore-me-customer-story/", "Victoria's Secret 子公司，Writer 平台客户"),
    ("Revolve", "锐步时尚（Revolve）", "https://www.revolve.com", "在线时尚零售", "北美", "自研 AI 搜索、推荐、造型与客服辅助", "https://www.digitalcommerce360.com/2026/02/27/revolve-group-net-sales-ai-q4-2025/", "上市时尚电商，第三方模型供应商待核验"),
    ("Medusa", "美杜莎电商（Medusa）", "https://medusajs.com", "开源无头电商平台", "全球英文", "文档支持使用 LLM 与 AI 助手构建 agentic commerce", "https://docs.medusajs.com/learn/introduction/build-with-llms-ai", "开发者工具型客户，API 采购意向需进一步核验"),
]


# Filled from public sources. Empty X values are intentional: no reliable public
# founder account was found, so the importer must not invent one.
FOUNDERS = {
    "Gorgias": ("Romain Lapeyre", "https://x.com/Romain_Lapeyre", 99, "https://www.gorgias.com/author/romain-lapeyre"),
    "Triple Whale": ("AJ Orbach", "https://x.com/AY_Orbach", 95, "https://www.triplewhale.com/expert/aj-orbach"),
    "Tidio": ("Tytus Gołas", "", 95, "https://www.tidio.com/people/tytus-golas/"),
    "Reversia": ("Anatole Rozan", "https://x.com/anatolerozan", 97, "https://www.linkedin.com/in/anatole-rozan/"),
    "Klaviyo": ("Andrew Bialecki", "https://x.com/abialecki", 99, "https://www.klaviyo.com/blog/author/andrew-bialecki"),
    "Loop Returns": ("Jonathan Poma", "https://x.com/pomajp", 99, "https://www.loopreturns.com/blog/origin-story/"),
    "Attentive": ("Brian Long", "https://x.com/BrianCLong", 97, "https://theorg.com/org/attentive/org-chart/brian-long"),
    "Postscript": ("Alex Beller", "https://x.com/ringmybeller", 99, "https://www.ycombinator.com/companies/postscript"),
    "ShipBob": ("Dhruv Saxena; Divey Gulati", "", 97, "https://www.linkedin.com/company/shipbob"),
    "Kodif": ("Chyngyz Dzhumanazarov", "https://x.com/cdzhuman", 97, "https://theorg.com/org/kodif-1/org-chart/chyngyz-dzhumanazarov"),
    "Stay AI": ("Gina Perrelli", "https://x.com/ginaperrelli", 99, "https://stay.ai/about/"),
    "Recharge": ("Oisin O'Connor", "", 98, "https://www.linkedin.com/in/oisino/"),
    "Chatbase": ("Yasser Elsaid", "https://x.com/yasser_elsaid_", 98, "https://yespress.io/yasser-elsaid"),
    "BigCommerce": ("Eddie Machaalani", "https://x.com/eddiemachaalani", 95, "https://mixergy.com/interviews/bigcommerce-with-mitchell-harper/"),
    "Commerce Layer": ("Filippo Conforti", "https://x.com/FilippoConforti", 91, "https://filippoconforti.com/"),
    "Nosto": ("Juha Valvanne", "https://x.com/juhavalvanne", 99, "https://x.com/NostoSolutions/status/781523117629960192"),
    "Yotpo": ("Tomer Tagrin", "https://x.com/ttagrin", 97, "https://www.crunchbase.com/person/tomer-tagrin"),
    "Okendo": ("Matt Goodman", "", 96, "https://www.crunchbase.com/person/matt-goodman"),
    # The likely Junip X profile could not be tied to the company strongly enough.
    "Junip": ("Stuart Arsenault", "", 86, "https://www.linkedin.com/in/stuartarsenault/"),
    "Rebuy": ("John Erck", "https://x.com/johnerck", 99, "https://www.linkedin.com/in/johnerck/"),
    "AfterShip": ("Teddy Chan", "https://x.com/teddylun", 98, "https://teddy.aftership.com/about/"),
    "Narvar": ("Amit Sharma", "https://x.com/amitbol", 99, "https://corp.narvar.com/authors/amit-sharma"),
    "Happy Returns": ("David Sobie; Mark Geller", "", 97, "https://mucker.com/mentor/david-sobie/"),
    "parcelLab": ("Tobias Buxhoidt", "", 98, "https://www.crunchbase.com/person/tobias-buxhoidt"),
    "Pacvue": ("Melissa Burdick", "", 98, "https://pacvue.com/about/about-us/"),
    "Malomo": ("Yaw Aning", "https://x.com/YawA", 90, "https://harlem.capital/hcp-founder-spotlight-malomo/"),
    "Zipchat": ("Michael Monin", "", 91, "https://promptsninja.com/zipchat-interview/"),
    "Ada": ("Mike Murchison", "https://x.com/mimurchison", 94, "https://www.ada.cx/blog/q-and-a-with-mike-murchison-ada-s-co-founder-and-ceo/"),
    "eesel AI": ("Amogh Sarda", "", 97, "https://www.airtree.vc/companies/eesel"),
    "Permut": ("Alex Lopera", "https://x.com/alopera10", 99, "https://www.linkedin.com/in/alexlopera/"),
    # Founder identity is verified; the historical/current X handle is ambiguous.
    "FERMÀT": ("Rishabh Jain", "", 88, "https://www.linkedin.com/in/rishabhmjain/"),
    "Respond.io": ("Gerardo Salandra", "", 99, "https://respond.io/author/gerardo-salandra"),
    "Connectly": ("Stefanos Loukakos", "", 97, "https://www.linkedin.com/in/sloukakos/"),
    "Vambe": ("Nicolás Camhi; Matías Pérez; Diego Chahuán", "", 94, "https://dplnews.com/vambe-serie-a-14-mdd-escalar-plataforma-ia-empresas-b2c/"),
    # This X account matches the founder's name, but has no visible company bio.
    "Clerk.io": ("Hans-Kristian Bjerregaard", "", 88, "https://www.clerk.io/blog/how-did-clerk-io-get-started"),
    "Klevu": ("Nilay Oza", "", 98, "https://www.crunchbase.com/person/nilay-oza"),
    "Faire": ("Max Rhodes", "https://x.com/MaxRhodesOK", 93, "https://alejandrocremades.com/max-rhodes-building-in-24-months-a-500m-business-by-addressing-small-retailers/"),
    "Pepper": ("Jaclyn Fu; Lia Winograd", "", 92, "https://www.forbes.com/profile/pepper/"),
    "Cocorico": ("Arthur Charle; Tom Charle; Colline Charle", "", 92, "https://www.cocorico.store/pages/la-marque"),
    "Question Beauty": ("Dasha", "", 96, "https://questionbeauty.com/pages/about-us-1"),
    "Snag": ("Brie Read", "", 96, "https://www.telegraph.co.uk/connect/small-business/snag-tights-key-success-preparing-worst/"),
    "Redmond Life": ("Milo Bosshardt; Lamar Bosshardt", "", 98, "https://www.forbes.com/companies/redmond/"),
    "Naked Wardrobe": ("Shirin Kaviani; Shida Kaviani; Shideh Kaviani", "", 98, "https://fashionista.com/2025/04/naked-wardrobe-fashionista-network"),
    "Scuffers": ("Jaime Cruz; Javier López", "", 96, "https://www.esquire.com/es/moda-hombre/a46195856/scuffers-entrevista-jaime-cruz/"),
    # Kept at a lower score because only a secondary interview result was found.
    "TRAMAS+": ("Alberto Hernández", "", 80, "公开采访检索结果，需二次核验"),
    "Dollar Shave Club": ("Michael Dubin", "https://x.com/mrdubin", 97, "https://www.crunchbase.com/person/michael-ryan-dubin"),
    "Glossier": ("Emily Weiss", "https://x.com/EmilyWWeiss", 99, "https://www.linkedin.com/in/emily-weiss-49014571/"),
    "Adore Me": ("Morgan Hermand-Waiche", "https://x.com/morganhermand", 99, "https://www.alumni.hbs.edu/stories/Pages/story-bulletin.aspx?num=3228"),
    "Revolve": ("Michael Mente; Mike Karanikolas", "", 99, "https://www.businessoffashion.com/people/michael-mente-mike-karanikolas/"),
    "Medusa": ("Sebastian Rindom", "https://x.com/sebrindom", 94, "https://survivejs.com/blog/medusa-interview/"),
}


def domain(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def merge_note(existing: str | None, addition: str) -> str:
    existing = (existing or "").strip()
    if SOURCE_CHANNEL in existing:
        return existing
    return f"{existing}\n\n{addition}".strip()


def main() -> None:
    db = DatabaseManager()
    conn = get_connection(db.db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute("SELECT id, name, website, notes FROM companies").fetchall()
        by_domain = {domain(row["website"] or ""): row for row in rows if domain(row["website"] or "")}
        inserted = updated = leads_written = 0

        for name_en, display, website, industry, region, hook, evidence_url, note in COMPANIES:
            d = domain(website)
            company_note = (
                f"{SOURCE_CHANNEL} | 英文原名：{name_en} | 中文名：{display} | "
                f"AI/API 信号：{hook} | 销售判断：{note} | 核验日期：{VERIFIED_ON}"
            )
            models = ", ".join(
                model for model in ("Claude", "OpenAI", "Gemini", "MCP")
                if model.lower() in hook.lower()
            )
            existing = by_domain.get(d) or by_domain.get(DOMAIN_ALIASES.get(d, ""))
            if existing:
                company_id = existing["id"]
                conn.execute(
                    """UPDATE companies
                       SET industry=?, city_or_region=?, discovery_url=?,
                           qualification_evidence=?, models_used=?,
                           notes=?, updated_at=CURRENT_TIMESTAMP
                       WHERE id=?""",
                    (
                        industry, region, evidence_url, hook, models,
                        merge_note(existing["notes"], company_note), company_id,
                    ),
                )
                updated += 1
            else:
                cur = conn.execute(
                    """INSERT INTO companies
                       (name, country_type, industry, scale, website, city_or_region,
                        source_channel, client_tier, has_image_heavy_demand, notes,
                        discovery_url, customer_type, models_used,
                        qualification_evidence, prospect_status)
                       VALUES (?, 'overseas', ?, ?, ?, ?, ?, 'B', 1, ?, ?, ?, ?, ?, 'pending')""",
                    (
                        display, industry, "企业规模待进一步核验", website, region,
                        SOURCE_CHANNEL, company_note, evidence_url,
                        "API / AI 应用客户", models, hook,
                    ),
                )
                company_id = cur.lastrowid
                by_domain[d] = {
                    "id": company_id, "name": display, "website": website,
                    "notes": company_note,
                }
                inserted += 1

            founder = FOUNDERS.get(name_en)
            if not founder:
                continue
            founder_name, x_url, confidence, founder_source = founder
            current = conn.execute(
                """SELECT id FROM leads
                   WHERE company_id=? AND lower(contact_name)=lower(?)""",
                (company_id, founder_name),
            ).fetchone()
            source_method = f"创始人及 X 公开资料核验：{founder_source}"
            if current:
                conn.execute(
                    """UPDATE leads
                       SET job_title='创始人 / Founder', contact_role_category='创始人',
                           x_url=?, confidence_score=?, source_method=?,
                           updated_at=CURRENT_TIMESTAMP
                       WHERE id=?""",
                    (x_url, confidence, source_method, current["id"]),
                )
            else:
                conn.execute(
                    """INSERT INTO leads
                       (company_id, contact_name, job_title, contact_role_category,
                        x_url, confidence_score, source_method)
                       VALUES (?, ?, '创始人 / Founder', '创始人', ?, ?, ?)""",
                    (company_id, founder_name, x_url, confidence, source_method),
                )
            leads_written += 1

        conn.commit()
        print(
            f"companies={len(COMPANIES)} inserted={inserted} updated={updated} "
            f"founder_leads={leads_written}"
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
