"""Read-only candidates for operator-maintained price bands.

Candidates are deliberately narrower than the public-offer list.  They only
help an operator choose a concrete product and unit to maintain; they never
produce a price band or a bargain decision themselves.
"""
import re

from services.category_policy import match_category
from offer import resource_topic
from services.taobao_alliance import has_current_promotion_signal, validated_offer


_MEASUREMENT = re.compile(
    r"\d+(?:\.\d+)?\s*(?:kg|g|l|ml|千克|克|斤|升|毫升|抽|张|包|卷|瓶|袋|盒|箱|提|片|粒|件)",
    re.IGNORECASE,
)


def _candidate_reason(offer):
    discount = offer.get("promotion_discount_cents")
    if isinstance(discount, int) and discount > 0:
        return f"官方推广价比接口参考价低 ¥{discount / 100:.2f}"
    coupon = offer.get("coupon_applied_cents")
    if isinstance(coupon, int) and coupon > 0:
        return f"官方返回单券条件试算，已扣 ¥{coupon / 100:.2f}"
    return "官方优惠路径仍需人工确认"


def load_maintained_price_candidates(db, categories, *, limit=12):
    """Return fresh, measurable official discounts that can seed operator work.

    This is intentionally read-only.  A candidate needs a current official
    Taobao Alliance observation, a positive price drop (or a safely
    single-coupon estimate), and an explicit measurement in its title.  It
    uses the current title classifier rather than the stored topic because
    older events can retain an obsolete ``other`` topic.  An unclassified
    candidate stays visible and explicitly asks the operator to configure a
    category; the four maintained price values remain blank.
    """
    fetch_limit = max(limit * 8, 80)
    rows = db.execute("""
        SELECT o.id AS opportunity_id, o.title, o.topic, o.url,
               s.category AS source_category,
               s.name AS source_name, e.metadata_json,
               a.checked_at, a.conditions
        FROM opportunities o
        JOIN sources s ON s.id=o.source_id
        JOIN events e ON e.id=o.event_id
        JOIN auto_reviews a ON a.opportunity_id=o.id
        WHERE s.method='taobao_alliance'
          AND s.enabled=1
          AND s.status='healthy'
          AND a.state='observed'
          AND s.last_success>=datetime('now','-' || MIN(2*s.interval_minutes+15,120) || ' minutes')
          AND e.last_seen_at>=datetime('now','-' || MIN(2*s.interval_minutes+15,120) || ' minutes')
        ORDER BY o.id DESC
        LIMIT ?
    """, (fetch_limit,)).fetchall()
    result = []
    for raw in rows:
        row = dict(raw)
        offer = validated_offer(row["metadata_json"])
        if not offer or not has_current_promotion_signal(offer):
            continue
        quote_cents = offer.get("quote_price_cents")
        actual_drop = offer.get("promotion_discount_cents")
        coupon_applied = offer.get("coupon_applied_cents")
        if not isinstance(quote_cents, int) or quote_cents <= 0:
            continue
        if not ((isinstance(actual_drop, int) and actual_drop > 0)
                or (isinstance(coupon_applied, int) and coupon_applied > 0)):
            continue
        if not _MEASUREMENT.search(row["title"] or ""):
            continue
        topic_key = resource_topic(row["title"], row["source_category"] or "")
        category = match_category(row["title"], topic_key, categories)
        path = offer.get("promotion_path") or {}
        result.append({
            "opportunity_id": row["opportunity_id"],
            "title": row["title"],
            "url": row["url"],
            "source_name": row["source_name"],
            "checked_at": row["checked_at"],
            "topic_key": topic_key,
            "category_id": category["id"] if category else None,
            "category_name": category["name"] if category else "",
            "price_cents": quote_cents,
            "reference_cents": offer.get("reserve_price_cents"),
            "reason": _candidate_reason(offer),
            "condition": path.get("purchase_requirement") or row.get("conditions") or "来源未返回额外条件字段",
        })
        if len(result) == limit:
            break
    return result


def maintenance_draft(candidates, raw_candidate_id):
    """Return an unsaved baseline draft for one current official candidate.

    Candidate data can identify an auditable starting item, but cannot safely
    infer necessary identity terms, a comparison unit, or price thresholds.
    This helper therefore copies only the title, a uniquely matched category,
    and provenance into a form draft.
    """
    try:
        candidate_id = int(raw_candidate_id)
    except (TypeError, ValueError):
        return None
    candidate = next((row for row in candidates
                      if row["opportunity_id"] == candidate_id), None)
    if candidate is None:
        return None
    return {
        "candidate_id": candidate_id,
        "name": (candidate["title"] or "")[:120],
        "category_id": candidate["category_id"],
        "category_name": candidate["category_name"],
        "notes": (
            f"维护起点：淘宝联盟官方候选 #{candidate_id}。"
            "已回链官方推广价与条件；匹配词、规格单位和四档价格带须由运营核实填写。"
        ),
    }
