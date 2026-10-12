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
            "sample_count": len(pairs),
            "calibrated_at": _now_iso(),
        }
    return result


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

    drop = reserve_cents - promotion_cents
    ratio = drop / reserve_cents
    payload = get_calibration().get(topic)
    # Premium prices (>= topic P97) are exempt from ratio/drop caps; the price
    # ceiling above is the only guard for them.
    high_log = (payload or {}).get("high_log")
    if high_log is not None and promotion_cents > 0 and math.log10(promotion_cents) >= high_log:
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
