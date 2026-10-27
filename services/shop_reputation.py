"""店铺信誉：识别"瞎搞小店"（用户：很多店铺都是瞎搞的，数据太离谱就不展示）。

淘宝联盟 API 不返回店铺粉丝数（只有 shop_title/user_type），因此用数据驱动的
"店铺离谱率"作为信誉代理：一个店铺在库内条目中被判"优惠证据不可信/荒谬高价"
的比例高（且条目数足够），说明该店整体在发布虚高划线/引流数据 —— 这类店
内的"临界离谱"条目（刚过正常判据线）按用户要求收紧过滤。

阈值全部可配置（环境变量覆盖，默认值即当前线上口径）：
  SHOP_BAD_RATE  离谱率阈值（默认 0.5：过半条目离谱即视为瞎搞店）
  SHOP_MIN_ITEMS 统计最小条目数（默认 3：太少无统计意义，避免误伤小店）
  SHOP_BAD_CAP_K 坏店校准界收紧系数（默认 0.7：降幅/优惠额上限 × 0.7）
"""
import json
import os
import threading
import time

SHOP_BAD_RATE = float(os.environ.get("SHOP_BAD_RATE", "0.5"))
SHOP_MIN_ITEMS = int(os.environ.get("SHOP_MIN_ITEMS", "3"))
SHOP_BAD_CAP_K = float(os.environ.get("SHOP_BAD_CAP_K", "0.7"))
STATS_REFRESH_SECONDS = 3600

_stats = None
_stats_lock = threading.Lock()
_last_refresh = 0.0


def _is_bad_evidence(topic, title, promo, reserve):
    """Whether one listing is "离谱"（优惠证据不可信 / 荒谬高价 / 品牌贵 / 低价小件）。"""
    from services import discount_calibration as dc
    bad = False
    if isinstance(promo, int) and not isinstance(promo, bool) and promo > 0:
        if isinstance(reserve, int) and not isinstance(reserve, bool) and reserve > promo:
            ok, _ = dc.judge_drop(topic, reserve, promo)
            if not ok:
                bad = True
        if dc.is_high_tier_unverifiable(topic, title, promo):
            bad = True
        if dc.is_brand_market_overpriced(topic, title, promo):
            bad = True
        if dc.is_low_value_small_item(topic, promo, title):
            bad = True
    return bad


def load_shop_stats(connection):
    """Compute per-shop (total_items, bad_items) for taobao_alliance listings."""
    c = connection.cursor()
    c.execute("""SELECT s.name AS source_name, o.title, e.metadata_json
        FROM opportunities o JOIN events e ON e.id = o.event_id
        JOIN sources s ON s.id = o.source_id
        WHERE s.method = 'taobao_alliance'""")
    stats = {}
    for source_name, title, meta in c:
        try:
            offer = json.loads(meta or '{}').get('taobao_alliance_offer') or {}
        except (ValueError, TypeError):
            continue
        shop = offer.get('shop_title') or ''
        if not shop:
            continue
        topic = _topic_for(source_name)
        bad = _is_bad_evidence(topic, title or '',
                               offer.get('promotion_price_cents'),
                               offer.get('reserve_price_cents'))
        st = stats.setdefault(shop, [0, 0])
        st[0] += 1
        if bad:
            st[1] += 1
    return stats


def _topic_for(source_name):
    from services import discount_calibration as dc
    return dc.topic_key(source_name)


def set_shop_stats(stats):
    """Test/refresh hook: override the cached per-shop stats."""
    global _stats, _last_refresh
    with _stats_lock:
        _stats = stats
        _last_refresh = time.time()


def get_shop_stats(connection=None):
    global _stats
    if _stats is None and connection is not None:
        _stats = load_shop_stats(connection)
    return _stats


def refresh_stats_if_stale(connection, max_age_seconds=STATS_REFRESH_SECONDS, now=None):
    """Reload the per-shop stats when older than max_age (web request path)."""
    global _stats, _last_refresh
    now = now if now is not None else time.time()
    if _stats is not None and now - _last_refresh < max_age_seconds:
        return _stats
    with _stats_lock:
        if _stats is not None and now - _last_refresh < max_age_seconds:
            return _stats
        try:
            _stats = load_shop_stats(connection)
            _last_refresh = now
        except Exception:
            pass  # keep the previous stats on failure; never crash the request
    return _stats


def is_bad_shop(shop_title, connection=None):
    """Whether a shop is a "瞎搞小店"：离谱率 ≥ SHOP_BAD_RATE 且条目 ≥ SHOP_MIN_ITEMS。"""
    if not shop_title:
        return False
    stats = get_shop_stats(connection)
    if not stats:
        return False
    total, bad = stats.get(shop_title, (0, 0))
    if total < SHOP_MIN_ITEMS:
        return False
    return bad / total >= SHOP_BAD_RATE


def tightened_caps(ratio_cap, drop_cap, shop_bad):
    """Apply the bad-shop tightening to calibrated caps (None-safe)."""
    if not shop_bad:
        return ratio_cap, drop_cap
    k = SHOP_BAD_CAP_K
    ratio_cap = ratio_cap * k if isinstance(ratio_cap, (int, float)) else ratio_cap
    drop_cap = int(drop_cap * k) if isinstance(drop_cap, int) else drop_cap
    return ratio_cap, drop_cap
