"""Data-driven discount plausibility calibration for living-household topics.

The old rules pinned business thresholds as constants (85% strikethrough drop,
20x markup, 50-yuan absolute guard, per-topic price ceilings).  Those numbers
are replaced here by statistics estimated from the actual corpus, so the
judgment adapts as the data drifts and never needs a hand-tuned constant for a
business rule:

* price ceiling  —— median(log10 promotion) + 6 * MAD(log10 promotion):
  catches absurd unit prices (a 2997-yuan detergent bundle, an industrial
  rice-polishing machine) while keeping premium genuine bundles (5L camellia
  oil, 12-bottle low-deuterium water) inside the band.
* drop/ratio cap —— per promotion-price bin (0.5 log10 wide), the P97 of the
  drop ratio and of the drop amount over records whose ratio <= 0.92 (the
  clearly-inflated tail is excluded so the estimate is not polluted by the very
  fraud it has to catch).  A record is implausible when its ratio exceeds the
  bin's ratio cap OR its saved amount exceeds the bin's drop cap.

Conservative seeds keep the system safe before the first calibration and for
topics with too few samples; once calibrated, the estimated numbers take over.
The whole calibration is persisted to SQLite and refreshed periodically by the
worker, so thresholds follow new data automatically.
"""
from datetime import datetime, timezone
import collections
import json
import math
import os
import re
import sqlite3
from decimal import Decimal

# ---- algorithm hyper-parameters (method, not business thresholds) ----------
LOG_BIN_WIDTH = 0.5          # log10 yuan width of promotion-price bins
BIN_LO, BIN_HI = 1.0, 7.5     # 10 fen .. 3.16e6 fen (covers ¥0.1 .. ¥31623)
P = 0.97                      # quantile used as the plausible upper bound
CUT = 0.92                    # ratio above which samples are excluded from calibration
MIN_BIN_SAMPLES = 30          # bins with fewer samples fall back to the topic global
CEILING_MAD_K = 6.0           # median + k*MAD as the absurd-price boundary
REFRESH_INTERVAL_SECONDS = 3600

# ---- conservative seeds (only used before calibration / when samples are few)
SEED_RATIO_CAP = 0.85
SEED_DROP_CAP_CENTS = 5000        # 50 元
SEED_LOW_PROMO_CENTS = 1000       # 10 元
SEED_PRICE_CEILING_CENTS = {
    "paper": 30000, "water": 40000, "rice": 50000, "flour": 40000,
    "cooking_oil": 200000, "laundry_detergent": 30000,
}

# Topic category terms: a reference-price discount amount is only produced when
# the title carries a topic category word and no interfering word.  The living
# keyword search also returns look-alike products (e.g. a "Kiehl's rice & wheat
# shampoo" or a "rice-colored Reiss sweater" caught by the "rice" keyword);
# without this guard those unrelated items dominate the discount ranking.
# This is a category dictionary, not a business threshold.
TOPIC_CATEGORY_TERMS = {
    "paper": (("纸", "抽", "柔", "巾", "卷"), ("面膜", "化妆", "护肤", "彩妆", "直邮")),
    "water": (("水", "泉", "矿", "瓶", "桶", "饮"), ("护肤", "爽肤", "化妆", "美容", "香水", "精华", "洗面", "面膜")),
    "rice": (("米", "稻", "粳", "糯", "胚", "粥", "饭", "糙", "籼", "珍珠米", "丝苗", "五常", "长粒", "香米", "碎米"),
             ("洗发", "护发", "化妆", "护肤", "彩妆", "面膜", "直邮", "条纹", "罗纹", "服饰", "衣", "裤", "裙", "鞋", "帽", "手机", "数码", "油")),
    "flour": (("面", "粉", "麦", "麸", "馍", "馒头", "饺子", "面条"),
              ("面膜", "化妆", "护肤", "彩妆", "粉底", "散粉", "爽身粉", "洗衣")),
    "cooking_oil": (("油", "脂", "茶油", "橄榄", "菜籽", "葵花", "花生油", "玉米油", "调和油", "豆油", "米糠", "亚麻", "山茶"),
                    ("机油", "润滑", "燃油", "汽油", "柴油", "护肤", "精华", "卸妆", "身体乳", "发油")),
    "laundry_detergent": (("洗衣", "洗洁", "柔顺", "皂", "渍", "凝珠", "衣物", "内衣"),
                          ("洗面", "洗发", "沐浴", "化妆", "护肤", "面膜")),
}
_GENERIC_INTERFERING_TERMS = ("洗发", "护发", "化妆", "护肤", "彩妆", "面膜", "直邮", "条纹", "罗纹",
                              "服饰", "手机", "数码", "机器", "设备", "机油", "润滑")

# 工业/农用加工设备（B端大件）拦截：碾米机、粉碎机、拌馅机等设备不是个人
# 消费捡漏品，优惠 100 元对它万元金额毫无意义（用户：怎么把机器的也给获取
# 到了，乱套了）。词表是集中可维护的配置数据；判定组合「设备词 + 金额≥500元」，
# 并对家用电器/消费电子形态豁免，避免误伤破壁机、洗衣机、有机米等真优惠。
EQUIPMENT_DEVICE_TERMS = (
    "碾米机", "磨面机", "磨粉机", "打粉机", "粉碎机", "膨化机", "拌馅机", "拌料机",
    "搅馅机", "淘洗机", "去石机", "去沙机", "剥谷机", "脱壳机", "脱皮机", "榨油机",
    "压面机", "分离机", "破碎机", "剥壳机", "脱粒机", "孵化机", "收割机", "播种机",
    "青贮机", "磨浆机", "切丝机", "切片机", "饲料机", "柴油机", "汽油机", "空压机",
    "真空包装机", "封口机", "灌装机", "输送机", "提升机", "烘干机", "清洗机", "搅拌机",
    "和面机", "绞肉机", "切肉机", "饺子机", "包馅机", "丸子机", "磨豆机", "爆米花机",
    "薯条机", "炸鸡炉", "烤鸭炉", "商用烤箱", "冰柜", "冷柜", "保鲜柜", "和面",
    "面条机", "馒头机", "豆腐机", "豆芽机",
)
# B端场景词 + 设备核心词组合兜底（未枚举的设备名，如"厂家供应芝麻清洗机"）。
BUSINESS_EQUIPMENT_MARKERS = ("商用", "工厂", "厂家", "批发", "摆摊", "农用", "工业",
                              "大型", "成套", "流水线", "养殖", "车间", "作坊", "粮食加工",
                              "粮油加工", "饲料", "秸秆", "稻谷", "粮食")
_EQUIPMENT_CORE_WORDS = ("机", "设备", "机械", "机组")
# 家用电器/消费电子形态豁免（即使含设备词或 B端组合也不拦；家用大件也能真捡漏）。
HOUSEHOLD_APPLIANCE_EXEMPT_TERMS = (
    "洗衣机", "干衣机", "烘干衣", "微波炉", "冰箱", "空调", "电视", "手机", "电脑",
    "平板", "耳机", "音箱", "破壁", "榨汁", "豆浆", "咖啡机", "电饭煲", "电水壶",
    "电风扇", "吸尘", "扫地机", "吹风机", "熨斗", "挂烫", "饮水机", "空气炸锅",
    "面包机", "电炖盅", "加湿器", "净化器", "取暖器", "热水器", "洗碗机", "消毒柜",
    "油烟机", "燃气灶", "理发器", "剃须刀", "美容仪", "剃毛器", "电动牙刷", "投影仪",
)
# 设备判定金额下界：500 元。家用小机器（几十元的家用粉碎机/爆米花机）低于
# 此线不拦，避免误伤低价家用真优惠；万元级设备必然超过此线。
EQUIPMENT_MIN_CENTS = 50000


def is_machinery_equipment(title, promo_cents):
    """Whether a title is an industrial/agricultural processing device (B2B).

    A 10,000-yuan rice mill with a 100-yuan coupon is not a personal-consumption
    bargain; showing it in the default offer list is noise (user screenshot).
    Exact device names, or a business marker plus an equipment core word, gate
    the item — but only at/above 500 yuan, and household appliances/consumer
    electronics are exempt so genuine deals (blenders, washing machines,
    organic rice) are never harmed.
    """
    if not title or not isinstance(promo_cents, int) or isinstance(promo_cents, bool) \
            or promo_cents < EQUIPMENT_MIN_CENTS:
        return False
    if any(word in title for word in HOUSEHOLD_APPLIANCE_EXEMPT_TERMS):
        return False
    if any(word in title for word in EQUIPMENT_DEVICE_TERMS):
        return True
    if any(word in title for word in BUSINESS_EQUIPMENT_MARKERS) and any(
            word in title for word in _EQUIPMENT_CORE_WORDS):
        return True
    return False

# Trial / first-order traffic SKUs ("尝鲜" 348ml x 2 bottles at 5 yuan each,
# "新客专享" small packs, "u先试用", "2.01元购" first-order subsidies).  They
# carry a deep-looking strikethrough but are marketing traffic, not real
# bargains: they must not produce a discount amount.
TRIAL_GATING_TERMS = ("尝鲜", "首单", "新人", "新客", "试饮", "试用", "小样", "引流",
                      "体验装", "一分购", "元购", "首购", "首件", "试吃", "试喝",
                      "随身装", "便携", "迷你", "U先", "u先", "新人礼", "首单礼金",
                      "免费领", "1分钱")
# A promotion price shared verbatim by many distinct listings inside one topic's
# low band is a first-order/unified subsidy price, not a market price.
GATED_MIN_COUNT = 5
# Market normal-price band: final price within median +/- k*MAD of the topic's
# promotion prices means the deal is priced "like everywhere else" (user:
# 跟其他平台价格一样 相差就一两块钱 肯定不算数).  Combined with a deep
# strikethrough (>= topic P90 ratio) it means the merchant built the discount
# from an inflated reference price — not a real bargain.  Both bounds are
# estimated from the corpus, never hard-coded.
MARKET_BAND_MAD_K = 1.0
DEEP_DROP_QUANTILE = 0.90

# Small-specification upper limits per unit family (physical goods attributes,
# not discount thresholds): a "一瓶一包几块钱" item the user rejects as traffic
# (30抽便携装, 2瓶尝鲜, 348ml小瓶) is small in EVERY measured spec, while a
# genuine bulk deal (400抽30包, 570ml*24瓶, 4.5L*2) exceeds at least one limit
# and is never gated.  Only combined with a low price (< topic median) does a
# small spec become a low-value small item.
SMALL_SPEC_LIMITS = {
    "sheet": 200,      # 抽 / 张 / 片
    "pack": 6,         # 包 / 提
    "bottle": 6,       # 瓶 / 支 / 粒
    "ml": 500,         # ml / 毫升
    "liter": 1.5,      # L / 升
    "kg": 2,           # kg / 斤
}
_SPEC_UNIT = {
    "抽": "sheet", "张": "sheet", "片": "sheet",
    "包": "pack", "提": "pack",
    "瓶": "bottle", "支": "bottle", "粒": "bottle",
    "ml": "ml", "毫升": "ml",
    "L": "liter", "l": "liter", "升": "liter",
    "g": "kg", "克": "kg",
    "kg": "kg", "斤": "kg",
}
_SPEC_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(抽|张|片|包|提|瓶|支|粒|ml|毫升|L|升|kg|斤|g|克)", re.IGNORECASE)
_BOX_RE = re.compile(r"(\d+(?:\.\d+)?)\s*箱")
_BAG_RE = re.compile(r"(\d+(?:\.\d+)?)\s*袋")
_TIMES_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(kg|斤|g|克|l|L|升)\s*[x×*]\s*(\d+(?:\.\d+)?)(?:\s*(?:袋|包|瓶|桶|提|盒|箱|罐))?",
    re.IGNORECASE)

# =============================================================================
# 品牌标准市场参考配置（正式配置数据，随主题关键词同步维护，非业务阈值）
# =============================================================================
# 用户要求："增加品牌的产品进行参考，一件/一瓶/一包这样的数据，优惠价金额
# 小于等于品牌标准市场价、小于市场价才算真优惠。这个你是知道的要进行维护"。
# 本表是唯一配置源：品牌词（含别名）→ 标准售卖单位 → 标准市场价（分）。
#
# 维护规则：
#   * terms  ：标题中出现的品牌词/别名（越长者优先匹配，如"华润五丰"优于"五丰"）。
#   * unit   ：标准售卖单位，必须与规格折算引擎 spec_unit_price_cents 的
#              输出单位一致（bottle500=500ml瓶 / bottle=瓶 / kg=公斤 /
#              liter=升 / pack100=100抽包 / pack=包）；商品折算单位与配置
#              单位不同则不比（保守放行）。
#   * price_cents：该品牌该单位的市场价（单位：分，整数）。初始值按主流
#              渠道零售常识设定，随渠道变化直接在此增改。
#   * note   ：规格/备注说明（纯文档，不影响逻辑）。
# 判据：折算标准单位到手价 > price_cents × BRAND_OVERPRICE_K(1.2) →
# 划线虚高/引流小规格，从默认列表排除、不产生优惠额；≤ 则真优惠放行。
# 未知品牌 / 无规格可折算 / 无标准价 → 永不拦截（不误伤真实捡漏）。
# =============================================================================
BRAND_REFERENCE_CONFIG = {
    "paper": {
        "维达":   {"terms": ("维达", "vinda"), "unit": "pack100", "price_cents": 350, "note": "维达超韧/棉韧抽纸单包(100抽)零售"},
        "清风":   {"terms": ("清风",), "unit": "pack100", "price_cents": 250, "note": "清风原木纯品单包(100抽)"},
        "洁柔":   {"terms": ("洁柔",), "unit": "pack100", "price_cents": 300, "note": "洁柔Face/立体美单包(100抽)"},
        "心相印": {"terms": ("心相印",), "unit": "pack100", "price_cents": 280, "note": "心相印茶语经典单包(100抽)"},
        "植护":   {"terms": ("植护",), "unit": "pack100", "price_cents": 200, "note": "植护低价抽纸单包"},
        "漫花":   {"terms": ("漫花",), "unit": "pack100", "price_cents": 200, "note": "漫花低价抽纸单包"},
        "丹娜丝": {"terms": ("丹娜丝",), "unit": "pack100", "price_cents": 200, "note": "丹娜丝悬挂式抽纸单提折算"},
        "怡飘":   {"terms": ("怡飘",), "unit": "pack100", "price_cents": 200, "note": "怡飘乳霜纸单包"},
        "卡布":   {"terms": ("卡布",), "unit": "pack100", "price_cents": 1200, "note": "卡布婴童乳霜纸单包(80抽级)"},
        "可优比": {"terms": ("可优比", "KUB", "kub"), "unit": "pack100", "price_cents": 1200, "note": "可优比婴童云柔巾单包(80抽级，100抽折算)"},
        "babycare": {"terms": ("babycare", "bc"), "unit": "pack100", "price_cents": 1658, "note": "babycare云柔巾乳霜保湿单包(库内样本P50折算)"},
        "德佑":   {"terms": ("德佑",), "unit": "pack100", "price_cents": 359, "note": "德佑乳霜纸单包(库内样本P50折算)"},
        "大王":   {"terms": ("大王", "GOO.N"), "unit": "pack100", "price_cents": 1200, "note": "大王奢润乳霜婴儿柔纸巾单包(100抽级，60抽整箱折算)"},
        "全棉时代": {"terms": ("全棉时代",), "unit": "pack100", "price_cents": 600, "note": "全棉时代棉柔巾单包"},
        "得宝":   {"terms": ("得宝", "tempo"), "unit": "pack100", "price_cents": 450, "note": "得宝抽纸单包(中高端品牌,90抽4.5元/包折算)"},
        "斑布":   {"terms": ("斑布",), "unit": "pack100", "price_cents": 350, "note": "斑布竹浆抽纸单包"},
        "顺清柔": {"terms": ("顺清柔",), "unit": "pack100", "price_cents": 200, "note": "顺清柔抽纸单包"},
        "理文":   {"terms": ("理文",), "unit": "pack100", "price_cents": 200, "note": "理文本色抽纸单包"},
        "洁云":   {"terms": ("洁云",), "unit": "pack100", "price_cents": 280, "note": "洁云抽纸单包"},
        "妮飘":   {"terms": ("妮飘", "nepia"), "unit": "pack100", "price_cents": 1200, "note": "妮飘鼻贵族单包(100抽)"},
        "舒洁":   {"terms": ("舒洁", "kleenex"), "unit": "pack100", "price_cents": 250, "note": "舒洁抽纸单包"},
        "惠寻":   {"terms": ("惠寻",), "unit": "pack100", "price_cents": 200, "note": "惠寻低价抽纸单包"},
        "京东京造": {"terms": ("京东京造", "京造"), "unit": "pack100", "price_cents": 250, "note": "京东京造抽纸单包"},
        "喵满分": {"terms": ("喵满分",), "unit": "pack100", "price_cents": 200, "note": "喵满分低价抽纸单包"},
        "悦适":   {"terms": ("悦适",), "unit": "pack100", "price_cents": 200, "note": "悦适抽纸单包"},
        "德宝":   {"terms": ("德宝",), "unit": "pack100", "price_cents": 400, "note": "德宝进口单包(100抽)"},
        "五月花": {"terms": ("五月花",), "unit": "pack100", "price_cents": 280, "note": "五月花抽纸单包(100抽)"},
        "丝飘":   {"terms": ("丝飘",), "unit": "pack100", "price_cents": 200, "note": "丝飘低价抽纸单包"},
        "双灯":   {"terms": ("双灯",), "unit": "pack100", "price_cents": 180, "note": "双灯卷纸/抽纸折算单包"},
        "泉林本色": {"terms": ("泉林本色",), "unit": "pack100", "price_cents": 280, "note": "泉林本色竹浆纸单包"},
        "幸福阳光": {"terms": ("幸福阳光",), "unit": "pack100", "price_cents": 280, "note": "幸福阳光本色纸单包"},
        "无染":   {"terms": ("无染",), "unit": "pack100", "price_cents": 260, "note": "无染竹浆纸单包"},
        "竹π":    {"terms": ("竹π",), "unit": "pack100", "price_cents": 240, "note": "竹π竹浆纸单包"},
        "山丘":   {"terms": ("山丘",), "unit": "pack100", "price_cents": 300, "note": "山丘本色纸单包"},
        "太阳":   {"terms": ("太阳",), "unit": "pack100", "price_cents": 200, "note": "太阳纸业抽纸单包"},
        "蓝漂":   {"terms": ("蓝漂",), "unit": "pack100", "price_cents": 200, "note": "蓝漂低价抽纸单包(曾现假优惠)"},
        "银鸽":   {"terms": ("银鸽",), "unit": "pack100", "price_cents": 180, "note": "银鸽抽纸单包"},
        "丽邦":   {"terms": ("丽邦",), "unit": "pack100", "price_cents": 200, "note": "丽邦低价抽纸单包"},
        "小宝贝": {"terms": ("小宝贝",), "unit": "pack100", "price_cents": 200, "note": "小宝贝低价抽纸单包"},
        "好吉利": {"terms": ("好吉利",), "unit": "pack100", "price_cents": 180, "note": "好吉利抽纸单包"},
        "维尔美": {"terms": ("维尔美",), "unit": "pack100", "price_cents": 190, "note": "维尔美抽纸单包"},
        "洁伴":   {"terms": ("洁伴",), "unit": "pack100", "price_cents": 180, "note": "洁伴抽纸单包"},
    },
    "water": {
        "百岁山": {"terms": ("百岁山",), "unit": "bottle500", "price_cents": 280, "note": "百岁山570ml单瓶零售约3元", "extra_units": {"liter": 526}},
        "农夫山泉": {"terms": ("农夫山泉",), "unit": "bottle500", "price_cents": 180, "note": "农夫山泉550ml单瓶零售约2元", "extra_units": {"liter": 364}},
        "怡宝":   {"terms": ("怡宝", "cestbon"), "unit": "bottle500", "price_cents": 180, "note": "怡宝555ml单瓶零售约2元", "extra_units": {"liter": 360}},
        "康师傅": {"terms": ("康师傅",), "unit": "bottle500", "price_cents": 150, "note": "康师傅550ml单瓶", "extra_units": {"liter": 273}},
        "娃哈哈": {"terms": ("娃哈哈",), "unit": "bottle500", "price_cents": 180, "note": "娃哈哈596ml单瓶", "extra_units": {"liter": 336}},
        "景田":   {"terms": ("景田",), "unit": "bottle500", "price_cents": 150, "note": "景田百岁山之外的景田水", "extra_units": {"liter": 273}},
        "恒大冰泉": {"terms": ("恒大冰泉",), "unit": "bottle500", "price_cents": 200, "note": "恒大冰泉500ml单瓶", "extra_units": {"liter": 400}},
        "依云":   {"terms": ("依云", "evian"), "unit": "bottle500", "price_cents": 1000, "note": "依云330ml进口单瓶", "extra_units": {"liter": 3030}},
        "昆仑山": {"terms": ("昆仑山",), "unit": "bottle500", "price_cents": 400, "note": "昆仑山550ml单瓶", "extra_units": {"liter": 727}},
        "屈臣氏": {"terms": ("屈臣氏",), "unit": "bottle500", "price_cents": 400, "note": "屈臣氏蒸馏水500ml单瓶", "extra_units": {"liter": 800}},
        "冰露":   {"terms": ("冰露",), "unit": "bottle500", "price_cents": 120, "note": "冰露550ml单瓶", "extra_units": {"liter": 218}},
        "纯水乐": {"terms": ("纯水乐",), "unit": "bottle500", "price_cents": 150, "note": "纯水乐550ml单瓶", "extra_units": {"liter": 273}},
        "泉阳泉": {"terms": ("泉阳泉",), "unit": "bottle500", "price_cents": 250, "note": "泉阳泉500ml单瓶", "extra_units": {"liter": 500}},
        "卓玛泉": {"terms": ("卓玛泉",), "unit": "bottle500", "price_cents": 300, "note": "卓玛泉西藏冰川水单瓶", "extra_units": {"liter": 600}},
        "今麦郎": {"terms": ("今麦郎",), "unit": "bottle500", "price_cents": 150, "note": "今麦郎纯净水550ml", "extra_units": {"liter": 273}},
        "凉白开": {"terms": ("凉白开",), "unit": "bottle500", "price_cents": 180, "note": "凉白开熟水550ml", "extra_units": {"liter": 327}},
        "润田":   {"terms": ("润田",), "unit": "bottle500", "price_cents": 120, "note": "润田纯净水550ml", "extra_units": {"liter": 218}},
        "乐百氏": {"terms": ("乐百氏",), "unit": "bottle500", "price_cents": 150, "note": "乐百氏纯净水550ml", "extra_units": {"liter": 273}},
        "崂山":   {"terms": ("崂山",), "unit": "bottle500", "price_cents": 250, "note": "崂山矿泉水550ml", "extra_units": {"liter": 455}},
        "统一":   {"terms": ("统一", "ALKAQUA", "alkaqua"), "unit": "bottle500", "price_cents": 300, "note": "统一爱夸水550ml", "extra_units": {"liter": 545}},
        "雀巢":   {"terms": ("雀巢", "nestle"), "unit": "bottle500", "price_cents": 180, "note": "雀巢优活水550ml", "extra_units": {"liter": 327}},
        "巴黎水": {"terms": ("巴黎水", "perrier"), "unit": "bottle500", "price_cents": 1500, "note": "巴黎水气泡水330ml", "extra_units": {"liter": 3000}},
        "圣培露": {"terms": ("圣培露",), "unit": "bottle500", "price_cents": 1500, "note": "圣培露气泡水330ml", "extra_units": {"liter": 3000}},
        "芙丝":   {"terms": ("芙丝", "voss"), "unit": "bottle500", "price_cents": 2500, "note": "VOSS芙丝水500ml", "extra_units": {"liter": 5000}},
        "5100":   {"terms": ("5100",), "unit": "bottle500", "price_cents": 1000, "note": "5100西藏冰川水500ml", "extra_units": {"liter": 1800}},
        "巴马铂泉": {"terms": ("巴马铂泉",), "unit": "bottle500", "price_cents": 600, "note": "巴马铂泉矿泉水500ml", "extra_units": {"liter": 1100}},
        "鼎湖山泉": {"terms": ("鼎湖山泉",), "unit": "bottle500", "price_cents": 200, "note": "鼎湖山泉500ml", "extra_units": {"liter": 360}},
        "椰树矿泉水": {"terms": ("椰树矿泉水", "椰树天然矿泉水"), "unit": "bottle500", "price_cents": 200, "note": "椰树矿泉水550ml", "extra_units": {"liter": 360}},
        "蓝剑":   {"terms": ("蓝剑",), "unit": "bottle500", "price_cents": 150, "note": "蓝剑冰川时代水", "extra_units": {"liter": 273}},
        "洞庭山": {"terms": ("洞庭山",), "unit": "bottle500", "price_cents": 200, "note": "洞庭山泉水(曾现桶装假优惠)", "extra_units": {"liter": 360}},
    },
    "rice": {
        "柴火大院": {"terms": ("柴火大院",), "unit": "kg", "price_cents": 4000, "note": "柴火大院有机五常米约40元/kg"},
        "十月稻田": {"terms": ("十月稻田",), "unit": "kg", "price_cents": 3000, "note": "十月稻田五常米约30元/kg"},
        "龙稻":   {"terms": ("龙稻",), "unit": "kg", "price_cents": 4000, "note": "龙稻五常米高端约40元/kg"},
        "福临门": {"terms": ("福临门",), "unit": "kg", "price_cents": 1200, "note": "福临门东北大米约12元/kg"},
        "金龙鱼": {"terms": ("金龙鱼",), "unit": "kg", "price_cents": 1200, "note": "金龙鱼大米约12元/kg"},
        "北大荒": {"terms": ("北大荒",), "unit": "kg", "price_cents": 1200, "note": "北大荒大米约12元/kg"},
        "香满园": {"terms": ("香满园",), "unit": "kg", "price_cents": 1000, "note": "香满园大米约10元/kg"},
        "孟乍隆": {"terms": ("孟乍隆",), "unit": "kg", "price_cents": 3000, "note": "孟乍隆泰国香米约30元/kg"},
        "华润五丰": {"terms": ("华润五丰", "五丰"), "unit": "kg", "price_cents": 1500, "note": "华润五丰大米约15元/kg"},
        "金健":   {"terms": ("金健",), "unit": "kg", "price_cents": 1000, "note": "金健大米约10元/kg"},
        "乔府大院": {"terms": ("乔府大院",), "unit": "kg", "price_cents": 3000, "note": "乔府大院五常米约30元/kg"},
        "裕道府": {"terms": ("裕道府",), "unit": "kg", "price_cents": 3000, "note": "裕道府五常米约30元/kg"},
        "恒大兴安": {"terms": ("恒大兴安",), "unit": "kg", "price_cents": 1500, "note": "恒大兴安大米约15元/kg"},
        "稻花香": {"terms": ("稻花香",), "unit": "kg", "price_cents": 2000, "note": "稻花香五常米约10元/斤=20元/kg"},
        "葵花阳光": {"terms": ("葵花阳光",), "unit": "kg", "price_cents": 800, "note": "葵花阳光东北米约8元/kg"},
        "响水":   {"terms": ("响水",), "unit": "kg", "price_cents": 2000, "note": "响水大米高端约20元/kg"},
        "五芳斋": {"terms": ("五芳斋",), "unit": "kg", "price_cents": 800, "note": "五芳斋大米约8元/kg"},
        "隆平高科": {"terms": ("隆平高科", "袁隆平"), "unit": "kg", "price_cents": 800, "note": "隆平高科/袁隆平大米约8元/kg"},
        "晶润":   {"terms": ("晶润",), "unit": "kg", "price_cents": 700, "note": "晶润大米约7元/kg"},
        "王家粮仓": {"terms": ("王家粮仓",), "unit": "kg", "price_cents": 1000, "note": "王家粮仓大米约10元/kg"},
        "皇家稻场": {"terms": ("皇家稻场",), "unit": "kg", "price_cents": 1200, "note": "皇家稻场大米约12元/kg"},
        "中储粮": {"terms": ("中储粮",), "unit": "kg", "price_cents": 700, "note": "中储粮大米约7元/kg"},
        "七河源": {"terms": ("七河源",), "unit": "kg", "price_cents": 800, "note": "七河源大米约8元/kg"},
    },
    "flour": {
        "金沙河": {"terms": ("金沙河",), "unit": "kg", "price_cents": 600, "note": "金沙河家用面粉约4元/kg"},
        "五得利": {"terms": ("五得利",), "unit": "kg", "price_cents": 800, "note": "五得利面粉约4元/kg"},
        "中裕":   {"terms": ("中裕",), "unit": "kg", "price_cents": 800, "note": "中裕面粉约6元/kg"},
        "金龙鱼": {"terms": ("金龙鱼",), "unit": "kg", "price_cents": 700, "note": "金龙鱼面粉约7元/kg"},
        "福临门": {"terms": ("福临门",), "unit": "kg", "price_cents": 700, "note": "福临门面粉约7元/kg"},
        "河套":   {"terms": ("河套",), "unit": "kg", "price_cents": 1500, "note": "河套雪花粉高端约15元/kg"},
        "塞北雪": {"terms": ("塞北雪",), "unit": "kg", "price_cents": 1000, "note": "塞北雪面粉约10元/kg"},
        "新良":   {"terms": ("新良",), "unit": "kg", "price_cents": 800, "note": "新良低筋/高筋粉约8元/kg"},
        "展艺":   {"terms": ("展艺",), "unit": "kg", "price_cents": 700, "note": "展艺烘焙粉约7元/kg"},
        "风筝":   {"terms": ("风筝",), "unit": "kg", "price_cents": 400, "note": "风筝牌面粉约4元/kg"},
        "古船":   {"terms": ("古船",), "unit": "kg", "price_cents": 600, "note": "古船面粉约5元/kg"},
        "天山":   {"terms": ("天山",), "unit": "kg", "price_cents": 1300, "note": "天山面粉约6元/kg"},
        "香满园": {"terms": ("香满园",), "unit": "kg", "price_cents": 900, "note": "香满园面粉约7元/kg"},
        "中粮":   {"terms": ("中粮",), "unit": "kg", "price_cents": 700, "note": "中粮面粉约7元/kg"},
        "今麦郎": {"terms": ("今麦郎",), "unit": "kg", "price_cents": 400, "note": "今麦郎面粉约4元/kg"},
        "想念":   {"terms": ("想念",), "unit": "kg", "price_cents": 600, "note": "想念面粉/挂面约5元/kg"},
        "陈克明": {"terms": ("陈克明",), "unit": "kg", "price_cents": 600, "note": "陈克明面粉/挂面约5元/kg"},
        "鲁王":   {"terms": ("鲁王",), "unit": "kg", "price_cents": 500, "note": "鲁王面粉约5元/kg"},
        "发达":   {"terms": ("发达",), "unit": "kg", "price_cents": 500, "note": "发达面粉约5元/kg"},
        "香雪":   {"terms": ("香雪",), "unit": "kg", "price_cents": 600, "note": "中粮香雪面粉约5元/kg"},
        "丹顶鹤": {"terms": ("丹顶鹤",), "unit": "kg", "price_cents": 600, "note": "丹顶鹤面粉约6元/kg"},
        "北大荒": {"terms": ("北大荒",), "unit": "kg", "price_cents": 600, "note": "北大荒面粉约6元/kg"},
        "王后":   {"terms": ("王后",), "unit": "kg", "price_cents": 1500, "note": "王后烘焙面粉高端约15元/kg"},
        "白燕":   {"terms": ("白燕",), "unit": "kg", "price_cents": 1200, "note": "白燕烘焙粉约12元/kg"},
    },
    "cooking_oil": {
        "金龙鱼": {"terms": ("金龙鱼",), "unit": "liter", "price_cents": 1500, "note": "金龙鱼调和油5L约75元"},
        "鲁花":   {"terms": ("鲁花",), "unit": "liter", "price_cents": 3600, "note": "鲁花5S花生油5L约120元"},
        "福临门": {"terms": ("福临门",), "unit": "liter", "price_cents": 1300, "note": "福临门食用油约13元/L"},
        "多力":   {"terms": ("多力",), "unit": "liter", "price_cents": 2000, "note": "多力葵花籽油约20元/L"},
        "胡姬花": {"terms": ("胡姬花",), "unit": "liter", "price_cents": 3000, "note": "胡姬花古法花生油约20元/L"},
        "刀唛":   {"terms": ("刀唛", "knife"), "unit": "liter", "price_cents": 3000, "note": "刀唛花生油约30元/L"},
        "西王":   {"terms": ("西王",), "unit": "liter", "price_cents": 1600, "note": "西王玉米油约16元/L"},
        "九三":   {"terms": ("九三",), "unit": "liter", "price_cents": 1500, "note": "九三非转基因豆油约15元/L"},
        "长寿花": {"terms": ("长寿花",), "unit": "liter", "price_cents": 1800, "note": "长寿花玉米油约18元/L"},
        "欧丽薇兰": {"terms": ("欧丽薇兰", "olivoil"), "unit": "liter", "price_cents": 3000, "note": "欧丽薇兰橄榄油约30元/L"},
        "千岛源": {"terms": ("千岛源",), "unit": "liter", "price_cents": 3000, "note": "千岛源山茶油约30元/L"},
        "金鼎":   {"terms": ("金鼎",), "unit": "liter", "price_cents": 1400, "note": "金鼎食用油约14元/L"},
        "鹰唛":   {"terms": ("鹰唛",), "unit": "liter", "price_cents": 1800, "note": "鹰唛花生油约18元/L"},
        "骆驼唛": {"terms": ("骆驼唛",), "unit": "liter", "price_cents": 1800, "note": "骆驼唛花生油约18元/L"},
        "香满园": {"terms": ("香满园",), "unit": "liter", "price_cents": 1400, "note": "香满园食用油约14元/L"},
        "道道全": {"terms": ("道道全",), "unit": "liter", "price_cents": 1400, "note": "道道全食用油约14元/L(曾现假优惠)"},
        "金胜":   {"terms": ("金胜",), "unit": "liter", "price_cents": 1600, "note": "金胜花生油约16元/L"},
        "龙大":   {"terms": ("龙大",), "unit": "liter", "price_cents": 1600, "note": "龙大花生油约16元/L"},
        "天府":   {"terms": ("天府",), "unit": "liter", "price_cents": 1200, "note": "天府菜籽油约12元/L"},
        "川菜王": {"terms": ("川菜王",), "unit": "liter", "price_cents": 1200, "note": "川菜王菜籽油约12元/L"},
        "贝蒂斯": {"terms": ("贝蒂斯", "betis"), "unit": "liter", "price_cents": 12000, "note": "贝蒂斯特级初榨橄榄油约120元/L"},
        "品利":   {"terms": ("品利",), "unit": "liter", "price_cents": 6000, "note": "品利橄榄油约60元/L"},
        "亿芭利": {"terms": ("亿芭利",), "unit": "liter", "price_cents": 6000, "note": "亿芭利橄榄油约60元/L"},
        "康宝娜": {"terms": ("康宝娜",), "unit": "liter", "price_cents": 6000, "note": "康宝娜橄榄油约60元/L"},
        "金浩":   {"terms": ("金浩",), "unit": "liter", "price_cents": 9000, "note": "金浩茶油约90元/L"},
        "唯他可可": {"terms": ("唯他可可", "vita coco"), "unit": "liter", "price_cents": 6000, "note": "唯他可可椰子水约60元/L"},
        "恒大兴安": {"terms": ("恒大兴安",), "unit": "liter", "price_cents": 1400, "note": "恒大兴安食用油约14元/L"},
        "北大荒": {"terms": ("北大荒",), "unit": "liter", "price_cents": 1400, "note": "北大荒食用油约14元/L"},
    },
    "laundry_detergent": {
        "蓝月亮": {"terms": ("蓝月亮",), "unit": "kg", "price_cents": 2000, "note": "蓝月亮洗衣液3kg约60元"},
        "绽家":   {"terms": ("绽家",), "unit": "kg", "price_cents": 4000, "note": "绽家香氛洗衣液1.7L装市场价约68元"},
        "立白":   {"terms": ("立白",), "unit": "kg", "price_cents": 1500, "note": "立白洗衣液3kg约45元"},
        "超能":   {"terms": ("超能",), "unit": "kg", "price_cents": 1500, "note": "超能洗衣液3kg约45元"},
        "威露士": {"terms": ("威露士",), "unit": "kg", "price_cents": 2000, "note": "威露士洗衣液3kg约60元"},
        "奥妙":   {"terms": ("奥妙",), "unit": "kg", "price_cents": 1500, "note": "奥妙洗衣液3kg约45元"},
        "汰渍":   {"terms": ("汰渍", "tide"), "unit": "kg", "price_cents": 2500, "note": "汰渍洗衣液3kg约45元"},
        "碧浪":   {"terms": ("碧浪", "ariel"), "unit": "kg", "price_cents": 2000, "note": "碧浪洗衣液3kg约60元"},
        "雕牌":   {"terms": ("雕牌",), "unit": "kg", "price_cents": 1200, "note": "雕牌洗衣液3kg约36元"},
        "卫新":   {"terms": ("卫新",), "unit": "kg", "price_cents": 1500, "note": "卫新洗衣液3kg约45元"},
        "花王":   {"terms": ("花王", "kao"), "unit": "kg", "price_cents": 3000, "note": "花王洗衣液3kg约90元"},
        "狮王":   {"terms": ("狮王", "lion"), "unit": "kg", "price_cents": 3000, "note": "狮王洗衣液3kg约90元"},
        "白猫":   {"terms": ("白猫",), "unit": "kg", "price_cents": 1300, "note": "白猫洗衣液3kg约30元"},
        "妈妈壹选": {"terms": ("妈妈壹选",), "unit": "kg", "price_cents": 1800, "note": "妈妈壹选洗衣液3kg约54元"},
        "好爸爸": {"terms": ("好爸爸",), "unit": "kg", "price_cents": 1800, "note": "好爸爸洗衣液3kg约54元"},
        "开米":   {"terms": ("开米",), "unit": "kg", "price_cents": 2000, "note": "开米洗衣液3kg约60元"},
        "洁霸":   {"terms": ("洁霸", "attack"), "unit": "kg", "price_cents": 1800, "note": "洁霸洗衣液3kg约54元"},
        "绿伞":   {"terms": ("绿伞",), "unit": "kg", "price_cents": 1200, "note": "绿伞洗衣液3kg约36元"},
        "洛娃":   {"terms": ("洛娃",), "unit": "kg", "price_cents": 900, "note": "洛娃洗衣液3kg约27元"},
        "金纺":   {"terms": ("金纺",), "unit": "liter", "price_cents": 1500, "note": "金纺柔顺剂3L约45元"},
        "滴露":   {"terms": ("滴露", "dettol"), "unit": "kg", "price_cents": 1800, "note": "滴露衣物除菌液约18元/kg"},
        "网易严选": {"terms": ("网易严选",), "unit": "kg", "price_cents": 1200, "note": "网易严选洗衣液约12元/kg"},
        "京东京造": {"terms": ("京东京造", "京造"), "unit": "kg", "price_cents": 1200, "note": "京东京造洗衣液约12元/kg"},
        "洁劲":   {"terms": ("洁劲",), "unit": "kg", "price_cents": 1200, "note": "立白洁劲洗衣液约12元/kg"},
        "威王":   {"terms": ("威王",), "unit": "kg", "price_cents": 1500, "note": "威王除菌液约15元/kg"},
        "泡泡娃": {"terms": ("泡泡娃",), "unit": "kg", "price_cents": 900, "note": "泡泡娃低价洗衣液约9元/kg"},
        "恒洁":   {"terms": ("恒洁",), "unit": "kg", "price_cents": 900, "note": "恒洁洗衣液约9元/kg"},
        "浪奇":   {"terms": ("浪奇",), "unit": "kg", "price_cents": 1200, "note": "浪奇洗衣液约12元/kg"},
        "家安":   {"terms": ("家安",), "unit": "kg", "price_cents": 1200, "note": "家安洗衣液约12元/kg"},
    },
}

# 品牌单品参考判据系数：折算标准单位到手价高于该品牌标准市场价达该倍数
# 以上视为"贵过市场价"（划线虚高/引流小规格）。1.2x 容忍渠道价差：整箱
# 真品折算单价 ≈ 零售市场价（1.0-1.2 倍）放行，尝鲜小规格折算单价 2-3 倍
# 拦截。
BRAND_OVERPRICE_K = 1.2

# ---------------------------------------------------------------------------
# 细分品类豁免词表（品牌判据只在"标准品类"上生效）
# ---------------------------------------------------------------------------
# 背景：品牌标准市场价是按普通标准品类口径维护的（纸=普通100抽包、水=常规
# 500ml 瓶、米面=普通公斤、油=调和/普通品类、洗衣液=常规公斤）。乳霜纸/悬挂
# 式/婴儿柔巾、苏打水、花生/橄榄/山茶油、烘焙/全麦面粉、杂粮/礼盒米、浓缩
# 洗衣液等细分品类单价系统性高于普通品类，若用普通标准价判定会误伤真品。
# 命中豁免词 → 品牌判据直接跳过（这些品类价格透明、假优惠少，且③④层已拦
# 低价小件/划线虚高）。标准品类（普通抽纸、矿泉水、调和/大豆/菜籽油、家用
# 面粉、普通大米、常规洗衣液）仍严格用品牌标准价判据。
BRAND_EXEMPT_TERMS = {
    "paper": ("乳霜", "云柔", "柔巾", "婴儿", "宝宝", "护理", "悬挂", "挂抽",
              "壁挂", "提挂", "加厚", "4层", "四层", "正厚", "厚实", "压花",
              "迷你", "Mini", "mini", "联名", "限定", "万圣节", "kitty", "凯蒂", "三丽鸥",
              "桂花香", "南瓜香", "厨房", "天鹅绒", "亲肤", "羊绒", "面纸", "手帕", "随身",
              "擦手", "厨房纸", "多康", "商用", "商务", "湿厕", "可冲散",
              "溶易抽", "黑金", "高端", "进口", "XL", "大尺寸"),
    "water": ("苏打", "气泡", "含气", "咖啡", "茶饮", "果味", "迷你", "儿童",
              "碱性", "电解", "体验", "联名", "限定", "婴儿", "母婴", "宝宝", "汪汪队",
              "润田翠"),
    "rice": ("杂粮", "糙米", "低GI", "黑米", "红米", "紫米", "胚芽", "有机",
             "礼盒", "特等", "臻选", "贡米", "五常", "香米", "长粒香",
             "茉莉香米", "皇家", "组合", "粮油", "小袋", "鲜米", "6步鲜", "稻谷鲜生"),
    "flour": ("全麦", "黑麦", "麦麸", "烘焙", "面包", "自发", "预拌", "杂粮",
              "瑞雪", "麦芯", "尚品", "雪花", "臻选", "有机", "淀粉", "澄面",
              "虾饺", "小袋", "石磨", "面筋粉", "谷朊粉", "六星", "七星",
              "八星", "九星", "金典", "冀南香", "沙粒", "wonder", "天山", "新疆",
              "多谷物", "谷物", "糕点", "低筋", "奇台", "臻品", "贵族", "河套雪"),
    "cooking_oil": ("花生", "橄榄", "山茶", "茶油", "菜籽油", "初榨", "胡麻",
                    "亚麻", "核桃", "芝麻", "葡萄籽", "红花", "芥花", "牛油果",
                    "葵花籽", "葵花仁", "葵仁", "玉米", "大豆油", "稻米油", "米糠油",
                    "二酯"),
    "laundry_detergent": ("浓缩", "凝珠", "手洗", "内衣", "婴儿", "宝宝",
                          "敏感", "孕妇", "香氛", "精油", "大师", "香护",
                          "除菌", "消毒", "袋装", "快洗", "植沐", "悦色", "柔顺",
                          "亲肤", "椰油", "氨基酸", "低泡", "柔护", "APG"),
}

# 小规格瓶装溢价系数（water：280-410ml 小瓶单 ml 价高于 500-570ml 标准瓶，
# 折算 500ml 后系统性偏高；统一放宽到该系数再比，避免小瓶装真优惠误伤；
# 尝鲜 348ml×2 ¥10.01 折算 ¥7.19 仍远超 2.80×1.4×1.2=¥4.70 照拦）。
SMALL_SPEC_BRAND_K = {"water": 1.4}

BRAND_OVERPRICE_K = 1.2

# 页面可编辑的品牌基准：数据库 brand_reference_overrides（改默认基准价/说明）
# 与 brand_reference_custom（新增品牌）在进程内缓存的合并视图。判断函数是纯函数，
# 通过模块级缓存读取；refresh_if_stale 与保存动作都会刷新，保证页面改价实时生效。
_BRAND_OVERRIDES = {}   # {(topic, brand_key): price_cents}
_BRAND_CUSTOM = {}      # {(topic, brand_key): {"terms":..., "unit":..., "price_cents":..., "note":...}}
_BRAND_LIBRARY_LOADED = False

UNIT_LABELS = {
    "pack100": "元/100抽", "pack": "元/包", "bottle500": "元/瓶(500ml)",
    "bottle": "元/瓶", "liter": "元/升", "kg": "元/公斤", "ml": "元/毫升",
}


def _load_brand_reference(db):
    """Refresh in-process overrides and custom brands from the database."""
    global _BRAND_OVERRIDES, _BRAND_CUSTOM, _BRAND_LIBRARY_LOADED
    overrides = {}
    custom = {}
    if db is not None:
        try:
            for row in db.execute(
                "SELECT topic, brand_key, price_cents FROM brand_reference_overrides"
            ).fetchall():
                if isinstance(row["price_cents"], int) and row["price_cents"] > 0:
                    overrides[(row["topic"], row["brand_key"])] = row["price_cents"]
            for row in db.execute(
                "SELECT topic, brand_key, terms_json, unit, price_cents, note "
                "FROM brand_reference_custom"
            ).fetchall():
                try:
                    terms = json.loads(row["terms_json"])
                except (TypeError, ValueError):
                    terms = []
                custom[(row["topic"], row["brand_key"])] = {
                    "terms": tuple(terms) if isinstance(terms, list) else (),
                    "unit": row["unit"], "price_cents": row["price_cents"],
                    "note": row["note"] or "",
                }
        except Exception:
            # 表不存在（旧库）时静默降级为纯代码配置，不阻塞启动
            overrides, custom = {}, {}
    _BRAND_OVERRIDES, _BRAND_CUSTOM, _BRAND_LIBRARY_LOADED = overrides, custom, True


def brand_specs(topic):
    """Merged brand specs for a topic: code defaults + page-added custom brands.

    Custom brands never collide with a default brand key (same key = override
    of the default spec's identity fields is not supported; use overrides for
    price only).
    """
    merged = dict(BRAND_REFERENCE_CONFIG.get(topic) or {})
    for (t, brand), spec in _BRAND_CUSTOM.items():
        if t == topic and brand not in merged:
            merged[brand] = spec
    return merged


def effective_brand_price(topic, brand, spec):
    """Apply a page-edited override price on top of the code default."""
    overridden = _BRAND_OVERRIDES.get((topic, brand))
    if isinstance(overridden, int) and overridden > 0:
        return overridden
    return spec.get("price_cents")


def apply_brand_override(db, topic, brand_key, price_cents, note=None):
    """Persist a price override and refresh the in-process view immediately."""
    db.execute(
        "INSERT INTO brand_reference_overrides (topic, brand_key, price_cents, note) "
        "VALUES (?,?,?,?) "
        "ON CONFLICT(topic, brand_key) DO UPDATE SET price_cents=excluded.price_cents, "
        "note=excluded.note", (topic, brand_key, int(price_cents), note or "")
    )
    _BRAND_OVERRIDES[(topic, brand_key)] = int(price_cents)


def add_custom_brand(db, topic, brand_key, terms, unit, price_cents, note=None):
    """Persist a new page-added brand and refresh the in-process view immediately."""
    db.execute(
        "INSERT INTO brand_reference_custom (topic, brand_key, terms_json, unit, price_cents, note) "
        "VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(topic, brand_key) DO UPDATE SET terms_json=excluded.terms_json, "
        "unit=excluded.unit, price_cents=excluded.price_cents, note=excluded.note",
        (topic, brand_key, json.dumps(list(terms), ensure_ascii=False), unit,
         int(price_cents), note or "")
    )
    _BRAND_CUSTOM[(topic, brand_key)] = {
        "terms": tuple(terms), "unit": unit, "price_cents": int(price_cents), "note": note or ""}


def brand_from_title(title, topic):
    """Longest brand term (incl. aliases) present in a title for a managed
    topic, from BRAND_REFERENCE_CONFIG plus page-added custom brands (or None).
    English aliases match case-insensitively; longer terms win
    ("华润五丰" over "五丰")."""
    if not title:
        return None
    lowered = title.lower()
    best = None
    best_brand = None
    for brand, spec in brand_specs(topic).items():
        for term in spec.get("terms") or ():
            if term.lower() in lowered and (best is None or len(term) > len(best)):
                best = term
                best_brand = brand
    return best_brand


def spec_unit_price_cents(title, final_cents):
    """Convert a final price to a standard-unit price (one 500ml bottle, one
    100-sheet pack, one kg, one litre) so a brand market comparison is
    meaningful across sizes.  Returns (unit_label, unit_price_cents) or None
    when no usable spec can be extracted.

    Multipliers: 箱×瓶/包, 袋×kg, 瓶×升.  Precedence: kg > litre > bottle+ml
    (500ml) > bottle > pack+sheets (100-sheet pack) > pack.  A lone ml without
    a bottle count ("570ml/瓶" style unit descriptions) is NOT a total-amount
    spec and never converts.  Bulk deals (24瓶/10kg*5袋/400抽30包) collapse to
    a low standard-unit price; 小规格尝鲜 (348ml×2 at ¥10) shows an inflated
    unit price that the brand market check catches.
    """
    if not isinstance(final_cents, int) or isinstance(final_cents, bool) or final_cents <= 0:
        return None
    title = title or ""
    specs = {}
    for m in _SPEC_RE.finditer(title):
        value = float(m.group(1))
        unit = m.group(2).lower()
        if unit in ("g", "克"):
            value /= 1000.0
        elif unit == "斤":
            value *= 0.5
        family = _SPEC_UNIT.get(unit)
        if family is not None:
            specs[family] = max(specs.get(family, 0.0), value)
    kg_times_hit = False
    for m in _TIMES_RE.finditer(title):
        value = float(m.group(1))
        unit = m.group(2).lower()
        n = float(m.group(3))
        family = _SPEC_UNIT.get(unit)
        if family is not None and n > 1:
            if unit in ("g", "克"):
                value /= 1000.0
            elif unit == "斤":
                value *= 0.5
            specs[family] = max(specs.get(family, 0.0), value * n)
            if family == "kg":
                kg_times_hit = True
    for m in _PACK_TIMES_RE.finditer(title):
        n = float(m.group(3))
        if n > 1:
            specs["pack"] = max(specs.get("pack", 0.0), float(m.group(1)) * n)
    # 加号组合量贩装："18+6包"=24包、"5.68L+5L"=10.68L（防把组合装当单规格误伤）
    plus_pack = re.search(r"(\d+)\s*\+\s*(\d+)\s*(包|提)", title)
    if plus_pack:
        specs["pack"] = float(plus_pack.group(1)) + float(plus_pack.group(2))
    plus_liter = re.search(r"(\d+(?:\.\d+)?)\s*[Ll升]\s*\+\s*(\d+(?:\.\d+)?)\s*[Ll升]", title)
    if plus_liter:
        specs["liter"] = float(plus_liter.group(1)) + float(plus_liter.group(2))
    # 无乘号桶数："5L4桶"=20L（"5L*4桶"已由 _TIMES_RE 处理，此处不重复）
    l_bucket = re.search(r"(\d+(?:\.\d+)?)\s*[Ll升]\s*(\d+)\s*桶", title)
    if l_bucket and specs.get("liter"):
        specs["liter"] *= float(l_bucket.group(2))
    box = _BOX_RE.search(title)
    bag = _BAG_RE.search(title)
    if not specs:
        return None
    box_n = float(box.group(1)) if box else 1.0
    bag_n = float(bag.group(1)) if bag else 1.0
    bottle = specs.get("bottle")
    ml = specs.get("ml")
    kg = specs.get("kg")
    liter = specs.get("liter")
    pack = specs.get("pack")
    sheet = specs.get("sheet")
    if kg and kg > 0:
        if kg_times_hit:
            total = kg
        else:
            total = kg * bag_n * (bottle or 1.0)
        return ("kg", int(final_cents / total))
    if liter and liter > 0:
        total = liter * (bottle or 1.0) * box_n
        return ("liter", int(final_cents / total))
    if bottle and ml and bottle > 0 and ml > 0:
        total = bottle * box_n
        return ("bottle500", int(final_cents / total * 500.0 / ml))
    if bottle and bottle > 0:
        total = bottle * box_n
        return ("bottle", int(final_cents / total))
    if pack and sheet and pack > 0 and sheet > 0:
        total = pack * box_n
        return ("pack100", int(final_cents / total * 100.0 / sheet))
    if pack and pack > 0:
        total = pack * box_n
        return ("pack", int(final_cents / total))
    return None


_PACK_TIMES_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(包|提)\s*[x×*]\s*(\d+(?:\.\d+)?)")


def bulk_kg_or_none(title):
    """Total bulk mass in kg parsed from a title, or None when unparseable.
    Supports kg/斤/g/克 with optional ×N multipliers ("5kg×2袋", "500g*10").
    Used only for small-pack premium exemption in flour (<5kg) and laundry
    (<2kg): 小包装单价系统性高于大包装，普通标准价判据会误伤真品。"""
    total = 0.0
    has = False
    for m in re.finditer(
            r"(\d+(?:\.\d+)?)\s*(kg|斤|g|克)\s*(?:[x×*]\s*(\d+(?:\.\d+)?))?",
            title, re.IGNORECASE):
        value = float(m.group(1))
        unit = m.group(2).lower()
        n = float(m.group(3)) if m.group(3) else 1.0
        if unit in ("g", "克"):
            value /= 1000.0
        elif unit == "斤":
            value *= 0.5
        total = max(total, value * n)
        has = True
    return total if has else None


def is_brand_market_overpriced(topic, title, final_cents):
    """Whether a branded item's standard-unit price is ABOVE the brand's
    standard market price (user: 优惠价金额只要小于等于品牌标准市场价；品牌品
    都是标准价格，小于市场价才是真优惠).  The reference is the maintained
    BRAND_STANDARD_PRICES table (per brand per topic, standard selling unit),
    editable like the category dictionary.  Unknown brand, unparseable spec or
    missing reference never gate (conservative, protects genuine bargains).
    """
    brand = brand_from_title(title, topic)
    if brand is None:
        return False
    if any(term in title for term in BRAND_EXEMPT_TERMS.get(topic, ())):
        return False
    priced = spec_unit_price_cents(title, final_cents)
    if priced is None:
        return False
    unit_label, unit_price = priced
    if unit_label == "pack":
        # 无抽数标题（"蓝漂抽纸10包装"）折算为"包"单价；主流纸包 100-140 抽，
        # 按 pack100 基准比较（低价纸 3 元/包=300 分 > 基准 150×1.2 拦截，
        # 130抽装 3 元/包仍在中端基准容差内不误伤）
        unit_label = "pack100"
    spec = brand_specs(topic).get(brand)
    if not isinstance(spec, dict):
        return False
    standard = effective_brand_price(topic, brand, spec)
    if spec.get("unit") != unit_label:
        # 多单位基准：如 water 品牌主单位 bottle500（单瓶），大瓶/桶装折算为
        # liter 时用 extra_units["liter"]（每升市场价）作参照，否则品牌判据失效
        # （用户：怡宝/农夫山泉/娃哈哈等对应产品单价都要有）。
        extra = spec.get("extra_units") or {}
        if not isinstance(extra, dict) or unit_label not in extra:
            return False
        standard = extra[unit_label]
    if not isinstance(standard, int):
        return False
    if topic == "flour":
        bulk = bulk_kg_or_none(title)
        if bulk is not None and bulk < 5.0:
            return False
    elif topic == "rice":
        bulk = bulk_kg_or_none(title)
        if bulk is not None and bulk < 2.5:
            return False
    elif topic == "laundry_detergent":
        bulk = bulk_kg_or_none(title)
        if bulk is not None and bulk < 2.0:
            return False
    factor = SMALL_SPEC_BRAND_K.get(topic, 1.0)
    if "箱" in title or "整箱" in title:
        # 整箱/大包装为常规规格，无小规格尝鲜溢价：用标准基准直接比较
        # （农夫山泉 550ml×24 整箱 3 元/瓶、百岁山整箱 4.58 元/瓶均应拦截；
        #  怡宝 4.5L×4桶×10箱 1.94 元/L 真便宜不误伤）
        # 但 water 小瓶（<450ml）整箱仍保留 1.4 小瓶溢价保护（380ml×24 正常价不误伤）
        small_bottle = False
        if topic == "water":
            ml_m = re.search(r"(\d+)\s*ml", title)
            if ml_m and int(ml_m.group(1)) < 450:
                small_bottle = True
        if not small_bottle:
            factor = 1.0
    if topic == "paper" and unit_label == "pack100":
        sm = re.search(r"(\d+(?:\.\d+)?)\s*抽", title)
        if sm and float(sm.group(1)) < 100.0:
            factor = max(factor, 1.3)
    return unit_price > int(standard * factor * BRAND_OVERPRICE_K)


BRAND_BASELINE_RANKS = {
    # ratio thresholds -> (state, label, rank), aligned with maintained_price_baselines
    "bargain": 0.60,   # 折算标准单价 ≤ 市场价 6 折 = 可捡漏
    "low": 0.85,       # ≤ 8.5 折 = 优惠
    "typical": 1.00,   # ≤ 市场价 = 低于常规
    "regular": 1.20,   # ≤ 市场价×1.2 = 常规
}
BRAND_BASELINE_UNVERIFIED_RATIO = 0.30  # 便宜到离谱：不标可捡漏，防假货/解析异常霸榜
BRAND_BASELINE_EXCLUDE_TERMS = {
    # 非本品类混品（单价通常低于本品类，用本品类标准价比较会误标"可捡漏"）：
    # 黄小米/杂粮等不是大米，不能用大米品牌标准价下结论。
    "rice": ("小米", "黄小米", "杂粮", "糯米", "黑米", "糙米", "藜麦", "燕麦米"),
}


def _brand_standard_reference(topic, brand):
    """Return (standard_cents, unit_label) or None when the brand has no
    usable maintained standard-market reference for this topic."""
    spec = brand_specs(topic).get(brand)
    if not isinstance(spec, dict):
        return None
    standard = effective_brand_price(topic, brand, spec)
    unit = spec.get("unit")
    if not isinstance(standard, int) or not isinstance(unit, str) or not unit:
        return None
    return standard, unit


def assess_brand_baseline(topic, title, amount_cents):
    """Positive-side bargain assessment using the maintained brand standard
    market prices (BRAND_REFERENCE_CONFIG, the operator-editable config the
    user asked to keep comprehensive).  Returns the same schema as
    maintained_price_baselines.assess_maintained_baseline so lists, detail
    pages and the 'maintained-baseline bargain first' sort all work unchanged.

    Only branded items whose spec converts to the brand's standard selling
    unit are assessed; exempt sub-categories (乳霜/悬挂/菜籽油/烘焙面粉…)
    have no standard-unit baseline and never get a conclusion (conservative,
    no mislabelling).  ratio < 0.30 is flagged unverified instead of 'bargain'
    so absurdly cheap rows (fake/lookup errors) never rank as genuine deals.
    """
    base = {
        "state": "no_match", "label": "尚无匹配品牌基准", "note": "",
        "rank": 0, "baseline_id": None, "baseline_name": "",
        "baseline_version": None, "unit_label": "", "current_unit_cents": None,
        "bargain_cents": None, "low_cents": None, "typical_cents": None,
        "high_cents": None, "typical_delta_cents": None, "matched_by": "",
        "ratio": None,
    }
    if not isinstance(amount_cents, int) or isinstance(amount_cents, bool) or amount_cents <= 0:
        return base
    topic = topic or "other"
    brand = brand_from_title(title or "", topic)
    if brand is None:
        return base
    if any(term in (title or "") for term in BRAND_BASELINE_EXCLUDE_TERMS.get(topic, ())):
        return base
    exempted = any(term in (title or "") for term in BRAND_EXEMPT_TERMS.get(topic, ()))
    ref = _brand_standard_reference(topic, brand)
    if ref is None:
        return base
    # 注意：不检查 BRAND_EXEMPT_TERMS——豁免词只作用于拦截侧（细分品类单价
    # 高不拦）；捡漏侧"折算低于品牌标准市场价"本身就是真便宜的强证据，忽略
    # 豁免词才能让柴火五常米/新疆面粉等真品获得捡漏标注，不浪费已维护的标准价。
    standard, unit = ref
    priced = spec_unit_price_cents(title or "", amount_cents)
    if priced is None:
        return base
    unit_label, unit_price = priced
    if unit_label != unit:
        return base
    ratio = unit_price / standard
    if exempted and ratio > BRAND_BASELINE_RANKS["typical"]:
        return base
    note = "该结论仅按本工作台维护的品牌标准市场价计算，不代表账号结算价或全网最低价。"
    matched_by = (f"品牌「{brand}」按标题规格折算为 {unit_price / 100:.2f} 元/{unit_label}"
                  f"，与配置标准市场价 {standard / 100:.2f} 元/{unit_label} 比较")
    if ratio < BRAND_BASELINE_UNVERIFIED_RATIO:
        return {**base, "state": "unverified_brand", "label": "按品牌基准：异常偏低待复核",
                "note": note, "baseline_name": brand,
                "baseline_version": "品牌标准市场价配置表", "effective_from": "持续生效",
                "unit_label": unit_label, "current_unit_cents": unit_price,
                "bargain_cents": int(standard * BRAND_BASELINE_RANKS["bargain"]),
                "low_cents": int(standard * BRAND_BASELINE_RANKS["low"]),
                "typical_cents": standard, "high_cents": int(standard * BRAND_BASELINE_RANKS["regular"]),
                "typical_delta_cents": standard - unit_price, "matched_by": matched_by,
                "ratio": ratio}
    for state, label, rank in (("maintained_bargain", "按品牌基准：可捡漏", 5),
                               ("maintained_low", "按品牌基准：优惠", 4),
                               ("maintained_below_typical", "按品牌基准：低于常规", 3),
                               ("maintained_regular", "按品牌基准：常规价格", 2),
                               ("maintained_high", "按品牌基准：价格偏高", 1)):
        if ratio <= BRAND_BASELINE_RANKS["regular"] or state == "maintained_high":
            threshold = BRAND_BASELINE_RANKS.get(state.split("_")[-1], None)
            # bargain/low/below_typical use their own upper bound; regular/high
            # fall through when ratio <= 1.2 / above it.
            if state in ("maintained_bargain", "maintained_low", "maintained_below_typical"):
                bound = {"maintained_bargain": 0.60, "maintained_low": 0.85,
                         "maintained_below_typical": 1.00}[state]
                if ratio > bound:
                    continue
            return {**base, "state": state, "label": label, "rank": rank,
                    "note": note, "baseline_name": brand,
                    "baseline_version": "品牌标准市场价配置表", "effective_from": "持续生效",
                    "unit_label": unit_label, "current_unit_cents": unit_price,
                    "bargain_cents": int(standard * BRAND_BASELINE_RANKS["bargain"]),
                    "low_cents": int(standard * BRAND_BASELINE_RANKS["low"]),
                    "typical_cents": standard,
                    "high_cents": int(standard * BRAND_BASELINE_RANKS["regular"]),
                    "typical_delta_cents": standard - unit_price, "matched_by": matched_by,
                    "ratio": ratio}
    return base


def small_spec_limits(title):
    """Whether a title describes a small-specification item in every measured
    unit (e.g. "30抽宝宝用" or "348ml*2瓶").  Titles with a large unit (整箱/
    30包/24瓶/1000张) return False so bulk deals are never gated; titles with
    no extractable spec return False as well."""
    if not title:
        return False
    seen = {}
    for m in _SPEC_RE.finditer(title):
        value = float(m.group(1))
        family = _SPEC_UNIT.get(m.group(2).lower())
        if family is not None:
            seen[family] = max(seen.get(family, 0.0), value)
    if not seen:
        return False
    return all(value < SMALL_SPEC_LIMITS[family] for family, value in seen.items())


def has_trial_gating_marker(title):
    """Whether the title carries a trial/first-order traffic marker.

    Only removes the discount identity (kept on the default list with the
    promotion price shown); unknown/empty titles return False so nothing is
    gated by default.
    """
    return bool(title) and any(word in title for word in TRIAL_GATING_TERMS)


def is_gated_price(topic, cents):
    """Whether a price is a unified first-order/subsidy traffic price.

    Checked against the final price of every discount path (reference gap,
    coupon or full promotion path), not only the reference-price gap, because
    the same subsidy price shows up as a coupon-applied / path-estimate total.
    Unknown topics and absent calibration never gate.
    """
    if not isinstance(cents, int) or isinstance(cents, bool) or cents <= 0:
        return False
    payload = get_calibration().get(topic) or {}
    gated = payload.get("gated_prices") or []
    return cents in gated


def category_matches(topic, title):
    """Whether a title belongs to the topic's product category.

    ``None``/unknown topic returns True (no category gate for custom sources);
    a managed topic requires at least one include term and no interfering term.
    """
    if not title:
        return True
    terms = TOPIC_CATEGORY_TERMS.get(topic)
    if not terms:
        return True
    include, exclude = terms
    if not any(word in title for word in include):
        return False
    if any(word in title for word in exclude) or any(
            word in title for word in _GENERIC_INTERFERING_TERMS):
        return False
    return True

_CACHE = None   # dict: topic -> calibration payload, or None


def topic_key(source_name):
    """Map a managed living-topic source name to its topic key (or None)."""
    name = " ".join(str(source_name or "").split())
    if not name:
        return None
    for topic in _core_topics():
        # name ("纸品") covers official source naming ("淘宝生活品·纸品优惠搜索"),
        # keyword ("抽纸") covers the original dev naming ("淘宝店抽纸优惠搜索").
        if topic.name in name or topic.keyword in name:
            return topic.key
    return None


def _import_topics():
    from services.taobao_living_topics import CORE_LIVING_TOPICS
    return CORE_LIVING_TOPICS


_CORE_TOPICS = None


def _core_topics():
    global _CORE_TOPICS
    if _CORE_TOPICS is None:
        _CORE_TOPICS = _import_topics()
    return _CORE_TOPICS


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _quantile(values, p):
    ordered = sorted(values)
    return ordered[min(int(p * len(ordered)), len(ordered) - 1)]


def _bin_of(log_promo):
    index = int((log_promo - BIN_LO) / LOG_BIN_WIDTH)
    lo = BIN_LO + index * LOG_BIN_WIDTH
    hi = lo + LOG_BIN_WIDTH
    if hi > BIN_HI:
        hi = BIN_HI
    return (round(lo, 4), round(hi, 4))


def _bins():
    bins = []
    lo = BIN_LO
    while lo < BIN_HI - 1e-9:
        hi = min(lo + LOG_BIN_WIDTH, BIN_HI)
        bins.append((round(lo, 4), round(hi, 4)))
        lo = hi
    return bins


def calibrate(db):
    """Estimate plausibility statistics per managed topic from the corpus.

    Returns a dict keyed by topic key; each payload holds the price ceiling,
    per-bin ratio/drop caps, global caps, sample counts and a timestamp.
    """
    from services.taobao_living_topics import CORE_LIVING_TOPICS
    # Match both the topic name ("纸品") and the original keyword ("抽纸") so
    # official production source naming ("淘宝生活品·纸品优惠搜索") is collected.
    clauses, params = [], []
    for topic in CORE_LIVING_TOPICS:
        for needle in (topic.name, topic.keyword):
            clauses.append("s.name LIKE ?")
            params.append(f"%{needle}%")
    keyword_clause = " OR ".join(clauses)
    rows = db.execute(f"""SELECT s.name AS source_name, e.metadata_json
        FROM opportunities o JOIN events e ON e.id = o.event_id
        JOIN sources s ON s.id = o.source_id
        WHERE s.method = 'taobao_alliance' AND ({keyword_clause})
        AND length(e.metadata_json) > 2""", params).fetchall()

    by_topic = {topic.key: [] for topic in CORE_LIVING_TOPICS}
    promo_values = {topic.key: [] for topic in CORE_LIVING_TOPICS}
    brand_units = {}
    for row in rows:
        topic = topic_key(row["source_name"])
        if topic is None:
            continue
        try:
            offer = json.loads(row["metadata_json"] or "{}").get("taobao_alliance_offer") or {}
        except (ValueError, TypeError):
            continue
        promo = offer.get("promotion_price_cents")
        reserve = offer.get("reserve_price_cents")
        if not isinstance(promo, int) or isinstance(promo, bool) or promo <= 0:
            continue
        promo_values[topic].append(promo)
        if isinstance(reserve, int) and not isinstance(reserve, bool) and reserve > promo:
            ratio = (reserve - promo) / reserve
            by_topic[topic].append((math.log10(promo), ratio, reserve - promo))
        else:
            by_topic[topic].append((math.log10(promo), None, 0))
        # 品牌单品参考价：命中品牌且可折算标准单位价的记录，按 主题-品牌-单位 收集
        title = row["title"] if ("title" in row.keys() and row["title"]) else ""
        brand = brand_from_title(title, topic)
        if brand is not None:
            unit = spec_unit_price_cents(title, promo)
            if unit is not None:
                brand_units.setdefault(topic, {}).setdefault(brand, {}).setdefault(unit[0], []).append(unit[1])

    result = {}
    for topic, pairs in by_topic.items():
        if not pairs:
            continue
        log_promos = [lp for lp, _r, _d in pairs]
        ordered = sorted(log_promos)
        median = ordered[len(ordered) // 2]
        mad = _quantile([abs(x - median) for x in ordered], 0.5)
        ceiling = int(10 ** (median + CEILING_MAD_K * mad)) + 1

        clean = [(lp, r, d) for lp, r, d in pairs if r is not None and r <= CUT
                 and d <= 3 * 10 ** lp]
        global_ratio = _quantile([r for _lp, r, _d in clean], P) if len(clean) >= MIN_BIN_SAMPLES else None
        global_drop = int(_quantile([d for _lp, _r, d in clean], P)) if len(clean) >= MIN_BIN_SAMPLES else None
        # Premium bins (promotion price at/above the bin that holds the topic's
        # P97) are exempt from the ratio/drop caps: deep discounts on genuine
        # premium goods (12-bottle low-deuterium water, 5L camellia-oil gift
        # sets) are normal there, and the absurd-price ceiling is the only guard
        # above this line.  Taking the bin's upper edge keeps edge cases (e.g. a
        # 69.9-yuan tissue at the P97 boundary) inside the strict band.
        p97_log = _quantile(sorted(log_promos), 0.97)
        high_log = _bin_of(p97_log)[1]

        # High-tier strikethrough plausibility: for promotion prices at/above
        # the P97 bin edge, ratio/drop caps do not apply (genuine premium deep
        # discounts are normal), so we bound the strikethrough itself with the
        # reserve/promo ratio distribution of the same high tier (trimmed the
        # same way as the caps).  Few high-tier samples fall back to the
        # topic-wide trimmed reserve/promo ratio.
        high_ratios = sorted(
            d / (10 ** lp) + 1.0
            for lp, r, d in clean if 10 ** lp >= high_log and r is not None and d > 0)
        topic_ratios = sorted(
            d / (10 ** lp) + 1.0
            for lp, r, d in clean if r is not None and d > 0)
        if len(high_ratios) >= MIN_BIN_SAMPLES:
            high_reserve_ratio_cap = _quantile(high_ratios, 0.90)
        elif len(topic_ratios) >= MIN_BIN_SAMPLES:
            high_reserve_ratio_cap = _quantile(topic_ratios, 0.90)
        else:
            high_reserve_ratio_cap = None

        bins = []
        for lo, hi in _bins():
            box = [item for item in clean if lo <= item[0] < hi]
            if len(box) < MIN_BIN_SAMPLES:
                bins.append((lo, hi, None, None))
                continue
            bins.append((
                lo, hi,
                round(_quantile([r for _lp, r, _d in box], P), 4),
                int(_quantile([d for _lp, _r, d in box], P)),
            ))

        promo_cents = sorted(promo_values[topic])
        median_cents = promo_cents[len(promo_cents) // 2]
        mad_cents = _quantile([abs(x - median_cents) for x in promo_cents], 0.5)
        result[topic] = {
            "price_ceiling_cents": ceiling,
            "high_log": round(high_log, 4),
            "high_reserve_ratio_cap": high_reserve_ratio_cap,
            "bins": bins,
            "global_ratio_cap": global_ratio,
            "global_drop_cap": global_drop,
            "gated_prices": _gated_promo_prices(promo_values[topic]),
            "low_price_floor": int(_quantile(promo_cents, 0.15)) if promo_cents else None,
            "price_p50_cents": median_cents,
            "market_band_low": int(median_cents - MARKET_BAND_MAD_K * mad_cents) if promo_cents else None,
            "market_band_high": int(median_cents + MARKET_BAND_MAD_K * mad_cents) if promo_cents else None,
            "deep_drop_threshold": round(_quantile([r for _lp, r, _d in pairs if r is not None], DEEP_DROP_QUANTILE), 4)
                if any(r is not None for _lp, r, _d in pairs) else None,
            "sample_count": len(pairs),
            "brand_reference_prices": {
                brand: {
                    unit_label: {
                        "median_cents": int(_quantile(values, BRAND_REFERENCE_QUANTILE)),
                        "sample_count": len(values),
                    }
                    for unit_label, values in units.items()
                }
                for brand, units in (brand_units.get(topic) or {}).items()
            },
            "calibrated_at": _now_iso(),
        }
    return result


def _gated_promo_prices(promo_values):
    """Unified first-order/subsidy prices: a low-band promotion price shared
    verbatim by many distinct listings (e.g. 2.01-yuan tissues) is a traffic
    SKU signal, not a market price.  Only prices below the topic median qualify,
    so genuine bulk deals in the mid/upper band are never gated."""
    if not promo_values:
        return []
    counts = collections.Counter(promo_values)
    ordered = sorted(promo_values)
    median = ordered[len(ordered) // 2]
    return sorted(p for p, n in counts.items()
                  if n >= GATED_MIN_COUNT and p < median)


def is_low_value_small_item(topic, cents, title=None):
    """Whether a final price is a low-value small-item price.

    Cheap single packs / single bottles (a 2-yuan tissue pack, a 5-yuan flour
    bag) are not "real bargains with big savings" for the user even when the
    strikethrough looks deep; they must not surface a discount amount.  Two
    data-driven routes:
      * spec route (title carries a spec): small in every measured unit
        (30抽/348ml*2瓶) AND below the topic median price → small item.  Bulk
        deals (400抽30包/24瓶/1000张) exceed a unit limit and are never gated.
      * price route (no spec): below the corpus P15 floor of promotion prices.
    The floors are estimated from the corpus, never hard-coded, and only the
    discount identity is dropped — the listing stays on the default list with
    its promotion price shown.
    """
    if not isinstance(cents, int) or isinstance(cents, bool) or cents <= 0:
        return False
    payload = get_calibration().get(topic) or {}
    if small_spec_limits(title):
        median = payload.get("price_p50_cents")
        return isinstance(median, int) and cents < median
    floor = payload.get("low_price_floor")
    return isinstance(floor, int) and cents < floor


def is_high_tier_unverifiable(topic, title, promo_cents):
    """Whether a high-tier promotion price has no verifiable market reference.

    At/above the topic P97 bin edge, a promotion price that is neither a
    maintained brand (has a brand standard-unit market price) nor convertible
    to a standard unit (spec extractable) has no way to check whether it is a
    bargain or an absurd markup (a 599-yuan carton of 300 sheets of tissue, a
    120-yuan custom restaurant napkin).  Its discount identity is not
    trustworthy, so it must not surface a saving.
    """
    if not isinstance(promo_cents, int) or isinstance(promo_cents, bool) or promo_cents <= 0:
        return False
    if not title:
        return False
    payload = get_calibration().get(topic) or {}
    high_log = payload.get("high_log")
    if not isinstance(high_log, (int, float)) or math.log10(promo_cents) < high_log:
        return False
    if brand_from_title(title, topic) is not None:
        return False          # brand has a maintained market reference
    return spec_unit_price_cents(title, promo_cents) is None


def is_market_band_fake_discount(topic, final_cents, ratio):
    """Whether a deep strikethrough lands the final price inside the topic's
    market normal-price band (median +/- k*MAD of promotion prices).

    A final price priced "like everywhere else" plus a deep discount label is
    an inflated-reference marketing trick, not a real bargain (user: 最后算下来
    跟其他平台价格一样 相差就一两块钱 肯定不算数).  The band and the deep-drop
    threshold are estimated from the corpus; unknown topics never gate.
    """
    if not isinstance(final_cents, int) or isinstance(final_cents, bool) or final_cents <= 0:
        return False
    if not isinstance(ratio, (int, float)) or not (0 < ratio < 1):
        return False
    payload = get_calibration().get(topic) or {}
    low = payload.get("market_band_low")
    high = payload.get("market_band_high")
    deep = payload.get("deep_drop_threshold")
    if not (isinstance(low, int) and isinstance(high, int) and isinstance(deep, float)):
        return False
    return low <= final_cents <= high and ratio >= deep


CALIBRATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS discount_calibration (
 topic TEXT PRIMARY KEY,
 payload TEXT NOT NULL,
 sample_count INTEGER NOT NULL,
 updated_at TEXT NOT NULL
);
"""


def save(db, calibration):
    db.executescript(CALIBRATION_SCHEMA)
    for topic, payload in calibration.items():
        db.execute(
            "INSERT OR REPLACE INTO discount_calibration(topic,payload,sample_count,updated_at) VALUES(?,?,?,?)",
            (topic, json.dumps(payload, ensure_ascii=False), payload["sample_count"], payload["calibrated_at"]))


def load(db):
    """Load persisted calibration into the in-memory cache. Returns the cache."""
    global _CACHE
    rows = db.execute("SELECT topic, payload FROM discount_calibration").fetchall()
    _CACHE = {row["topic"]: json.loads(row["payload"]) for row in rows}
    return _CACHE


def refresh_if_stale(db, max_age_seconds=REFRESH_INTERVAL_SECONDS, now=None):
    """Recompute calibration when the persisted snapshot is older than max_age."""
    now = now or datetime.now(timezone.utc)
    _load_brand_reference(db)
    rows = db.execute("SELECT topic, updated_at FROM discount_calibration").fetchall()
    fresh = all(
        (now - datetime.fromisoformat(row["updated_at"])).total_seconds() < max_age_seconds
        for row in rows
    ) if rows else False
    if rows and _CACHE is None:
        # A fresh persisted snapshot must still be loaded into a cold process:
        # otherwise a restarted web/worker (or any new process) silently falls
        # back to the looser seed caps (0.85 / 50 yuan) until the snapshot ages
        # out, letting 74-85% fake strikethroughs through for up to an hour.
        load(db)
    if not fresh:
        calibration = calibrate(db)
        if calibration:
            save(db, calibration)
            load(db)
            return calibration
    return _CACHE


def seed_calibration():
    """Conservative defaults so a missing calibration still protects users."""
    payload = {
        "price_ceiling_cents": None,
        "bins": [],
        "global_ratio_cap": SEED_RATIO_CAP,
        "global_drop_cap": SEED_DROP_CAP_CENTS,
        "sample_count": 0,
        "calibrated_at": "",
    }
    return {key: dict(payload, price_ceiling_cents=cents)
            for key, cents in SEED_PRICE_CEILING_CENTS.items()}


def get_calibration():
    """Return the current calibration cache (seeds when never loaded)."""
    if _CACHE is None:
        return seed_calibration()
    return _CACHE


def set_calibration(calibration):
    """Replace the in-memory calibration (used by tests and refresh)."""
    global _CACHE
    _CACHE = calibration
    return _CACHE


def price_ceiling_cents(topic):
    """Data-driven absurd-price ceiling for a topic, or the seed ceiling."""
    payload = get_calibration().get(topic)
    ceiling = (payload or {}).get("price_ceiling_cents")
    if isinstance(ceiling, int):
        return ceiling
    return SEED_PRICE_CEILING_CENTS.get(topic)


def _lookup_caps(topic, promotion_cents):
    """Return (ratio_cap, drop_cap) for a promotion price, or (None, None)."""
    payload = get_calibration().get(topic)
    if not payload:
        return None, None
    if promotion_cents and promotion_cents > 0:
        log_promo = math.log10(promotion_cents)
        target = _bin_of(log_promo)
        for lo, hi, ratio_cap, drop_cap in payload.get("bins", []):
            if (lo, hi) == target and ratio_cap is not None:
                return ratio_cap, drop_cap
    return payload.get("global_ratio_cap"), payload.get("global_drop_cap")


def judge_drop(topic, reserve_cents, promotion_cents, shop_bad=False):
    """Data-driven (plausible, reason) for a reference-price reduction.

    ``topic`` may be ``None`` for sources outside the managed living topics;
    those still get the global caps or the conservative seeds.  ``shop_bad``
    marks a listing from a "瞎搞小店"（店铺离谱率高，见 services/shop_reputation）:
    its caps are tightened by SHOP_BAD_CAP_K so that borderline absurd
    strikethroughs from low-trust shops no longer surface (user: 很多店铺都是
    瞎搞的，数据太离谱就不展示).
    """
    from services.shop_reputation import SHOP_BAD_CAP_K
    if not (isinstance(reserve_cents, int) and isinstance(promotion_cents, int)
            and not isinstance(reserve_cents, bool) and not isinstance(promotion_cents, bool)
            and reserve_cents > promotion_cents > 0):
        return False, "无有效参考价差"

    ceiling = price_ceiling_cents(topic)
    if ceiling and promotion_cents > ceiling:
        return False, "推广价超出当前生活品主题合理上界（由校准分布估计），划线价不作为优惠证据"

    payload = get_calibration().get(topic) or {}
    gated = payload.get("gated_prices") or []
    if gated and promotion_cents in gated:
        return False, "低价引流统一价（首单/新人/试饮等小规格引流 SKU），不作为优惠证据"

    drop = reserve_cents - promotion_cents
    ratio = drop / reserve_cents
    # Premium prices (>= topic P97 bin edge) are protected from per-bin caps:
    # deep discounts on genuine premium goods (12-bottle low-deuterium water,
    # camellia-oil gift sets) are normal there.  They are still bound by the
    # topic-wide drop cap so that 90%+ inflated strikethroughs (e.g. a shampoo
    # caught by the "rice" keyword) do not surface a huge fake saving.
    high_log = (payload or {}).get("high_log")
    global_drop = (payload or {}).get("global_drop_cap")
    global_ratio = (payload or {}).get("global_ratio_cap")
    if high_log is not None and promotion_cents > 0 and math.log10(promotion_cents) >= high_log:
        # Premium prices (>= topic P97 bin edge) may carry deep discounts on
        # genuine premium goods (12-bottle low-deuterium water, camellia-oil
        # gift sets), so the ratio/drop caps do not apply.  They are still
        # bound by (a) the topic-wide ratio cap and (b) the plausibility of the
        # strikethrough itself: reserve/promo above the calibrated high-tier
        # ratio (e.g. a 188.9-yuan tissue carton "discounted" by 510 yuan,
        # reserve = 3.7x promo) is an inflated strikethrough, not a bargain.
        if global_ratio is not None and ratio > global_ratio:
            return False, f"降幅超过该主题全局可信上界（{global_ratio:.0%}），划线价作为优惠证据不可信"
        high_ratio_cap = (payload or {}).get("high_reserve_ratio_cap")
        if isinstance(high_ratio_cap, (int, float)):
            if shop_bad:
                high_ratio_cap = high_ratio_cap * SHOP_BAD_CAP_K
            if reserve_cents > promotion_cents * high_ratio_cap:
                return False, (f"划线价超过该主题高价段可信比值（{high_ratio_cap:.1f}倍推广价"
                               + ("，且店铺离谱率高" if shop_bad else "") + "），划线价作为优惠证据不可信")
        return True, ""

    ratio_cap, drop_cap = _lookup_caps(topic, promotion_cents)
    # The per-bin caps come from the same corpus and can drift high when a
    # bin's samples are few or contaminated; the topic-global cap (estimated
    # from trimmed samples) always stays as the floor for the plausible bound.
    if ratio_cap is not None and global_ratio is not None:
        ratio_cap = min(ratio_cap, global_ratio)
    if drop_cap is not None and isinstance(global_drop, int):
        drop_cap = min(drop_cap, global_drop)

    # Seeds apply the same semantics as the old absolute guard.
    if ratio_cap is None:
        seed_ratio_cap = SEED_RATIO_CAP * SHOP_BAD_CAP_K if shop_bad else SEED_RATIO_CAP
        seed_drop_cap = int(SEED_DROP_CAP_CENTS * SHOP_BAD_CAP_K) if shop_bad else SEED_DROP_CAP_CENTS
        if promotion_cents < SEED_LOW_PROMO_CENTS and drop > seed_drop_cap:
            return False, "低价值商品优惠额超过50元，划线价作为优惠证据不可信"
        if ratio >= seed_ratio_cap:
            return False, "降幅达到参考价85%或以上，参考价作为划线价不可信"
        if reserve_cents > promotion_cents * 20:
            return False, "参考价超过推广价20倍，参考价作为划线价不可信"
        if drop - promotion_cents > promotion_cents // 20:
            # 主动审计发现：中低价假优惠的共性 = 优惠额显著超过促销价本身
            # （蓝漂纸 ¥5.89 优惠 ¥10.01、洞庭山桶装水 ¥92.93 优惠 ¥106.07、
            # 道道全菜籽油 ¥54.8 优惠 ¥104.2 等）。真实优惠恒有 drop <= promo；
            # 半价划线（drop ≈ promo）在 5% 容差内不受影响。高价段真品
            # （低氘水等市场价 2 倍+）已在上方高价段分支豁免。
            return False, "优惠额显著超过推广价（划线价高于推广价2倍以上），划线价作为优惠证据不可信"
        return True, ""

    if shop_bad:
        ratio_cap = ratio_cap * SHOP_BAD_CAP_K if isinstance(ratio_cap, (int, float)) else ratio_cap
        drop_cap = int(drop_cap * SHOP_BAD_CAP_K) if isinstance(drop_cap, int) else drop_cap
    if ratio_cap is not None and ratio > ratio_cap:
        return False, f"降幅超过该价位校准可信上界（{ratio_cap:.0%}），划线价作为优惠证据不可信"
    if drop_cap is not None and drop > drop_cap:
        return False, f"优惠额超过该价位校准可信上界（¥{drop_cap / 100:.0f}），划线价作为优惠证据不可信"
    if drop - promotion_cents > promotion_cents // 20:
        # 同 seed 分支：中低价假优惠的硬证据（优惠额显著超过售价本身）
        return False, "优惠额显著超过推广价（划线价高于推广价2倍以上），划线价作为优惠证据不可信"
    return True, ""


def brand_reference_table(db):
    """Full merged brand library for the maintenance page: every configured
    brand with its default and effective (override-aware) reference price."""
    _load_brand_reference(db)
    rows = []
    for topic in BRAND_REFERENCE_CONFIG:
        for brand, spec in brand_specs(topic).items():
            default_cents = spec.get("price_cents")
            effective = effective_brand_price(topic, brand, spec)
            custom = (topic, brand) in _BRAND_CUSTOM
            rows.append({
                "topic": topic,
                "brand": brand,
                "terms": list(spec.get("terms") or ()),
                "unit": spec.get("unit", ""),
                "unit_label": UNIT_LABELS.get(spec.get("unit", ""), spec.get("unit", "")),
                "default_cents": default_cents,
                "price_cents": effective,
                "overridden": (topic, brand) in _BRAND_OVERRIDES,
                "custom": custom,
                "note": spec.get("note", ""),
            })
    rows.sort(key=lambda r: (r["topic"], r["brand"]))
    return rows
