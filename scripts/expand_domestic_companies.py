import sys
from pathlib import Path
from urllib.parse import urlparse
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.database import DatabaseManager, get_connection

# 公开官网与品牌/集团主体。按官网主域名和现有 CRM 去重。
COMPANIES = [
('深圳市大疆创新科技有限公司（DJI 大疆）','https://www.dji.com/cn','深圳','消费电子/影像设备'),
('小米科技有限责任公司（Xiaomi）','https://www.mi.com','北京','消费电子/智能家居'),
('华为终端有限公司（Huawei Consumer）','https://consumer.huawei.com/cn','深圳','消费电子/智能终端'),
('荣耀终端股份有限公司（HONOR）','https://www.honor.com/cn','深圳','消费电子/智能终端'),
('维沃移动通信有限公司（vivo）','https://www.vivo.com.cn','东莞','消费电子/智能终端'),
('OPPO 广东移动通信有限公司','https://www.oppo.com/cn','东莞','消费电子/智能终端'),
('深圳市万普拉斯科技有限公司（OnePlus）','https://www.oneplus.com/cn','深圳','消费电子/智能终端'),
('真我移动通信有限公司（realme）','https://www.realme.com/cn','深圳','消费电子/智能终端'),
('珠海市魅族科技有限公司（MEIZU）','https://www.meizu.com','珠海','消费电子/智能终端'),
('联想集团有限公司（Lenovo）','https://www.lenovo.com.cn','北京','消费电子/电脑设备'),
('云鲸智能创新（深圳）有限公司','https://www.narwal.com','深圳','智能家电/清洁机器人'),
('添可智能科技有限公司（Tineco）','https://www.tineco.com','苏州','智能家电/清洁电器'),
('杭州老板电器股份有限公司','https://www.robam.com','杭州','厨房家电'),
('宁波方太厨具有限公司','https://www.fotile.com','宁波','厨房家电'),
('美的集团股份有限公司','https://www.midea.com.cn','佛山','综合家电'),
('海尔集团公司','https://www.haier.com','青岛','综合家电'),
('海信集团控股股份有限公司','https://www.hisense.com','青岛','综合家电/显示设备'),
('TCL 科技集团股份有限公司','https://www.tcl.com','惠州','消费电子/显示设备'),
('创维集团有限公司（Skyworth）','https://www.skyworth.com','深圳','消费电子/显示设备'),
('奥克斯集团有限公司（AUX）','https://www.auxgroup.com','宁波','综合家电'),
('珠海格力电器股份有限公司','https://www.gree.com','珠海','综合家电'),
('九阳股份有限公司（Joyoung）','https://www.joyoung.com','济南','厨房小家电'),
('小熊电器股份有限公司','https://www.bear.com.cn','佛山','厨房小家电'),
('浙江苏泊尔股份有限公司','https://www.supor.com.cn','杭州','炊具/厨房家电'),
('广东德尔玛科技股份有限公司','https://www.deerma.com','佛山','生活小家电'),
('深圳市正浩创新科技股份有限公司（EcoFlow）','https://www.ecoflow.com/cn','深圳','户外电源/储能'),
('深圳市征拓科技有限公司（Zendure）','https://www.zendure.com','深圳','户外电源/储能'),
('深圳市德兰明海新能源股份有限公司（BLUETTI）','https://www.bluettipower.cn','深圳','户外电源/储能'),
('深圳市韶音科技有限公司（Shokz）','https://www.shokz.com.cn','深圳','音频穿戴设备'),
('深圳市漫步者科技股份有限公司（EDIFIER）','https://www.edifier.com.cn','深圳','音频设备'),
('珀莱雅化妆品股份有限公司','https://www.proya-group.com','杭州','美妆护肤'),
('华熙生物科技股份有限公司','https://www.bloomagebiotech.com','北京','美妆护肤/生物科技'),
('云南贝泰妮生物科技集团股份有限公司','https://www.botanee.com.cn','昆明','美妆护肤'),
('伽蓝（集团）股份有限公司（自然堂）','https://www.jala.com.cn','上海','美妆护肤'),
('上海上美化妆品股份有限公司（韩束）','https://www.chicmaxgroup.com','上海','美妆护肤'),
('上海百雀羚日用化学有限公司','https://www.pechoin.com','上海','美妆护肤'),
('广东丸美生物技术股份有限公司','https://www.marubi.cn','广州','美妆护肤'),
('水羊集团股份有限公司','https://www.syounggroup.com','长沙','美妆电商/品牌管理'),
('毛戈平化妆品股份有限公司','https://www.maogeping.com','杭州','彩妆'),
('广州谷雨生物科技有限公司（谷雨）','https://www.guyuskin.com','广州','美妆护肤'),
('美尚（广州）化妆品股份有限公司（Colorkey）','https://www.colorkey.com','广州','彩妆'),
('上海橘宜化妆品有限公司（橘朵 Judydoll）','https://www.judydoll.com','上海','彩妆'),
('杭州花知晓电子商务有限公司（Flower Knows）','https://flowerknows.co','杭州','彩妆/跨境电商'),
('丽知（上海）品牌管理有限公司（INTO YOU）','https://www.intoyoucosmetics.com','上海','彩妆'),
('上海逐本生物科技有限公司','https://www.zhuben.com','上海','美妆护肤'),
('杭州诗裴丝化妆品有限公司（SPES）','https://www.spes.cn','杭州','洗护个护'),
('杭州白贝壳实业股份有限公司（Babycare）','https://www.babycare.com','杭州','母婴消费品牌'),
('杭州可优比母婴用品有限公司（KUB）','https://www.kub.cn','杭州','母婴消费品牌'),
('深圳全棉时代科技有限公司','https://www.purcotton.com','深圳','母婴/生活用品'),
('润本生物技术股份有限公司','https://www.runben.com','广州','母婴/个护用品'),
('波司登国际服饰（中国）有限公司','https://www.bosideng.com','上海','服饰/羽绒服'),
('宁波太平鸟时尚服饰股份有限公司','https://www.peacebird.com','宁波','时尚服饰'),
('浙江森马服饰股份有限公司','https://www.semir.com','温州','时尚服饰'),
('快尚时装（广州）有限公司（URBAN REVIVO）','https://www.urbanrevivo.com','广州','时尚服饰/跨境'),
('李宁（中国）体育用品有限公司','https://www.lining.com','北京','运动服饰'),
('安踏体育用品集团有限公司','https://www.anta.com','厦门','运动服饰'),
('特步（中国）有限公司','https://www.xtep.com.cn','厦门','运动服饰'),
('三六一度（中国）有限公司','https://www.361sport.com','厦门','运动服饰'),
('骆驼（中国）户外用品有限公司（CAMEL）','https://www.camel.com.cn','广州','户外服饰'),
('深圳减字科技有限公司（蕉内 Bananain）','https://www.bananain.com','深圳','服饰/内衣'),
('彼悦（北京）科技有限公司（Ubras）','https://www.ubras.com','北京','服饰/内衣'),
('上海睿秀电子商务有限公司（内外 NEIWAI）','https://www.neiwai.life','上海','服饰/内衣'),
('玛伊娅服饰（上海）有限公司（MAIA ACTIVE）','https://www.maiaactive.com','上海','运动服饰'),
('蕉下控股有限公司（Beneunder）','https://www.beneunder.com','深圳','户外服饰'),
('杭州必是特服装有限公司（BEASTER）','https://www.beaster.com','杭州','潮流服饰'),
('海澜之家集团股份有限公司','https://www.hla.com.cn','无锡','时尚服饰'),
('福建七匹狼实业股份有限公司','https://www.septwolves.com','厦门','时尚服饰'),
('九牧王股份有限公司','https://www.joeone.com','泉州','时尚服饰'),
('雅戈尔时尚股份有限公司','https://www.youngor.com','宁波','时尚服饰'),
('深圳歌力思服饰股份有限公司','https://www.ellassay.com','深圳','高端时尚服饰'),
('农夫山泉股份有限公司','https://www.nongfuspring.com','杭州','食品饮料'),
('元气森林（北京）食品科技集团有限公司','https://www.genki.forest','北京','食品饮料'),
('四川茶姬企业管理有限公司（霸王茶姬）','https://www.chagee.com','成都','新式茶饮'),
('奈雪的茶控股有限公司','https://www.naixue.com','深圳','新式茶饮'),
('深圳美西西餐饮管理有限公司（喜茶 HEYTEA）','https://www.heytea.com','深圳','新式茶饮'),
('瑞幸咖啡（中国）有限公司','https://www.luckincoffee.com','厦门','咖啡饮品'),
('东鹏饮料（集团）股份有限公司','https://www.szeastroc.com','深圳','食品饮料'),
('绝味食品股份有限公司','https://www.juewei.cn','长沙','休闲食品'),
('三只松鼠股份有限公司','https://www.3songshu.com','芜湖','休闲食品/电商'),
('良品铺子股份有限公司','https://www.lppz.com','武汉','休闲食品/电商'),
('洽洽食品股份有限公司','https://www.qiaqiafood.com','合肥','休闲食品'),
('内蒙古伊利实业集团股份有限公司','https://www.yili.com','呼和浩特','乳制品'),
('内蒙古蒙牛乳业（集团）股份有限公司','https://www.mengniu.com.cn','呼和浩特','乳制品'),
('中国飞鹤有限公司','https://www.feihe.com','北京/齐齐哈尔','乳制品/母婴'),
('上海旺旺食品集团有限公司','https://www.want-want.com','上海','食品饮料'),
('杭州饱嗝电子商务有限公司（王饱饱）','https://www.wangbaobao.com','杭州','食品/新消费'),
('上海三顿半咖啡有限公司','https://www.saturnbird.com','上海','咖啡/新消费'),
('自嗨锅（重庆）食品有限公司','https://www.zihaiguo.com','重庆','方便食品/新消费'),
('林氏家居股份有限公司','https://www.linshimuye.com','佛山','家具家居/电商'),
('顾家家居股份有限公司','https://www.kukahome.com','杭州','家具家居'),
('敏华控股有限公司（芝华仕 CHEERS）','https://www.manwahholdings.com','香港/惠州','家具家居'),
('欧派家居集团股份有限公司','https://www.oppein.com','广州','定制家居'),
('金牌厨柜家居科技股份有限公司','https://www.goldenhome.cc','厦门','定制家居'),
('志邦家居股份有限公司','https://www.zbom.com','合肥','定制家居'),
('乐歌人体工学科技股份有限公司','https://www.loctek.com','宁波','智能家居/办公'),
('青岛易来智能科技股份有限公司（Yeelight）','https://www.yeelight.com','青岛','智能照明'),
('名创优品（广州）有限责任公司（MINISO）','https://www.miniso.com','广州','生活方式零售'),
('北京乐自天成文化发展股份有限公司（52TOYS）','https://www.52toys.com','北京','潮玩/IP'),
('奥飞娱乐股份有限公司','https://www.gdalpha.com','广州','玩具/IP内容'),
('上海卡游科技有限公司','https://www.kayou110.com','上海','卡牌/IP消费品'),
]
assert len(COMPANIES) == 100, len(COMPANIES)

SCENES = {
'消费电子':'围绕一个已授权 SKU 批量生成多渠道商品场景图和短视频分镜，保持产品结构与参数准确',
'家电':'围绕一个已授权 SKU 批量生成家居使用场景和大促素材变体，先验证细节准确率',
'美妆':'围绕一款已授权产品生成促销场景图与多版式素材，严格保持包装文字和色号准确',
'母婴':'围绕一个已授权产品生成电商场景图，避免未经证实的功效、医学和婴幼儿安全表达',
'服饰':'使用已授权商品图生成多场景穿搭和多渠道版式，先验证服装版型、纹理与颜色准确度',
'食品':'围绕一款已授权产品生成节日促销和生活场景素材，保持包装、配料与规格信息准确',
'茶饮':'围绕新品和节日活动批量生成门店、电商与社媒视觉变体，保持产品外观和活动文字准确',
'咖啡':'围绕新品和节日活动批量生成电商与社媒视觉变体，保持产品外观和活动文字准确',
'家具':'使用已授权产品图生成不同户型和风格的家居场景，保持尺寸、材质和结构准确',
'家居':'使用已授权产品图生成不同空间和风格的场景素材，保持尺寸、材质和结构准确',
'潮玩':'围绕已授权 IP 和商品生成上新海报与短视频分镜，不扩展未授权角色设定',
'玩具':'围绕已授权 IP 和商品生成上新海报与短视频分镜，不扩展未授权角色设定',
'零售':'为新品和活动批量生成商品陈列、社媒和广告视觉变体，先做品牌审核',
}

def domain(url):
    return (urlparse(url).hostname or '').lower().removeprefix('www.')

def scene_for(industry):
    for key, value in SCENES.items():
        if key in industry:
            return value
    return '围绕已授权商品批量生成电商、广告和社媒视觉变体，以审核通过率和修改时间评估价值'

db = DatabaseManager()
con = get_connection(db.db_path)
try:
    before = con.execute("SELECT count(*) FROM companies WHERE country_type='domestic'").fetchone()[0]
    existing_domains = {domain(r[0]) for r in con.execute("SELECT website FROM companies WHERE website IS NOT NULL AND website != ''")}
    existing_names = {r[0] for r in con.execute('SELECT name FROM companies')}
    con.execute('BEGIN IMMEDIATE')
    inserted = []
    for name, website, region, industry in COMPANIES:
        d = domain(website)
        if name in existing_names or d in existing_domains:
            raise RuntimeError(f'与现有数据库重复: {name} / {d}')
        scene = scene_for(industry)
        role = '待定位：电商视觉负责人、品牌内容负责人或数字营销负责人'
        notes = f'国内公开来源补充（2026-10-14） | 具体切入场景: {scene} | 建议联系角色: {role} | 预算、个人负责人、素材产量和采购时机待核验'
        evidence = f'企业公开官网展示其“{industry}”相关品牌、产品或服务；官网用于企业主体及主营方向初步核验，具体内容产量与采购意愿仍需人工确认。'
        cur = con.execute("""INSERT INTO companies
            (name,country_type,industry,scale,website,city_or_region,source_channel,client_tier,has_image_heavy_demand,notes,discovery_url,customer_type,qualification_evidence,prospect_status)
            VALUES (?,?,?,?,?,?,?,?,1,?,?,?,?,?)""",
            (name,'domestic',industry,'公开规模待量化',website,region,'国内公开来源扩充（2026-10-14）','B',notes,website,'电商商拍客户',evidence,'pending'))
        inserted.append(cur.lastrowid)
        existing_names.add(name); existing_domains.add(d)
    con.commit()
    after = con.execute("SELECT count(*) FROM companies WHERE country_type='domestic'").fetchone()[0]
    marked = con.execute("SELECT count(*) FROM companies WHERE source_channel='国内公开来源扩充（2026-10-14）'").fetchone()[0]
    print({'before': before, 'inserted': len(inserted), 'after': after, 'marked': marked, 'id_range': [min(inserted), max(inserted)]})
    assert after == before + 100 and marked == 100
except Exception:
    con.rollback()
    raise
finally:
    con.close()
