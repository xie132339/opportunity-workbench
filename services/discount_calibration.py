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
    "kg": "kg", "斤": "kg",
}
_SPEC_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(抽|张|片|包|提|瓶|支|粒|ml|毫升|L|升|kg|斤)", re.IGNORECASE)
_BOX_RE = re.compile(r"(\d+(?:\.\d+)?)\s*箱")
_BAG_RE = re.compile(r"(\d+(?:\.\d+)?)\s*袋")

# 主流家用品牌词典（维护性数据，随主题关键词同步维护，非业务阈值）。
# 品牌标准市场参考价 = 人工维护表（用户明确"维护这样的数据肯定能有的"）；
# 单位为该主题的标准售卖单位市场价（用户：一件/一瓶/一包），跨主题品牌
# （金龙鱼/福临门）按主题分列。初始值按主流渠道零售常识设定，**可在
# BRAND_STANDARD_PRICES 直接增改**；判据只拦"折算标准单位到手价明显高于
# 品牌标准市场价"（系数 BRAND_OVERPRICE_K），低规格引流/划线虚高被拦，
# 整箱大促折算单价 ≤ 标准市场价则永远放行。
BRAND_TERMS = {
    "paper": ("维达", "清风", "洁柔", "心相印", "植护", "漫花", "丹娜丝", "怡飘",
              "卡布", "可优比", "全棉时代", "得宝", "斑布", "顺清柔", "理文"),
    "water": ("百岁山", "农夫山泉", "怡宝", "康师傅", "娃哈哈", "景田", "恒大冰泉",
              "依云", "昆仑山", "屈臣氏"),
    "rice": ("柴火大院", "十月稻田", "龙稻", "福临门", "金龙鱼", "北大荒", "香满园",
             "孟乍隆", "华润五丰"),
    "flour": ("金沙河", "五得利", "中裕", "金龙鱼", "福临门", "河套", "塞北雪", "新良", "展艺"),
    "cooking_oil": ("金龙鱼", "鲁花", "福临门", "多力", "胡姬花", "刀唛", "西王",
                    "九三", "长寿花", "欧丽薇兰", "千岛源"),
    "laundry_detergent": ("蓝月亮", "立白", "超能", "威露士", "奥妙", "汰渍", "碧浪",
                          "雕牌", "卫新", "花王", "狮王", "白猫"),
}
BRAND_STANDARD_PRICES = {
    "paper": {
        "维达": 300, "清风": 250, "洁柔": 300, "心相印": 280, "植护": 150, "漫花": 150,
        "丹娜丝": 150, "怡飘": 150, "卡布": 1200, "可优比": 1200, "全棉时代": 600,
        "得宝": 300, "斑布": 200, "顺清柔": 150, "理文": 150,
    },
    "water": {
        "百岁山": 280, "农夫山泉": 180, "怡宝": 180, "康师傅": 150, "娃哈哈": 180,
        "景田": 150, "恒大冰泉": 200, "依云": 1000, "昆仑山": 400, "屈臣氏": 300,
    },
    "rice": {
        "柴火大院": 4000, "十月稻田": 3000, "龙稻": 4000, "福临门": 1200, "金龙鱼": 1200,
        "北大荒": 1200, "香满园": 1000, "孟乍隆": 3000, "华润五丰": 1500,
    },
    "flour": {
        "金沙河": 400, "五得利": 400, "中裕": 600, "金龙鱼": 700, "福临门": 700,
        "河套": 1500, "塞北雪": 1000, "新良": 800, "展艺": 700,
    },
    "cooking_oil": {
        "金龙鱼": 1500, "鲁花": 2400, "福临门": 1300, "多力": 2000, "胡姬花": 2000,
        "刀唛": 3000, "西王": 1600, "九三": 1500, "长寿花": 1800, "欧丽薇兰": 3000,
        "千岛源": 3000,
    },
    "laundry_detergent": {
        "蓝月亮": 2000, "立白": 1500, "超能": 1500, "威露士": 2000, "奥妙": 1500,
        "汰渍": 1500, "碧浪": 2000, "雕牌": 1200, "卫新": 1500, "花王": 3000,
        "狮王": 3000, "白猫": 1000,
    },
}
# 品牌单品参考判据系数：折算标准单位到手价高于该品牌标准市场价达该倍数
# 以上视为"贵过市场价"（划线虚高/引流小规格）。1.2x 容忍渠道价差：整箱
# 真品折算单价 ≈ 零售市场价（1.0-1.2 倍）放行，尝鲜小规格折算单价 2-3 倍
# 拦截。
BRAND_OVERPRICE_K = 1.2


def brand_from_title(title, topic):
    """Longest brand term present in a title for a managed topic (or None)."""
    if not title:
        return None
    terms = BRAND_TERMS.get(topic) or ()
    best = None
    for term in terms:
        if term in title and (best is None or len(term) > len(best)):
            best = term
    return best


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
        family = _SPEC_UNIT.get(m.group(2).lower())
        if family is not None:
            specs[family] = max(specs.get(family, 0.0), value)
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
        total = kg * bag_n * (bottle or 1.0)
        return ("kg", int(final_cents / total))
    if liter and liter > 0:
        total = liter * (bottle or 1.0)
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
    priced = spec_unit_price_cents(title, final_cents)
    if priced is None:
        return False
    unit_label, unit_price = priced
    standard = (BRAND_STANDARD_PRICES.get(topic) or {}).get(brand)
    if not isinstance(standard, int):
        return False
    return unit_price > int(standard * BRAND_OVERPRICE_K)


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

        clean = [(lp, r, d) for lp, r, d in pairs if r is not None and r <= CUT]
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
    rows = db.execute("SELECT topic, updated_at FROM discount_calibration").fetchall()
    fresh = all(
        (now - datetime.fromisoformat(row["updated_at"])).total_seconds() < max_age_seconds
        for row in rows
    ) if rows else False
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


def judge_drop(topic, reserve_cents, promotion_cents):
    """Data-driven (plausible, reason) for a reference-price reduction.

    ``topic`` may be ``None`` for sources outside the managed living topics;
    those still get the global caps or the conservative seeds.
    """
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
    if high_log is not None and promotion_cents > 0 and math.log10(promotion_cents) >= high_log:
        global_ratio = (payload or {}).get("global_ratio_cap")
        if global_ratio is not None and ratio > global_ratio:
            return False, f"降幅超过该主题全局可信上界（{global_ratio:.0%}），划线价作为优惠证据不可信"
        return True, ""

    ratio_cap, drop_cap = _lookup_caps(topic, promotion_cents)

    # Seeds apply the same semantics as the old absolute guard.
    if ratio_cap is None:
        if promotion_cents < SEED_LOW_PROMO_CENTS and drop > SEED_DROP_CAP_CENTS:
            return False, "低价值商品优惠额超过50元，划线价作为优惠证据不可信"
        if ratio >= SEED_RATIO_CAP:
            return False, "降幅达到参考价85%或以上，参考价作为划线价不可信"
        if reserve_cents > promotion_cents * 20:
            return False, "参考价超过推广价20倍，参考价作为划线价不可信"
        return True, ""

    if ratio_cap is not None and ratio > ratio_cap:
        return False, f"降幅超过该价位校准可信上界（{ratio_cap:.0%}），划线价作为优惠证据不可信"
    if drop_cap is not None and drop > drop_cap:
        return False, f"优惠额超过该价位校准可信上界（¥{drop_cap / 100:.0f}），划线价作为优惠证据不可信"
    return True, ""
