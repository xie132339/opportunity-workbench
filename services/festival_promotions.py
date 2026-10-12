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


def savings_plan_for_row(row, now=None):
    """Explain the strongest combination supported by this item's official fields.

    This is intentionally item-scoped: the material API cannot see a shopper's
    personal coupon wallet or the rest of their cart.
    """
    evidence = validated_offer_for_row(row, now=now)
    if not evidence:
        return {"label": "暂时算不了", "steps": [],
                "note": "淘宝没有返回足够的优惠条件。"}

    promotion_price = evidence.get("promotion_price_cents")
    path = evidence.get("promotion_path") or {}
    candidates = []
    if evidence.get("promotion_path_complete"):
        estimate = path.get("estimate_cents")
        saving = promotion_price - estimate if isinstance(promotion_price, int) and isinstance(estimate, int) else None
        candidates.append({
            "label": "按淘宝接口给出的完整组合走",
            "price_cents": estimate,
            "price_label": "接口路径预估",
            "saving_cents": saving,
            "steps": [" + ".join(filter(None, (part.get("title"), part.get("description"))))
                      for part in path.get("components", [])],
            "note": "组合金额与接口返回的预估价一致；结算前仍以淘宝购物车为准。",
        })
    if evidence.get("quote_basis") == "single_coupon_conditional_estimate":
        threshold = evidence.get("coupon_threshold_cents")
        coupon = evidence.get("coupon_amount_cents")
        estimate = evidence.get("quote_price_cents")
        candidates.append({
            "label": "单品券试算（不与其他优惠重复相减）",
            "price_cents": estimate,
            "price_label": "单券条件价",
            "saving_cents": coupon,
            "steps": [f"推广价 ¥{promotion_price / 100:.2f}",
                      f"满 ¥{threshold / 100:.2f} 用券减 ¥{coupon / 100:.2f}"],
            "note": "这是单券条件试算；先领券，再在结算页确认账号可用。",
        })

    # Compare only complete paths for the same item quantity. Never stack
    # independent discounts; a multi-item estimate stays a separate option.
    candidates = [plan for plan in candidates
                  if isinstance(plan.get("price_cents"), int) and plan["price_cents"] > 0]
    if candidates:
        plan = min(candidates, key=lambda option: option["price_cents"])
        if len(candidates) > 1:
            plan["note"] = "在接口可复算的单券与完整路径中选择预估价较低的一项；不把两者相加。结算前以淘宝为准。"
    elif evidence.get("price_drop_candidate"):
        plan = {
            "label": "直接买当前推广价",
            "price_cents": promotion_price,
            "price_label": "当前推广价",
            "saving_cents": evidence.get("promotion_discount_cents"),
            "steps": ["当前推广价已低于接口参考价"],
            "note": "另有优惠券时，只有淘宝显示可叠加才继续使用；这里不重复扣券。",
        }
    elif evidence.get("coupon_state") == "threshold_not_met":
        threshold = evidence.get("coupon_threshold_cents")
        coupon = evidence.get("coupon_amount_cents")
        gap = threshold - promotion_price if isinstance(threshold, int) and isinstance(promotion_price, int) else None
        plan = {
            "label": "先别为了券多买",
            "price_cents": None,
            "saving_cents": None,
            "steps": [f"单件 ¥{promotion_price / 100:.2f}，券为满 ¥{threshold / 100:.2f} 减 ¥{coupon / 100:.2f}"],
            "gap_cents": gap if isinstance(gap, int) and gap > 0 else None,
            "note": "只有购物车里有本来就要买的商品时再凑门槛；否则多花的钱可能超过券额。",
        }
    else:
        plan = {"label": "暂时算不了", "price_cents": None,
                "saving_cents": None, "steps": [],
                "note": "官方返回的路径或券条件不完整，不能判断组合后的价格。"}

    rounding_price = evidence.get("rounding_up_price_cents")
    rounding_desc = evidence.get("rounding_up_price_description") or "凑单条件未返回"
    if isinstance(rounding_price, int) and rounding_price > 0:
        plan["rounding_up"] = {"price_cents": rounding_price, "description": rounding_desc}
    return plan


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
            row["savings_plan"] = savings_plan_for_row(row, now=now)
            direct.append(row)
        elif participation["bucket"] == "bundle":
            row["participation"] = participation
            row["savings_plan"] = savings_plan_for_row(row, now=now)
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


# 本轮用户关注的商品范围；价格、优惠、店铺入口均来自当前采集记录。
SHOP_SPOTLIGHTS = (
    ('大米', '天猫超市', ('金龙鱼', '金玉稻香', '五常', '5kg'), ('油',)),
    ('抽纸', '清风个人洗护旗舰店', ('清风', '悬挂', '250抽'), ()),
    ('面粉', '雪健旗舰店', ('雪健', '全麦', '2.5kg'), ()),
)


def recommended_shop_offers(db, now=None):
    """Bounded, read-only shortlist. No live API call on page navigation."""
    from datetime import timezone, timedelta
    result = []
    for category, shop, terms, excluded in SHOP_SPOTLIGHTS:
        conditions = ' AND '.join(['o.title LIKE ?'] * len(terms) + ['o.title NOT LIKE ?'] * len(excluded))
        # Reuse each living-topic source and its indexed current-event range.
        from urllib.parse import parse_qs, urlparse
        source_ids = [r['id'] for r in db.execute(
            "SELECT id,url FROM sources WHERE enabled=1 AND method='taobao_alliance'")
            if category in parse_qs(urlparse(r['url']).query).get('q', [''])[0]]
        rows = []
        for source_id in source_ids:
            rows.extend(db.execute("""SELECT o.id,o.title,o.url,e.metadata_json,e.last_seen_at
                FROM events e INDEXED BY idx_events_source_last_seen
                JOIN opportunities o ON o.event_id=e.id JOIN sources s ON s.id=e.source_id
                WHERE e.source_id=? AND e.last_seen_at>=datetime('now','-120 minutes')
                  AND o.status!='ignored' AND """ + conditions + """
                  AND s.last_success>=datetime('now','-' || MIN(2*s.interval_minutes+15,120) || ' minutes')
                  AND e.last_seen_at>=datetime(s.last_success,'-2 minutes')
                  AND json_valid(e.metadata_json)
                  AND json_extract(e.metadata_json,'$.taobao_alliance_offer.shop_title')=?
                ORDER BY e.last_seen_at DESC,o.id DESC LIMIT 30""",
                [source_id, *['%' + t + '%' for t in terms],
                 *['%' + t + '%' for t in excluded], shop]).fetchall())
        rows.sort(key=lambda r: (r['last_seen_at'], r['id']), reverse=True)
        candidates, seen = [], set()
        for record in rows:
            row = dict(record)
            offer = validated_offer_for_row(row, now=now)
            if not offer or offer.get('shop_title') != shop:
                continue
            identity = offer['item_id']
            if identity in seen:
                continue
            seen.add(identity)
            # Reference/list-price differences alone do not qualify as a recommendation.
            path = offer.get('promotion_path') or {}
            if not path.get('complete') or path['known_discount_cents'] < 500:
                continue
            parts = path['components']
            if any(term in row['title'] + ' '.join(p['description'] for p in parts)
                   for term in ('88VIP', '新人', '新客', 'U先', '省钱卡')):
                continue
            collected = datetime.fromisoformat(row['last_seen_at'])
            if collected.tzinfo is None:
                collected = collected.replace(tzinfo=timezone.utc)
            candidates.append(dict(row, category=category, shop=shop,
                price_cents=path['estimate_cents'], before_cents=offer['promotion_price_cents'],
                saving_cents=path['known_discount_cents'], steps=parts,
                end_at=min(p['end_at'] for p in parts),
                updated_at=collected.astimezone(timezone(timedelta(hours=8))).strftime('%m-%d %H:%M')))
        if candidates:
            candidates.sort(key=lambda r: (r['price_cents'], -r['saving_cents'], -r['id']))
            result.append(candidates[0])
    return result
