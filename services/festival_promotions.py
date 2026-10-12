"""Holiday promotion configuration and conservative participation ranking.

Festival pages reuse the official activity and product-material pipeline.  This
module adds no scraper and never invents a campaign price: it only presents
the currently returned official material fields in a participation-oriented
order.
"""
import os
from datetime import datetime

from db import connect
from services.money import cents
from services.product_search import official_promotion_discount
from services.taobao_alliance import configuration_error, validated_offer_for_row
from services.taobao_campaigns import (campaign_window, create_campaign,
                                       material_source_url, refresh_campaign)


FESTIVAL_PRESETS = {
    "double_11": {"label": "双11", "description": "11 月官方大促会场与活动物料"},
    "mid_year_618": {"label": "618", "description": "年中官方大促会场与活动物料"},
    "double_12": {"label": "双12", "description": "12 月官方大促会场与活动物料"},
    "new_year": {"label": "年货节", "description": "春节前官方年货活动会场与物料"},
    "custom": {"label": "自定义节日", "description": "以当期淘宝联盟官方公告为准"},
}


def festival_options():
    return [(key, value["label"]) for key, value in FESTIVAL_PRESETS.items()]


def _text(value, label, minimum=2, maximum=64):
    value = " ".join(str(value or "").split())
    if not minimum <= len(value) <= maximum:
        raise ValueError(f"{label}须为 {minimum} 到 {maximum} 个字符")
    return value


def _year(value):
    try:
        value = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("节日年份必须是整数") from exc
    if not 2020 <= value <= 2100:
        raise ValueError("节日年份须在 2020 到 2100 之间")
    return value


def _minimum_discount(value):
    try:
        amount = cents(value, required=True)
    except ValueError as exc:
        raise ValueError("最低优惠额必须是非负金额") from exc
    if not 0 <= amount <= 100000000:
        raise ValueError("最低优惠额超出允许范围")
    return amount


def create_festival(db, *, festival_code, festival_year, festival_name, campaign_name,
                    activity_material_id, material_id="", interval_minutes=60,
                    minimum_discount="5", env=None):
    """Create one holiday entry and its underlying auditable official campaign."""
    festival_code = str(festival_code or "").strip()
    if festival_code not in FESTIVAL_PRESETS:
        raise ValueError("节日类型不受支持")
    festival_year = _year(festival_year)
    preset = FESTIVAL_PRESETS[festival_code]
    festival_name = _text(festival_name, "节日名称") if festival_code == "custom" else preset["label"]
    campaign_name = _text(campaign_name, "会场名称")
    minimum_discount_cents = _minimum_discount(minimum_discount)
    campaign_id = create_campaign(
        db, name=f"{festival_name}{festival_year}·{campaign_name}",
        activity_material_id=activity_material_id, material_id=material_id,
        interval_minutes=interval_minutes, env=env,
    )
    return db.execute("""INSERT INTO festival_promotions
        (festival_code,festival_name,festival_year,campaign_id,minimum_discount_cents)
        VALUES(?,?,?,?,?)""", (festival_code, festival_name, festival_year,
                                  campaign_id, minimum_discount_cents)).lastrowid


def create_festival_material_feed(db, *, festival_code, festival_year,
                                  material, minimum_discount="5",
                                  interval_minutes=60, env=None):
    """Attach a freshly discovered official product material to a holiday.

    This is intentionally independent of a campaign hall: a material ID is
    official evidence for a product feed, never an activity hall ID.
    """
    festival_code = str(festival_code or "").strip()
    if festival_code not in FESTIVAL_PRESETS:
        raise ValueError("节日类型不受支持")
    festival_year = _year(festival_year)
    minimum_discount_cents = _minimum_discount(minimum_discount)
    try:
        interval_minutes = int(interval_minutes)
    except (TypeError, ValueError) as exc:
        raise ValueError("同步间隔必须为整数分钟") from exc
    if not 15 <= interval_minutes <= 1440:
        raise ValueError("同步间隔须在 15 到 1440 分钟之间")
    if not isinstance(material, dict):
        raise ValueError("官方物料信息无效")
    material_id = str(material.get("material_id") or "").strip()
    if not material_id.isdigit() or not 1 <= len(material_id) <= 24:
        raise ValueError("官方物料 ID 无效")
    material_name = _text(material.get("material_name"), "官方物料名称", maximum=160)
    preset = FESTIVAL_PRESETS[festival_code]
    festival_name = _text(material.get("festival_name"), "节日名称") if festival_code == "custom" else preset["label"]
    existing = db.execute("""SELECT id FROM festival_material_feeds
        WHERE festival_code=? AND festival_year=? AND material_id=?""",
        (festival_code, festival_year, material_id)).fetchone()
    if existing:
        raise ValueError(f"该官方物料已加入节日监控 #{existing['id']}")
    url = material_source_url(material_id, query=preset["label"])
    source = db.execute("SELECT id FROM sources WHERE platform=? AND url=? AND method=?",
                         ("淘宝联盟", url, "taobao_alliance")).fetchone()
    source_id = source["id"] if source else db.execute("""INSERT INTO sources
        (platform,name,category,url,method,parser,status,enabled,interval_minutes)
        VALUES(?,?,?,?,?,?,?,?,?)""", (
            "淘宝联盟", f"淘宝联盟官方物料·{material_name[:80]}", "零售优惠", url,
            "taobao_alliance", "taobao_alliance",
            "not_configured" if configuration_error(env or os.environ) else "pending",
            1, interval_minutes,
        )).lastrowid
    return db.execute("""INSERT INTO festival_material_feeds
        (festival_code,festival_name,festival_year,material_id,material_name,
         material_start_time,material_end_time,source_id,minimum_discount_cents)
        VALUES(?,?,?,?,?,?,?,?,?)""", (
            festival_code, festival_name, festival_year, material_id, material_name,
            str(material.get("start_time") or "")[:40],
            str(material.get("end_time") or "")[:40], source_id,
            minimum_discount_cents,
        )).lastrowid


def participation_for_row(row, now=None):
    """Return a user action only when the corresponding official condition exists.

    A high coupon face value that needs an unknown basket is deliberately placed
    in ``bundle`` rather than competing with an individually usable promotion.
    """
    evidence = validated_offer_for_row(row, now=now)
    if not evidence:
        return {"bucket": "unconfirmed", "label": "官方字段不足", "priority": 0,
                "action": "保留观察，等待下一轮官方物料返回完整条件。"}
    discount = official_promotion_discount(row)
    if evidence.get("price_drop_candidate") and discount:
        return {"bucket": "direct", "label": "可直接参与", "priority": 3,
                "action": "从官方推广入口进入商品，按当前联盟推广价下单。",
                "caution": "价格、库存和账号资格以淘宝页面或结算页为准。"}
    if evidence.get("coupon_candidate") and discount:
        return {"bucket": "direct", "label": "领取单品券后参与", "priority": 2,
                "action": "从官方推广入口领取或确认单品券，再按已满足的单件门槛下单。",
                "caution": "券资格和结算扣减尚未按当前账号核验。"}
    if evidence.get("promotion_path_complete") and discount:
        return {"bucket": "direct", "label": "官方路径可复算", "priority": 1,
                "action": "从官方会场或推广入口按返回的完整促销路径参与。",
                "caution": "页面条件发生变化时，以淘宝结算页为准。"}
    if evidence.get("coupon_state") == "threshold_not_met":
        threshold = evidence.get("coupon_threshold_cents")
        price = evidence.get("promotion_price_cents")
        gap = threshold - price if isinstance(threshold, int) and isinstance(price, int) else None
        return {"bucket": "bundle", "label": "需凑单后再核验", "priority": 0,
                "gap_cents": gap if isinstance(gap, int) and gap > 0 else None,
                "action": "当前单件不满足券门槛；只有购物车其他商品也能满足同一官方规则时再计算。",
                "caution": "不把券面额当作本商品已获得的优惠。"}
    return {"bucket": "unconfirmed", "label": "参与条件不足", "priority": 0,
            "action": "当前官方物料无法完整说明单品参与条件。"}


def _festival_rows(db, source_id):
    return [dict(row) for row in db.execute("""SELECT o.id,o.title,o.url,o.source_id,
        s.method AS source_method,s.name AS source_name,s.status AS source_status,
        s.last_success,e.metadata_json,e.last_seen_at,e.published_at
        FROM opportunities o
        JOIN sources s ON s.id=o.source_id
        JOIN events e ON e.id=o.event_id
        WHERE o.source_id=? AND o.status!='ignored' AND s.enabled=1
          AND s.method='taobao_alliance'
          AND s.last_success>=datetime('now','-' || MIN(2*s.interval_minutes+15,120) || ' minutes')
          AND e.last_seen_at>=datetime('now','-' || MIN(2*s.interval_minutes+15,120) || ' minutes')
        ORDER BY o.id DESC LIMIT 500""", (source_id,))]


def festival_products(db, festival, now=None):
    """Prepare current holiday products in separate direct/bundle buckets."""
    source_id = festival.get("source_id")
    if not source_id:
        return {"direct": [], "bundle": [], "unconfirmed_count": 0}
    direct, bundle, unconfirmed = [], [], 0
    for row in _festival_rows(db, source_id):
        discount = official_promotion_discount(row)
        participation = participation_for_row(row, now=now)
        if participation["bucket"] == "direct":
            if not discount or discount["amount_cents"] < festival["minimum_discount_cents"]:
                continue
            row["discount"] = discount
            row["participation"] = participation
            direct.append(row)
        elif participation["bucket"] == "bundle":
            row["participation"] = participation
            bundle.append(row)
        else:
            unconfirmed += 1
    direct.sort(key=lambda row: (
        row["discount"]["amount_cents"], row["discount"].get("rate_basis_points") or -1,
        row["participation"]["priority"], row.get("published_at") or row.get("last_seen_at") or "", row["id"]),
        reverse=True)
    bundle.sort(key=lambda row: (-(row["participation"].get("gap_cents") or 10**12), row["id"]), reverse=True)
    return {"direct": direct, "bundle": bundle, "unconfirmed_count": unconfirmed}


def list_festivals(db, now=None):
    rows = [dict(row) for row in db.execute("""SELECT f.*,c.name AS campaign_name,
        c.activity_material_id,c.material_id,c.source_id,c.status,c.page_name,c.page_start_time,
        c.page_end_time,c.promotion_url,c.short_promotion_url,c.last_error,
        s.status AS source_status,s.last_success AS source_last_success
        FROM festival_promotions f
        JOIN taobao_campaigns c ON c.id=f.campaign_id
        LEFT JOIN sources s ON s.id=c.source_id
        ORDER BY f.festival_year DESC,f.id DESC""")]
    feeds = [dict(row) for row in db.execute("""SELECT f.id,f.festival_code,f.festival_name,
        f.festival_year,f.material_id,f.material_name,f.material_start_time,
        f.material_end_time,f.source_id,f.minimum_discount_cents,
        s.status,s.last_success AS source_last_success,s.last_error
        FROM festival_material_feeds f JOIN sources s ON s.id=f.source_id
        ORDER BY f.festival_year DESC,f.id DESC""")]
    for feed in feeds:
        feed.update({"feed_type": "official_material", "campaign_name": feed["material_name"],
                     "activity_material_id": "", "promotion_url": "",
                     "short_promotion_url": "", "page_name": "",
                     "page_start_time": feed["material_start_time"],
                     "page_end_time": feed["material_end_time"],
                     "source_status": feed["status"]})
        rows.append(feed)
    for row in rows:
        row.setdefault("feed_type", "campaign")
        row["preset"] = FESTIVAL_PRESETS[row["festival_code"]]
        row["window"] = campaign_window(row, now=now)
        row["products"] = festival_products(db, row, now=now)
    return rows


def refresh_festival(festival_id, *, env=None, now=None):
    with connect() as db:
        row = db.execute("SELECT campaign_id FROM festival_promotions WHERE id=?", (festival_id,)).fetchone()
    if not row:
        raise ValueError("节日大促不存在")
    return refresh_campaign(row["campaign_id"], env=env, now=now)
