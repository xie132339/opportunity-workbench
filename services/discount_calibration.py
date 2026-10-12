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
                      "体验装", "一分购", "元购")
# A promotion price shared verbatim by many distinct listings inside one topic's
# low band is a first-order/unified subsidy price, not a market price.
GATED_MIN_COUNT = 5


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

        result[topic] = {
            "price_ceiling_cents": ceiling,
            "high_log": round(high_log, 4),
            "bins": bins,
            "global_ratio_cap": global_ratio,
            "global_drop_cap": global_drop,
            "gated_prices": _gated_promo_prices(promo_values[topic]),
            "low_price_floor": int(_quantile(sorted(promo_values[topic]), 0.15)) if promo_values[topic] else None,
            "sample_count": len(pairs),
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


def is_low_value_small_item(topic, cents):
    """Whether a final price is a low-value small-item price (topic P15).

    Cheap single packs / single bottles (a 2-yuan tissue pack, a 5-yuan flour
    bag) are not "real bargains with big savings" for the user even when the
    strikethrough looks deep; they must not surface a discount amount.  The
    floor is estimated from the corpus (P15 of promotion prices), never
    hard-coded, and only the discount identity is dropped — the listing stays
    on the default list with its promotion price shown.
    """
    if not isinstance(cents, int) or isinstance(cents, bool) or cents <= 0:
        return False
    payload = get_calibration().get(topic) or {}
    floor = payload.get("low_price_floor")
    return isinstance(floor, int) and cents < floor


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
