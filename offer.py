"""Conservative title hints; account eligibility is always confirmed by a person."""
import re
import unicodedata

NEW_USER_WORDS = re.compile(r"新人|新客|新用户|首单|首购|首次下单|新注册|新人券|新客券")


def detected_offer_type(title):
    return "suspected_new_user" if NEW_USER_WORDS.search(title) else "unknown"


RESOURCE_LABELS = {'purchase':'商品报价', 'free_claim':'免费领取线索', 'shipping':'付邮领取',
                   'trial':'申请试用', 'lottery':'抽奖活动', 'points':'积分兑换',
                   'rebate':'先付后返', 'coupon':'优惠券／权益', 'campaign':'优惠会场',
                   'pending':'待识别优惠入口', 'unknown':'待分类'}
TOPIC_LABELS = {'home':'家用日用品', 'food':'食品餐饮', 'beauty':'个护美妆', 'baby':'母婴用品',
                'pet':'宠物用品', 'travel':'出行生活', 'digital':'会员与数字资源',
                'electronics':'手机数码', 'other':'其他资源'}


def product_subcategory(title, topic='other'):
    """Small display taxonomy for grouping; exact-SKU matching remains authoritative."""
    patterns = {
        'home': [('纸品','抽纸|卷纸|纸巾|湿巾|湿厕纸|湿纸巾|厨房纸|卫生纸'),
                 ('清洁巾与棉柔巾','洗脸巾|棉柔巾|柔巾|擦脸巾|清洁巾|卸妆巾'),
                 ('清洁洗护','洗衣|洗洁|清洁|垃圾袋|拖把|扫把|消毒'),
                 ('厨具餐具','锅|碗|盘|刀具|保鲜|水杯|餐具'),
                 ('家居五金与电工','开关插座|插座面板|电工|家居五金|水龙头|门锁|合页|铰链|螺丝|电源插座'),
                 ('家居收纳','收纳|置物|家居|床品|灯具')],
        'food': [('乳品','牛奶|酸奶|奶酪'), ('饮料冲调','饮料|咖啡|茶饮|冲饮|矿泉水'), ('零食粮油','零食|坚果|粮油|米面|调味'), ('罐头熟食','午餐肉|罐头|火腿肠|卤味|熟食'), ('生鲜食品','水果|果蔬|生鲜|肉类|海鲜|橙|苹果|香蕉|柑橘|橘子|柚子|葡萄|西瓜|桃子?|梨|草莓|蓝莓|荔枝|龙眼|哈密瓜|芒果|榴莲|樱桃|车厘子|菠萝|火龙果|石榴|山竹|枣|山楂')],
        'beauty': [('护肤','面霜|精华|护肤|面膜|防晒'), ('洗护','洗发|护发|沐浴|牙膏|洗手液'), ('彩妆香氛','彩妆|口红|香水')],
        'baby': [('纸尿裤与湿巾','尿裤|纸尿裤|湿巾'), ('奶粉辅食','奶粉|辅食|婴儿食品'), ('母婴用品','婴儿|母婴|童车|奶瓶')],
        'pet': [('宠物食品','猫粮|狗粮|主食|冻干|猫条'), ('猫砂清洁','猫砂|尿垫|除臭'), ('宠物用品','宠物|猫窝|狗窝|牵引')],
        'digital': [('会员权益','会员|PLUS|VIP|连续包月'), ('软件服务','软件|网盘|云服务|兑换码'), ('游戏数字内容','游戏|点卡|皮肤')],
        'electronics': [('手机','手机|iPhone|iPad|REDMI|荣耀|华为'), ('电脑办公','电脑|笔记本|显示器|打印机'), ('数码配件','耳机|充电|数据线|移动电源|键盘|鼠标'), ('家用电器','冰箱|洗衣机|空调|电饭煲|吸尘器')],
        'travel': [('出行交通','打车|机票|火车|加油'), ('住宿旅游','酒店|民宿|景区|旅游'), ('本地生活','电影|餐饮|外卖|团购')],
        # “包” also appears in nearly every package count (e.g. 1包/10包).
        # Match actual bags/luggage, not product packaging.
        'other': [('图书文具','图书|书籍|文具'),
                  ('服饰鞋包','衣服|服饰|外套|衬衫|T恤|裤子|裙子|袜子|运动鞋|皮鞋|凉鞋|靴子|双肩包|背包|手提包|斜挎包|旅行包|电脑包|箱包|包袋|钱包'),
                  ('其他商品','')],
    }
    if re.search(r'抽纸|卷纸|纸巾|面巾纸|卫生纸|厨房纸|湿厕纸|湿巾|湿纸巾', title or '', re.I):
        return '纸品'
    groups = patterns.get(topic, patterns['other'])
    for label, pattern in groups:
        if pattern and re.search(pattern, title or '', re.I):
            return label
    return groups[-1][0]


def resource_kind(title, body=''):
    """Mechanism hints only; no promise of eligibility or free fulfillment."""
    text = title + '\n' + (body or '')
    for kind, pattern in [('rebate',r'晒反|晒返|返后|先付后返|先买后返|返现'),
                          ('shipping',r'付邮|运费自理|支付\s*\d+元运费'),
                          ('trial',r'试用|中选'),('lottery',r'抽奖|中奖|抽中|翻牌'),
                          ('points',r'积分.{0,6}兑|兑.{0,6}积分')]:
        if re.search(pattern,text): return kind
    if re.search(r'券包|支付券|省钱卡|立减金|红包|赠金|兑换码|会员权益',title): return 'coupon'
    normalized=__import__('unicodedata').normalize('NFKC',title)
    if re.search(r'(?:领|抢|膨胀|宠物|全品)[券卷劵]|\d+(?:\.\d+)?\s*[-－]\s*\d+(?:\.\d+)?[^\d\s]{0,6}[券卷劵]',normalized):return 'coupon'
    if re.search(r'会场|领券中心|京喜特价',normalized):return 'campaign'
    if re.search(r'免费领|免费送|0元领|零元领|免费兑换',title): return 'free_claim'
    return 'purchase' if re.search(r'\d+(?:\.\d+)?\s*元|[¥￥]\d',title) else 'unknown'


def resource_topic(title, source_category=''):
    # Product identity is more specific than a broad feed label or ingredient
    # word (for example, grape-seed toothpaste is personal care, not produce).
    if re.search(r'牙膏', title or '', re.I):
        return 'beauty'
    categories=[('home',r'家用|日用|家居|家清|厨'),('food',r'食品|饮料|果蔬|餐饮'),
                ('beauty',r'美妆|个护'),('baby',r'母婴'),('pet',r'宠物'),
                ('travel',r'出行'),('digital',r'会员|数字'),('electronics',r'数码|手机')]
    for kind,pattern in categories:
        if re.search(pattern,source_category):return kind
    patterns=[('baby',r'尿裤|奶粉|母婴|婴儿'),
              ('home',r'抽纸|卷纸|纸巾|湿厕纸|湿巾|湿纸巾|洗脸巾|棉柔巾|柔巾|擦脸巾|清洁巾|卸妆巾|洗衣|洗洁|垃圾袋|保鲜|纸碗|水杯|厨房|卫生巾|清洁|电火锅|开关插座|插座面板|电工|家居五金|水龙头|门锁|合页|铰链'),('pet',r'猫砂|猫粮|狗粮|宠物'),
              ('food',r'外卖|咖啡|奶茶|餐券|饮料|零食|牛奶|水果|猕猴桃|柠檬水|橙|苹果|香蕉|柑橘|橘子|柚子|葡萄|西瓜|桃子?|梨|草莓|蓝莓|荔枝|龙眼|哈密瓜|芒果|榴莲|樱桃|车厘子|菠萝|火龙果|石榴|山竹|枣|山楂'),
              ('travel',r'打车|酒店|机票|出行|加油|电影|地图'),
              ('beauty',r'精华|面霜|洗发|护发|沐浴|洗手液|牙膏|身体乳|护手|美妆|护肤'),
              ('digital',r'会员|爱奇艺|腾讯视频|网盘|软件|游戏|兑换码'),
              ('electronics',r'手机|耳机|电脑|显示器|充电|iPhone|REDMI')]
    for kind,pattern in patterns:
        if re.search(pattern,title,re.I):return kind
    return 'other'

def paper_package_prices(title, total_cents, order_quantity=None):
    """Normalize explicit paper containers and sheet counts; never infer missing units."""
    if total_cents is None or not title:
        return None
    text = unicodedata.normalize('NFKC', str(title))
    if not re.search(r'抽纸|卷纸|纸巾|面巾纸|卫生纸|厨房纸|纸品|湿厕纸|湿巾|湿纸巾|洗脸巾|棉柔巾|柔巾|擦脸巾|清洁巾|卸妆巾', text):
        return None
    containers = re.findall(r'(?<![0-9.])([0-9]+)[ ]*(包|提|卷)(?!装)', text)
    if len(containers) != 1:
        return None
    try:
        order_quantity = int(order_quantity)
        if order_quantity < 1:
            return None
    except (TypeError, ValueError):
        return None
    count, container_unit = containers[0]
    total_containers = int(count) * order_quantity
    result = dict(pack_count=total_containers,
                  per_pack_cents=round(int(total_cents) / total_containers),
                  pack_unit=container_unit,
                  basis='按原文明确包装/卷提数量折算；不代表商家最终结算价')
    sheet_counts = re.findall(r'(?<![0-9.])([0-9]+(?:[.][0-9]+)?)[ ]*(抽|张|片)', text)
    if len(sheet_counts) == 1:
        each_count, unit = sheet_counts[0]
        total_sheets = float(each_count) * total_containers
        if total_sheets > 0:
            result['per_hundred_cents'] = round(int(total_cents) * 100 / total_sheets)
            result['hundred_unit'] = {'抽':'百抽','张':'百张','片':'百片'}[unit]
    return result
