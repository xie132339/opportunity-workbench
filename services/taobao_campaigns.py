"""Official Taobao campaign halls and their optional product-feed sources.

A campaign hall is evidence of an official activity entry.  It is intentionally
stored separately from product material sources: the hall never becomes a
product quote, and a product material ID is never inferred from an activity ID.
"""
import json
import os
import re
from datetime import datetime, timezone
from urllib.parse import urlencode

from db import connect
from services.taobao_alliance import (API_ENDPOINT, TaobaoAllianceAPIError,
                                      TaobaoAllianceConfigurationError, configuration_error,
                                      fetch_activity_info, search_config)

SOURCE_CATEGORY = "零售优惠"
SOURCE_PREFIX = "淘宝官方活动·"


def _digits(value, label, *, required=False):
    value = str(value or "").strip()
    if required and not value:
        raise ValueError(f"{label}不能为空")
    if value and not re.fullmatch(r"[0-9]{1,24}", value):
        raise ValueError(f"{label}必须为 1 到 24 位数字")
    return value


def validate_campaign(name, activity_material_id, material_id, interval_minutes):
    name = " ".join(str(name or "").split())
    if not 2 <= len(name) <= 64:
        raise ValueError("活动名称须为 2 到 64 个字符")
    activity_material_id = _digits(activity_material_id, "官方活动 ID", required=True)
    material_id = _digits(material_id, "商品物料 ID")
    try:
        interval_minutes = int(interval_minutes)
    except (TypeError, ValueError) as exc:
        raise ValueError("检查间隔必须是整数分钟") from exc
    if not 15 <= interval_minutes <= 1440:
        raise ValueError("检查间隔须在 15 到 1440 分钟之间")
    return name, activity_material_id, material_id, interval_minutes


def material_source_url(material_id, query="双11"):
    material_id = _digits(material_id, "商品物料 ID", required=True)
    query = " ".join(str(query or "").split())
    if not 1 <= len(query) <= 100:
        raise ValueError("商品物料兼容查询词须为 1 到 100 个字符")
    url = API_ENDPOINT + "?" + urlencode((
        ("material_id", material_id), ("q", query), ("page_no", "1"),
        ("page_size", "100"), ("max_pages", "all"),
    ))
    search_config(url)
    return url


def create_campaign(db, *, name, activity_material_id, material_id="", interval_minutes=60, env=None):
    """Persist one official campaign and optionally its independently scheduled feed."""
    name, activity_material_id, material_id, interval_minutes = validate_campaign(
        name, activity_material_id, material_id, interval_minutes)
    existing = db.execute("SELECT id FROM taobao_campaigns WHERE activity_material_id=?", (activity_material_id,)).fetchone()
    if existing:
        raise ValueError(f"官方活动 ID 已由活动 #{existing['id']} 维护")
    source_id = None
    if material_id:
        url = material_source_url(material_id)
        source = db.execute("SELECT id FROM sources WHERE platform=? AND url=? AND method=?",
                            ("淘宝联盟", url, "taobao_alliance")).fetchone()
        if source:
            source_id = source['id']
        else:
            status = "not_configured" if configuration_error(env or os.environ) else "pending"
            source_id = db.execute("""INSERT INTO sources
                (platform,name,category,url,method,parser,status,enabled,interval_minutes)
                VALUES(?,?,?,?,?,?,?,?,?)""", (
                    "淘宝联盟", f"{SOURCE_PREFIX}{name}物料", SOURCE_CATEGORY, url,
                    "taobao_alliance", "taobao_alliance", status, 1, interval_minutes,
                )).lastrowid
    return db.execute("""INSERT INTO taobao_campaigns
        (name,activity_material_id,material_id,source_id,interval_minutes)
        VALUES(?,?,?,?,?)""", (name, activity_material_id, material_id, source_id, interval_minutes)).lastrowid


def _parse_time(value):
    value = str(value or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt), fmt == "%Y-%m-%d"
        except ValueError:
            pass
    return None, False


def campaign_window(campaign, now=None):
    """Give a bounded display state; date-only ranges are never guessed active."""
    now = now or datetime.now()
    start, start_date_only = _parse_time(campaign.get("page_start_time"))
    end, end_date_only = _parse_time(campaign.get("page_end_time"))
    if not start or not end or end < start:
        return {"state": "time_unconfirmed", "label": "活动时间待官方会场确认"}
    if start_date_only or end_date_only:
        today = now.date()
        if today < start.date():
            return {"state": "upcoming", "label": "活动日期尚未开始"}
        if today > end.date():
            return {"state": "expired", "label": "活动日期已结束"}
        return {"state": "date_range", "label": "日期范围内，具体时段以会场页为准"}
    if now < start:
        return {"state": "upcoming", "label": "活动尚未开始"}
    if now > end:
        return {"state": "expired", "label": "活动已结束"}
    return {"state": "active", "label": "官方活动时间内"}


def campaign_copy(campaign):
    """Return manual-use copy only; this module never publishes externally."""
    url = campaign.get("short_promotion_url") or campaign.get("promotion_url")
    if not url:
        return ""
    timing = " 至 ".join(filter(None, (campaign.get("page_start_time"), campaign.get("page_end_time")))) or "以官方会场页面为准"
    return "\n".join((
        f"官方活动：{campaign.get('page_name') or campaign.get('name')}",
        f"活动时间：{timing}",
        f"官方入口：{url}",
        "链接可能产生推广佣金；价格、库存、券资格及最终支付金额以淘宝页面或结算页为准。",
    ))


def _safe_payload(value):
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return "{}"


def refresh_campaign(campaign_id, *, http_get=None, env=None, now=None):
    """Fetch outside a database transaction, then record only auditable activity data."""
    with connect() as db:
        row = db.execute("SELECT * FROM taobao_campaigns WHERE id=?", (campaign_id,)).fetchone()
    if not row:
        raise ValueError("官方活动不存在")
    campaign = dict(row)
    checked_at = (now or datetime.now(timezone.utc)).replace(tzinfo=None).isoformat(sep=" ", timespec="seconds")
    report = {}
    try:
        info = fetch_activity_info(campaign["activity_material_id"], http_get=http_get, env=env, now=now, report=report)
    except (TaobaoAllianceAPIError, TaobaoAllianceConfigurationError, ValueError) as exc:
        with connect() as db:
            db.execute("""UPDATE taobao_campaigns SET status='failed',last_checked=?,last_error=?
                WHERE id=?""", (checked_at, f"{getattr(exc, 'code', type(exc).__name__)}: {str(exc)[:240]}", campaign_id))
        return {"id": campaign_id, "status": "failed", "error": str(exc), "report": report}
    with connect() as db:
        db.execute("""UPDATE taobao_campaigns SET status='verified',page_name=?,page_start_time=?,page_end_time=?,
            promotion_url=?,short_promotion_url=?,response_json=?,last_checked=?,last_success=?,last_error=''
            WHERE id=?""", (
                info["page_name"], info["page_start_time"], info["page_end_time"],
                info["promotion_url"], info["short_promotion_url"], _safe_payload(info["raw_payload"]),
                checked_at, checked_at, campaign_id,
            ))
    return {"id": campaign_id, "status": "verified", "campaign": info, "report": report}


def refresh_due_campaigns(*, env=None, now=None):
    with connect() as db:
        ids = [row["id"] for row in db.execute("""SELECT id FROM taobao_campaigns
            WHERE enabled=1 AND (last_checked IS NULL OR last_checked<=datetime('now','-' || interval_minutes || ' minutes'))
            ORDER BY id""")]
    return [refresh_campaign(campaign_id, env=env, now=now) for campaign_id in ids]


def list_campaigns(db, now=None):
    rows = [dict(row) for row in db.execute("""SELECT c.*,s.name AS source_name,s.status AS source_status,
        s.enabled AS source_enabled,s.last_success AS source_last_success
        FROM taobao_campaigns c LEFT JOIN sources s ON s.id=c.source_id ORDER BY c.id DESC""")]
    for row in rows:
        row["window"] = campaign_window(row, now=now)
        row["promotion_copy"] = campaign_copy(row)
    return rows
