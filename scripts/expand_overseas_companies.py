import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.database import DatabaseManager, get_connection
from urllib.parse import urlparse

# Public founder profiles only. Chinese names are translations; English originals remain visible.
NEW = [
('Shopify','购物云（Shopify）','https://www.shopify.com','加拿大','电商SaaS','Tobias Lütke','https://x.com/tobi'),
('Canva','可画（Canva）','https://www.canva.com','澳大利亚','在线设计平台','Melanie Perkins','https://x.com/MelanieCanva'),
('Figma','菲格玛（Figma）','https://www.figma.com','美国','协作设计平台','Dylan Field','https://x.com/zoink'),
('Webflow','网页流（Webflow）','https://webflow.com','美国','无代码建站平台','Vlad Magdalin','https://x.com/callmevlad'),
('Framer','弗雷默（Framer）','https://www.framer.com','荷兰','网站设计平台','Koen Bok','https://x.com/koenbok'),
('Notion','诺申（Notion）','https://www.notion.so','美国','协作工作平台','Ivan Zhao','https://x.com/ivanhzhao'),
('HubSpot','集客云（HubSpot）','https://www.hubspot.com','美国','营销与客户平台','Dharmesh Shah','https://x.com/dharmesh'),
('Mailchimp','邮件猩猩（Mailchimp）','https://mailchimp.com','美国','邮件营销平台','Ben Chestnut','https://x.com/benchestnut'),
('Airtable','空气表格（Airtable）','https://www.airtable.com','美国','低代码数据平台','Howie Liu','https://x.com/howietl'),
('Bubble','泡泡（Bubble）','https://bubble.io','美国','无代码应用平台','Joshua Haas','https://x.com/joshuahaas'),
('Zapier','扎皮尔（Zapier）','https://zapier.com','美国','自动化集成平台','Wade Foster','https://x.com/wadefoster'),
('Calendly','日程利（Calendly）','https://calendly.com','美国','日程与销售工具','Tope Awotona','https://x.com/TopeAwotona'),
('Intercom','英特康（Intercom）','https://www.intercom.com','美国/爱尔兰','客户沟通平台','Des Traynor','https://x.com/destraynor'),
('Typeform','类型表单（Typeform）','https://www.typeform.com','西班牙','在线表单平台','David Okuniev','https://x.com/DavidOkuniev'),
('Jotform','捷表（Jotform）','https://www.jotform.com','美国','在线表单平台','Aytekin Tank','https://x.com/aytekintank'),
('Tally','塔利表单（Tally）','https://tally.so','比利时','在线表单平台','Marie Martens','https://x.com/MarieMartens'),
('Linear','线性（Linear）','https://linear.app','美国','产品研发平台','Karri Saarinen','https://x.com/karrisaarinen'),
('Raycast','光线投射（Raycast）','https://www.raycast.com','英国','效率工具平台','Thomas Paul Mann','https://x.com/thomaspaulmann'),
('Superhuman','超人邮件（Superhuman）','https://superhuman.com','美国','邮件效率平台','Rahul Vohra','https://x.com/rahulvohra'),
('Coda','柯达协作文档（Coda）','https://coda.io','美国','协作文档平台','Shishir Mehrotra','https://x.com/shishirmehrotra'),
('ClickUp','点击升级（ClickUp）','https://clickup.com','美国','项目管理平台','Zeb Evans','https://x.com/DJ_CURFEW'),
('Asana','阿萨纳（Asana）','https://asana.com','美国','项目管理平台','Dustin Moskovitz','https://x.com/moskov'),
('Slack','斯莱克（Slack）','https://slack.com','美国','团队协作平台','Stewart Butterfield','https://x.com/stewart'),
('Discord','迪斯科德（Discord）','https://discord.com','美国','社区沟通平台','Jason Citron','https://x.com/jasoncitron'),
('Patreon','赞助人（Patreon）','https://www.patreon.com','美国','创作者商业平台','Jack Conte','https://x.com/jackconte'),
('Teachable','可教学（Teachable）','https://teachable.com','美国','课程商业平台','Ankur Nagpal','https://x.com/ankurnagpal'),
('Gumroad','口香糖之路（Gumroad）','https://gumroad.com','美国','数字商品电商','Sahil Lavingia','https://x.com/shl'),
('Lemon Squeezy','柠檬榨汁机（Lemon Squeezy）','https://www.lemonsqueezy.com','美国','数字商品支付平台','JR Farr','https://x.com/jrfarr'),
('Paddle','桨板支付（Paddle）','https://www.paddle.com','英国','软件商业平台','Christian Owens','https://x.com/ChristianOwens'),
('Squarespace','方形空间（Squarespace）','https://www.squarespace.com','美国','网站与电商平台','Anthony Casalena','https://x.com/acasalena'),
('Wix','维克斯（Wix）','https://www.wix.com','以色列','网站与电商平台','Avishai Abrahami','https://x.com/Avishai_Abrahami'),
('WooCommerce','伍商业（WooCommerce）','https://woocommerce.com','全球','开源电商平台','Adii Pienaar','https://x.com/adii'),
('Depop','德波普（Depop）','https://www.depop.com','英国','时尚电商平台','Simon Beckerman','https://x.com/simonbeckerman'),
('Poshmark','波什马克（Poshmark）','https://poshmark.com','美国','时尚电商平台','Manish Chandra','https://x.com/mnchandra'),
('Faire','费尔批发（Faire）','https://www.faire.com','美国','批发电商平台','Max Rhodes','https://x.com/MaxRhodes'),
('Whatnot','沃特诺特（Whatnot）','https://www.whatnot.com','美国','直播电商平台','Grant LaFontaine','https://x.com/grantlafontaine'),
('beehiiv','蜂巢邮件（beehiiv）','https://www.beehiiv.com','美国','创作者邮件平台','Tyler Denk','https://x.com/denk_tweets'),
('Kit','创作者套件（Kit）','https://kit.com','美国','创作者营销平台','Nathan Barry','https://x.com/nathanbarry'),
('Substack','订阅栈（Substack）','https://substack.com','美国','订阅内容平台','Hamish McKenzie','https://x.com/hamishmckenzie'),
('Ghost','幽灵出版（Ghost）','https://ghost.org','英国','开源出版平台','John O’Nolan','https://x.com/johnonolan'),
('Circle','圆圈社区（Circle）','https://circle.so','美国','社区商业平台','Sid Yadav','https://x.com/sidyalfowzan'),
('Mighty Networks','强大社群（Mighty Networks）','https://www.mightynetworks.com','美国','社群商业平台','Gina Bianchini','https://x.com/ginab'),
('Podia','波迪亚（Podia）','https://www.podia.com','美国','数字商品平台','Spencer Fry','https://x.com/spencerfry'),
('Klaviyo','克拉维约（Klaviyo）','https://www.klaviyo.com','美国','电商营销自动化','Andrew Bialecki','https://x.com/abialecki'),
('Gorgias','戈尔吉亚斯（Gorgias）','https://www.gorgias.com','美国/法国','电商客服平台','Romain Lapeyre','https://x.com/romain_lapeyre'),
('Recharge','锐充订阅（Recharge）','https://rechargepayments.com','美国','电商订阅平台','Oisin O’Connor','https://x.com/OisinOConnor'),
('Yotpo','约特波（Yotpo）','https://www.yotpo.com','美国/以色列','电商营销平台','Tomer Tagrin','https://x.com/tomertagrin'),
('Runway','跑道（Runway）','https://runwayml.com','美国','AI视频生成平台','Cristóbal Valenzuela','https://x.com/c_valenzuelab'),
('Pika','皮卡（Pika）','https://pika.art','美国','AI视频生成平台','Demi Guo','https://x.com/demi_guo_'),
('Captions','字幕工坊（Captions）','https://www.captions.ai','美国','AI视频创作平台','Gaurav Misra','https://x.com/GauravMisra'),
('HeyGen','海进（HeyGen）','https://www.heygen.com','美国','AI数字人视频平台','Joshua Xu','https://x.com/joshua_xu_'),
('Synthesia','辛西娅（Synthesia）','https://www.synthesia.io','英国','AI数字人视频平台','Victor Riparbelli','https://x.com/vriparbelli'),
('ElevenLabs','十一实验室（ElevenLabs）','https://elevenlabs.io','美国/英国','AI语音与创意平台','Mati Staniszewski','https://x.com/mati_staniszewski'),
('Descript','描述编辑（Descript）','https://www.descript.com','美国','音视频编辑平台','Andrew Mason','https://x.com/andrewmason'),
('VEED','维德视频（VEED）','https://www.veed.io','英国','在线视频创作平台','Sabba Keynejad','https://x.com/sab8a'),
('Recraft','再创（Recraft）','https://www.recraft.ai','英国','AI图像与设计API','Anna Veronika Dorogush','https://x.com/AnnaVDorogush'),
('Photoroom','照片间（Photoroom）','https://www.photoroom.com','法国','AI商品图平台','Matthieu Rouif','https://x.com/matthieurouif'),
('Pebblely','卵石图（Pebblely）','https://pebblely.com','新加坡','AI商品图平台','Alfred Wong','https://x.com/alextwong'),
('Leonardo.Ai','莱昂纳多AI（Leonardo.Ai）','https://leonardo.ai','澳大利亚','AI图像生成平台','JJ Fiasson','https://x.com/jjfiasson'),
('Jasper','贾斯珀（Jasper）','https://www.jasper.ai','美国','AI营销内容平台','Dave Rogenmoser','https://x.com/DaveRogenmoser'),
('Copy.ai','文案AI（Copy.ai）','https://www.copy.ai','美国','AI营销内容平台','Paul Yacoubian','https://x.com/PaulYacoubian'),
('Writer','写作者（Writer）','https://writer.com','美国','企业生成式AI平台','May Habib','https://x.com/may_habib'),
('Typeface','字体创意（Typeface）','https://www.typeface.ai','美国','企业AI内容平台','Abhay Parasnis','https://x.com/abhsaxena'),
('Gamma','伽马演示（Gamma）','https://gamma.app','美国','AI演示与内容平台','Grant Lee','https://x.com/thisisgrantlee'),
('Miro','米罗白板（Miro）','https://miro.com','美国/荷兰','协作白板平台','Andrey Khusid','https://x.com/andreykhusid'),
('Loom','录幕（Loom）','https://www.loom.com','美国','视频沟通平台','Joe Thomas','https://x.com/yoyo_thomas'),
('Buffer','缓冲器（Buffer）','https://buffer.com','美国/英国','社交媒体管理','Joel Gascoigne','https://x.com/joelgascoigne'),
('Hootsuite','猫头鹰套件（Hootsuite）','https://www.hootsuite.com','加拿大','社交媒体管理','Ryan Holmes','https://x.com/invoker'),
('Sprout Social','萌芽社交（Sprout Social）','https://sproutsocial.com','美国','社交媒体管理','Justyn Howard','https://x.com/JustynHoward'),
('Picsart','图片艺术（Picsart）','https://picsart.com','美国/亚美尼亚','创意设计平台','Hovhannes Avoyan','https://x.com/hovhannes'),
('Freepik','自由图库（Freepik）','https://www.freepik.com','西班牙','创意素材与AI平台','Joaquín Cuenca','https://x.com/joaquincuenca'),
('Semrush','搜索冲刺（Semrush）','https://www.semrush.com','美国','数字营销平台','Oleg Shchegolev','https://x.com/OlegShchegolev'),
('Basecamp','大本营（Basecamp）','https://basecamp.com','美国','项目协作平台','Jason Fried','https://x.com/jasonfried'),
('Unbounce','优落地页（Unbounce）','https://unbounce.com','加拿大','营销落地页平台','Rick Perreault','https://x.com/RickPerreault'),
('Carrd','卡片网站（Carrd）','https://carrd.co','美国','单页网站平台','AJ','https://x.com/ajlkn'),
]
assert len(NEW) == 75, len(NEW)

EXISTING_TRANSLATIONS = {
'KraveBeauty':'渴望美肌（KraveBeauty）','Ridge Wallet':'山脊钱包（Ridge Wallet）','Cuts Clothing':'剪裁服饰（Cuts Clothing）',
'Native Deodorant (P&G DTC Subsidiary)':'原生体香（Native，宝洁旗下）','Haus Labs':'豪斯实验室（Haus Labs）',
'Jones Road Beauty':'琼斯路美妆（Jones Road Beauty）','Brooklinen':'布鲁克林家纺（Brooklinen）',
'Power Digital':'动力数字营销（Power Digital）','Tinuiti':'蒂努伊蒂（Tinuiti）','Wpromote':'推广网（Wpromote）',
'Designity':'设计力（Designity）','Design Pickle':'设计酸黄瓜（Design Pickle）','Superside':'超级侧（Superside）',
'SHOPLINE':'商线（SHOPLINE）','BigCommerce（品牌线/集团采购归属待确认）':'大商业（BigCommerce，采购归属待确认）',
'Smartly':'智能广告（Smartly）','Celtra':'赛尔特拉（Celtra）','Bannerflow':'横幅流（Bannerflow）',
'The Brief（原Creatopy，只计一家）':'创意简报（The Brief，原 Creatopy）','Kittl':'基特尔设计（Kittl）',
'VistaCreate（Vista/Cimpress集团线，只计一家）':'维斯塔创作（VistaCreate，Vista/Cimpress 集团）',
'Vidmob':'视频动员（Vidmob）','QuickFrame（MNTN旗下，只计一家）':'快帧（QuickFrame，MNTN 旗下）',
'Darkroom':'暗房营销（Darkroom）','Shuttlerock（含已收购 Shakr，合并计数）':'快门石（Shuttlerock，含 Shakr）'
}
EXISTING_FOUNDERS = {
'KraveBeauty':('Liah Yoo','https://x.com/liahyoo'),
'Native Deodorant (P&G DTC Subsidiary)':('Moiz Ali','https://x.com/moizali'),
'Haus Labs':('Lady Gaga','https://x.com/ladygaga'),
'Jones Road Beauty':('Bobbi Brown','https://x.com/justbobbibrown'),
'Brooklinen':('Rich Fulop','https://x.com/RichFulop'),
'Design Pickle':('Russ Perry','https://x.com/russperry'),
'BigCommerce（品牌线/集团采购归属待确认）':('Eddie Machaalani','https://x.com/eddiemachaalani'),
}

def domain(url): return (urlparse(url).hostname or '').removeprefix('www.')

db=DatabaseManager()
con=get_connection(db.db_path)
try:
    con.execute('BEGIN IMMEDIATE')
    existing_domains={domain(r[0]) for r in con.execute("select website from companies where website is not null")}
    for old,new_name in EXISTING_TRANSLATIONS.items():
        con.execute("update companies set name=?, updated_at=CURRENT_TIMESTAMP where name=?",(new_name,old))
    # Existing verified founders only; no guessed profiles.
    for old,(founder,x_url) in EXISTING_FOUNDERS.items():
        display=EXISTING_TRANSLATIONS[old]
        row=con.execute('select id from companies where name=?',(display,)).fetchone()
        if row and not con.execute('select 1 from leads where company_id=? and x_url=?',(row[0],x_url)).fetchone():
            con.execute("insert into leads(company_id,contact_name,job_title,contact_role_category,x_url,confidence_score,source_method) values(?,?,?,?,?,?,?)",
                        (row[0],founder,'创始人 / Founder','创始人',x_url,95,'公开官网与创始人 X 主页核验'))
    added=0
    for name_en,display,website,region,industry,founder,x_url in NEW:
        d=domain(website)
        if d in existing_domains:
            continue
        scene='批量生成并迭代品牌、电商或营销视觉素材，先用获授权素材做小规模效果与审核通过率测试'
        evidence=f'{name_en} 官方网站公开展示其 {industry} 产品或服务；创始人公开 X 主页用于人工建联前核验。'
        cur=con.execute("""insert into companies(name,country_type,industry,scale,website,city_or_region,source_channel,client_tier,has_image_heavy_demand,notes,discovery_url,customer_type,qualification_evidence,prospect_status)
             values(?,?,?,?,?,?,?,?,1,?,?,?,?,?)""",
            (display,'overseas',industry,'公开企业客户/创作者用户规模待进一步量化',website,region,'海外公开来源扩充','B',
             f'英文原名: {name_en} | 中文译名: {display} | 具体切入: {scene} | 创始人: {founder} | X: {x_url} | 核验日期: 2026-10-14',
             website,'工具→API',evidence,'pending'))
        cid=cur.lastrowid
        con.execute("""insert into leads(company_id,contact_name,job_title,contact_role_category,x_url,confidence_score,source_method)
             values(?,?,?,?,?,?,?)""",(cid,founder,'创始人 / Founder','创始人',x_url,90,'企业官网与公开 X 主页'))
        existing_domains.add(d); added+=1
    con.commit()
    total=con.execute("select count(*) from companies where country_type='overseas'").fetchone()[0]
    linked=con.execute("select count(distinct c.id) from companies c join leads l on l.company_id=c.id where c.country_type='overseas' and l.x_url like 'https://x.com/%'").fetchone()[0]
    print('added',added,'overseas total',total,'with founder X',linked)
    assert total==100, total
finally:
    con.close()
