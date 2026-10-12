"""Official Taobao Alliance material API adapter.

This adapter preserves structured public quotes and separate promotion candidates.
It does not claim checkout eligibility, coupon redemption, inventory, or a
market-wide bargain.
"""
import hashlib
import hmac
import json
import os
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from urllib.parse import parse_qsl, parse_qs, urlparse

import requests


API_ENDPOINT = "https://eco.taobao.com/router/rest"
API_METHOD = "taobao.tbk.dg.material.optional.upgrade"
ACTIVITY_INFO_METHOD = "taobao.tbk.activity.info.get"
MATERIAL_IDS_METHOD = "taobao.tbk.optimus.tou.material.ids.get"
CHINA_TZ = timezone(timedelta(hours=8))
ALLOWED_SEARCH_PARAMS = frozenset({"q", "material_id", "cat", "start_price", "end_price", "is_tmall", "page_no", "page_size", "has_coupon", "max_pages"})
TAOBAO_ITEM_HOSTS = frozenset({"item.taobao.com", "m.taobao.com", "h5.m.taobao.com"})
TMALL_ITEM_HOSTS = frozenset({"detail.tmall.com", "chaoshi.detail.tmall.com"})
SELLER_MARKETPLACES = {"0": "taobao", "1": "tmall"}
PROMOTION_SIGNAL_FIELDS = (
    "price_drop_candidate",
    "coupon_candidate",
    "promotion_path_candidate",
)
# A merchant-set reference (strikethrough) price can be inflated arbitrarily.
# A price-drop signal that relies on it is only explainable when the drop stays
# within a plausible range; otherwise the drop is kept for audit but never
# presented as an official discount amount.
REFERENCE_DROP_MAX_RATIO = 0.85  # drop/reserve must be < 85%; 85%+ strikethrough on household items is inflated (merchants exploit 90%/89.5% edges)
REFERENCE_MAX_MARKUP = 20        # reserve / promotion must be <= 20x


def reference_drop_plausibility(reserve_cents, promotion_cents):
    """Return (plausible, reason) for a reference-price reduction.

    ``reserve_price`` is set by the merchant, so an extreme reduction against it
    (e.g. a 199 yuan strikethrough on a 2.9 yuan pack of tissues) is treated as
    an unverified marketing anchor rather than an official discount.
    """
    if not (isinstance(reserve_cents, int) and isinstance(promotion_cents, int)
            and not isinstance(reserve_cents, bool) and not isinstance(promotion_cents, bool)
            and reserve_cents > promotion_cents > 0):
        return False, "无有效参考价差"
    drop = reserve_cents - promotion_cents
    # Absolute guard: a household item costing under 10 yuan cannot credibly
    # "save" over 50 yuan (e.g. 6.26 yuan tissues with a 53-yuan strikethrough).
    if promotion_cents < 1000 and drop > 5000:
        return False, "低价值商品优惠额超过50元，划线价作为优惠证据不可信"
    if drop * 20 >= reserve_cents * 17:  # drop / reserve >= 0.85（含边界；生活品低于1.5折的划线基本为虚高）
        return False, "降幅达到参考价85%或以上，参考价作为划线价不可信"
    if reserve_cents > promotion_cents * REFERENCE_MAX_MARKUP:
        return False, "参考价超过推广价20倍，参考价作为划线价不可信"
    return True, ""


def has_current_promotion_signal(offer):
    """Whether official material carries a current, auditable promotion signal."""
    return bool(isinstance(offer, dict) and any(bool(offer.get(field))
                                                for field in PROMOTION_SIGNAL_FIELDS))


class TaobaoAllianceConfigurationError(RuntimeError):
    """Required server-side affiliate credentials are missing or invalid."""


class TaobaoAllianceAPIError(RuntimeError):
    """TOP returned an invalid response or a remote API error."""

    def __init__(self, message, code="api_error", remote_code=""):
        super().__init__(message)
        self.code = code
        # Keep the TOP sub-code separate from our stable local classification.
        # It is evidence for diagnosing an API/package problem, not a field
        # that callers should have to scrape from a human error message.
        self.remote_code = str(remote_code or "")[:80]


def configuration_error(env=None):
    env = os.environ if env is None else env
    missing = [key for key in (
        "TAOBAO_ALLIANCE_APP_KEY", "TAOBAO_ALLIANCE_APP_SECRET",
        "TAOBAO_ALLIANCE_ADZONE_ID",
    ) if not str(env.get(key) or "").strip()]
    if missing:
        return "缺少服务端配置：" + ", ".join(missing)
    if not str(env["TAOBAO_ALLIANCE_APP_KEY"]).strip().isdigit():
        return "TAOBAO_ALLIANCE_APP_KEY 必须为数字 AppKey"
    if not str(env["TAOBAO_ALLIANCE_ADZONE_ID"]).strip().isdigit():
        return "TAOBAO_ALLIANCE_ADZONE_ID 必须为数字推广位 ID"
    return ""


def search_config(source_url):
    """Validate and return the keyword/filter query stored on a source row."""
    parsed = urlparse(source_url or "")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("淘宝联盟 API 地址端口无效") from exc
    if (parsed.scheme != "https" or parsed.hostname != "eco.taobao.com"
            or port not in (None, 443) or parsed.path != "/router/rest"
            or parsed.username or parsed.password or parsed.fragment):
        raise ValueError("淘宝联盟入口必须是官方 HTTPS /router/rest 地址")
    pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)) or any(key not in ALLOWED_SEARCH_PARAMS for key in keys):
        raise ValueError("淘宝联盟入口包含不支持或重复的搜索参数")
    values = dict(pairs)
    keyword = values.get("q", "").strip()
    material_id = values.get("material_id", "").strip()
    if keyword and len(keyword) > 100:
        raise ValueError("淘宝联盟搜索词 q 长度不超过 100 个字符")
    if material_id and not re.fullmatch(r"[0-9]{1,24}", material_id):
        raise ValueError("淘宝联盟物料 ID 必须为 1 到 24 位数字")
    if not keyword and not material_id:
        raise ValueError("淘宝联盟入口必须提供搜索词 q 或官方商品物料 ID material_id")
    if keyword:
        values["q"] = keyword
    if material_id:
        values["material_id"] = material_id
    for key in ("start_price", "end_price"):
        if key in values:
            try:
                amount = Decimal(values[key])
            except InvalidOperation as exc:
                raise ValueError(f"{key} 必须是正数") from exc
            if not amount.is_finite() or amount <= 0 or amount > Decimal("10000000"):
                raise ValueError(f"{key} 必须是 0 到 10000000 元之间的正数")
    if ("start_price" in values and "end_price" in values
            and Decimal(values["start_price"]) > Decimal(values["end_price"])):
        raise ValueError("start_price 不能高于 end_price")
    for key in ("is_tmall", "has_coupon"):
        if key in values:
            value = values[key].strip().lower()
            if value not in ("0", "1", "true", "false"):
                raise ValueError(f"{key} 只能是 true/false 或 1/0")
            values[key] = "true" if value in ("1", "true") else "false"
    if "max_pages" in values:
        values["max_pages"] = values["max_pages"].strip().lower()
        if values["max_pages"] != "all" and (not values["max_pages"].isdigit()
                                               or not 1 <= int(values["max_pages"]) <= 1000):
            raise ValueError("max_pages 必须是 all 或 1 到 1000 之间的整数")
    else:
        # A source without an explicit cap must discover every page the official
        # API makes addressable, rather than silently retaining page one only.
        values["max_pages"] = "all"
    for key, default, upper in (("page_no", 1, 1000), ("page_size", 100, 100)):
        if key in values:
            if not values[key].isdigit() or not 1 <= int(values[key]) <= upper:
                raise ValueError(f"{key} 必须在 1 到 {upper} 之间")
        else:
            values[key] = str(default)
    return values


def _money_cents(value):
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not amount.is_finite() or amount <= 0 or amount > Decimal("10000000"):
        return None
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _optional_cents(value):
    if value is None or str(value).strip() == "":
        return None
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not amount.is_finite() or amount < 0 or amount > Decimal("10000000"):
        return None
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _datetime(value):
    value = str(value or "").strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=CHINA_TZ)
        except ValueError:
            continue
    return None


def _coupon_window(start_raw, end_raw, now):
    """Date-only API fields have no proven intra-day boundary.

    Only dates strictly inside the reported range establish a current window.
    Boundary days remain explicit conditional leads instead of guessed times.
    """
    start, end = _datetime(start_raw), _datetime(end_raw)
    if start and end:
        if start >= end:
            return "validity_invalid"
        return "not_started" if now < start else "expired" if now >= end else "current"
    try:
        start_date = datetime.strptime(str(start_raw), "%Y-%m-%d").date()
        end_date = datetime.strptime(str(end_raw), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return "validity_unknown"
    if start_date > end_date:
        return "validity_invalid"
    if now.date() < start_date:
        return "not_started"
    if now.date() > end_date:
        return "expired"
    if start_date < now.date() < end_date:
        return "current"
    return "validity_precision_unknown"


def marketplace_from_seller_type(value):
    """Map the documented Alliance seller type to a recognized marketplace.

    Both Taobao and Tmall are official Taobao-ecosystem merchant results.  Any
    other value stays unclassified instead of being admitted based on a title
    or a promotion link alone.
    """
    return SELLER_MARKETPLACES.get(str(value).strip())


def _product_url(item):
    item_id = str(item.get("num_iid") or "").strip()
    raw = str(item.get("item_url") or "").strip()
    if not raw or len(raw) > 2048:
        return None
    parsed = urlparse(raw)
    try:
        port = parsed.port
    except ValueError:
        return None
    new_id = str(item.get("item_id") or "").strip()
    if (re.fullmatch(r"[A-Za-z0-9_-]{1,128}", new_id)
            and parsed.scheme == "https" and parsed.hostname == "s.click.taobao.com"
            and port in (None, 443) and parsed.path not in ("", "/")
            and not (parsed.username or parsed.password or parsed.fragment)):
        return raw
    if not item_id.isdigit():
        return None
    marketplace = marketplace_from_seller_type(item.get("user_type"))
    expected_hosts = TAOBAO_ITEM_HOSTS if marketplace == "taobao" else TMALL_ITEM_HOSTS
    if (parsed.scheme != "https" or parsed.hostname not in expected_hosts
            or port not in (None, 443)
            or parsed.path not in ("/item.htm", "/awp/core/detail.htm")
            or parsed.username or parsed.password or parsed.fragment):
        return None
    query = parse_qs(parsed.query)
    ids = query.get("id", [])
    if len(ids) != 1 or ids[0] != item_id:
        return None
    host = "item.taobao.com" if marketplace == "taobao" else "detail.tmall.com"
    return f"https://{host}/item.htm?id={item_id}"


def _is_presale(item):
    # TOP's zk_final_price can be a pre-sale price. Until deposit/tail timing
    # has a dedicated plan, retain the raw row but do not advertise it as spot.
    return any(item.get(key) not in (None, "", 0, "0", "0.00") for key in (
        "presale_deposit", "presale_start_time", "presale_end_time",
        "presale_tail_start_time", "presale_tail_end_time"))


def _current_promotion_path(item, public_cents, now):
    """Preserve a current official promotion path without inventing an after-price.

    ``final_promotion_price`` is sometimes lower than the sum that its returned
    path explains.  That can include account, channel, or other promotion
    factors omitted from this response.  The returned path is still useful
    official evidence, but only a fully reconciled path may expose the API's
    final estimate as a price.
    """
    estimated = _money_cents(item.get("final_promotion_price"))
    paths = item.get("final_promotion_path")
    if not isinstance(paths, list) or not paths:
        return None
    parsed = []
    for part in paths:
        if not isinstance(part, dict):
            return None
        fee = _money_cents(part.get("promotion_fee"))
        try:
            start = datetime.fromtimestamp(int(part["promotion_start_time"]) / 1000, CHINA_TZ)
            end = datetime.fromtimestamp(int(part["promotion_end_time"]) / 1000, CHINA_TZ)
        except (KeyError, ValueError, TypeError, OverflowError, OSError):
            return None
        if not fee or not start <= now < end:
            return None
        parsed.append({"title": str(part.get("promotion_title") or "")[:80],
                       "description": str(part.get("promotion_desc") or "")[:160],
                       "discount_cents": fee, "start_at": start.isoformat(),
                       "end_at": end.isoformat()})
    known_discount_cents = sum(part["discount_cents"] for part in parsed)
    known_estimate_cents = public_cents - known_discount_cents
    if known_estimate_cents <= 0:
        return None
    reconciled = bool(estimated and estimated == known_estimate_cents)
    return {
        "estimate_cents": estimated if reconciled else None,
        "returned_estimate_cents": estimated,
        "known_estimate_cents": known_estimate_cents,
        "known_discount_cents": known_discount_cents,
        "unexplained_discount_cents": (known_estimate_cents - estimated
                                         if estimated and estimated < known_estimate_cents else 0),
        "complete": reconciled,
        "components": parsed,
        "purchase_requirement": str(item.get("predict_rounding_up_price_desc") or "")[:120],
    }


def parse_item(item, now=None):
    """Turn one TOP result into a quote; promotion eligibility is assessed separately."""
    if not isinstance(item, dict):
        return None
    now = now or datetime.now(CHINA_TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(CHINA_TZ)
    title = re.sub(r"\s+", " ", str(item.get("title") or "")).strip()
    url = _product_url(item)
    stable_id = str(item.get("num_iid") or item.get("item_id") or "").strip()
    reserve_cents = _money_cents(item.get("reserve_price"))
    promotion_cents = _money_cents(item.get("zk_final_price"))
    marketplace = marketplace_from_seller_type(item.get("user_type"))
    if (not title or not url or not promotion_cents
            or not marketplace or _is_presale(item)):
        return None

    coupon_amount_cents = _optional_cents(item.get("coupon_amount"))
    coupon_threshold_cents = _optional_cents(item.get("coupon_start_fee"))
    commission_amount_cents = _optional_cents(item.get("commission_amount"))
    coupon_start = _datetime(item.get("coupon_start_time"))
    coupon_end = _datetime(item.get("coupon_end_time"))
    event_start = _datetime(item.get("event_start_time"))
    event_end = _datetime(item.get("event_end_time"))
    if ((event_start and event_start > now) or (event_end and event_end <= now)):
        return None
    remain_value = item.get("coupon_remain_count")
    remain_raw = str(remain_value).strip() if remain_value is not None and not isinstance(remain_value, bool) else ""
    remain_count = int(remain_raw) if remain_raw.isdigit() else None
    coupon_window = _coupon_window(item.get("coupon_start_time"), item.get("coupon_end_time"), now)
    coupon_dates_current = coupon_window == "current"
    coupon_remaining = remain_count is not None and remain_count > 0
    coupon_eligible_by_threshold = bool(
        coupon_amount_cents and coupon_amount_cents > 0
        and coupon_threshold_cents is not None
        and promotion_cents >= coupon_threshold_cents
    )
    coupon_candidate = bool(coupon_dates_current and coupon_remaining and coupon_eligible_by_threshold
                            and coupon_amount_cents < promotion_cents)
    price_drop_plausible, price_drop_reason = reference_drop_plausibility(reserve_cents, promotion_cents)
    price_drop = bool(price_drop_plausible)
    promotion_path = _current_promotion_path(item, promotion_cents, now)
    rounding_up_price_cents = _optional_cents(item.get("predict_rounding_up_price"))
    rounding_up_description = " ".join(str(item.get("predict_rounding_up_price_desc") or "").split())[:120]

    coupon_state = "not_returned"
    if coupon_amount_cents:
        if remain_count == 0:
            coupon_state = "depleted"
        elif coupon_window != "current":
            coupon_state = coupon_window
        elif remain_count is None:
            coupon_state = "remaining_unknown"
        elif not coupon_remaining:
            coupon_state = "depleted"
        elif not coupon_eligible_by_threshold:
            coupon_state = "threshold_not_met"
        elif coupon_amount_cents >= promotion_cents:
            coupon_state = "deduction_invalid"
        else:
            coupon_state = "active_candidate"

    # Only apply the returned coupon in a one-item illustration when the API
    # gives a live coupon, its threshold is met by the undiscounted promotion
    # amount, and the item has no separate promotion-price reduction. We never
    # stack a coupon onto zk_final_price because the API does not prove that
    # the two mechanisms stack for this user/SKU.
    quote_basis = "alliance_promotion_price"
    quote_cents = promotion_cents
    coupon_applied_cents = None
    if (reserve_cents == promotion_cents and coupon_candidate and coupon_amount_cents
            and promotion_cents >= (coupon_threshold_cents or 0)
            and coupon_amount_cents < promotion_cents):
        quote_cents = promotion_cents - coupon_amount_cents
        coupon_applied_cents = coupon_amount_cents
        quote_basis = "single_coupon_conditional_estimate"
    discount_cents = reserve_cents - promotion_cents if reserve_cents is not None else None
    coupon_info = ""
    if coupon_amount_cents:
        threshold = coupon_threshold_cents
        coupon_info = (
            f"接口返回优惠券满{threshold / 100:.2f}元减{coupon_amount_cents / 100:.2f}元"
            if threshold is not None else f"接口返回优惠券减{coupon_amount_cents / 100:.2f}元，门槛未给出"
        )
        if coupon_start and coupon_end:
            coupon_info += f"，有效期{coupon_start.strftime('%Y-%m-%d %H:%M')}至{coupon_end.strftime('%Y-%m-%d %H:%M')}"
        coupon_info += "；需跳转领取，账号适用与结算扣减未核实"
    if quote_basis == "single_coupon_conditional_estimate":
        coupon_info += f"；按单件满足门槛试算券后价¥{quote_cents / 100:.2f}，非结算实测"
    reference_text = f"参考价¥{reserve_cents / 100:.2f}；" if reserve_cents else "参考价未提供；"
    if (reserve_cents and promotion_cents and reserve_cents > promotion_cents
            and not price_drop_plausible):
        reference_text += "参考价过高，未据此认定优惠；"
    snippet = ("淘宝联盟官方推广 API 返回：" + reference_text
               + f"推广价¥{promotion_cents / 100:.2f}；"
               + (f"推广价低于参考价¥{discount_cents / 100:.2f}。"
                  if price_drop else (f"单件优惠条件试算报价¥{quote_cents / 100:.2f}。" if coupon_applied_cents else "未据此认定价格优势。"))
               + (coupon_info + "。" if coupon_info else "")
               + ((f"接口预估优惠后价¥{promotion_path['estimate_cents'] / 100:.2f}；"
                   if promotion_path.get('complete') else
                   f"当前已返回促销路径可解释至¥{promotion_path['known_estimate_cents'] / 100:.2f}；"
                   + (f"接口另返回¥{promotion_path['returned_estimate_cents'] / 100:.2f}，但差额未被当前路径解释，不展示为到手价；"
                      if promotion_path.get('returned_estimate_cents') else ""))
                  + "、".join(p['title'] + p['description'] for p in promotion_path['components'])
                  + (f"；{promotion_path['purchase_requirement']}" if promotion_path['purchase_requirement'] else "")
                  + "；商品规格、账号资格和结算条件未核实，不作为确定到手价。"
                  if promotion_path else "")
               + ("链接为官方推广入口，尚未独立核实跳转后的商品页。" if item.get("item_id") and not item.get("num_iid") else "")
               + "此为联盟公开推广数据，不是商家结算页到手价或全网市场价。")
    metadata = {
        "taobao_alliance_source_fields": {
            key: item.get(key) for key in (
                "num_iid", "item_id", "title", "item_url", "reserve_price", "zk_final_price",
                "coupon_amount", "coupon_start_fee", "coupon_start_time",
                "coupon_end_time", "coupon_remain_count", "coupon_total_count",
                "coupon_info", "event_start_time", "event_end_time", "shop_title", "user_type",
                "presale_deposit", "presale_start_time", "presale_end_time",
                "presale_tail_start_time", "presale_tail_end_time",
                "final_promotion_price", "final_promotion_path", "predict_rounding_up_price",
                "predict_rounding_up_price_desc",
                "commission_amount", "commission_rate", "income_rate", "commission_type",
            ) if key in item
        },
        "taobao_alliance_offer": {
            "num_iid": str(item.get("num_iid") or ""),
            "item_id": stable_id,
            "link_kind": "official_promotion_link" if item.get("item_id") and not item.get("num_iid") else "direct_product",
            "shop_title": re.sub(r"\s+", " ", str(item.get("shop_title") or "")).strip()[:120],
            "marketplace_key": marketplace,
            "seller_type": str(item.get("user_type") or ""),
            "reserve_price_cents": reserve_cents,
            "promotion_price_cents": promotion_cents,
            "commission_amount_cents": commission_amount_cents,
            "commission_rate": str(item.get("commission_rate") or "").strip()[:40],
            "income_rate": str(item.get("income_rate") or "").strip()[:40],
            "commission_type": str(item.get("commission_type") or "").strip()[:80],
            "promotion_discount_cents": discount_cents,
            "quote_price_cents": quote_cents,
            "quote_basis": quote_basis,
            "coupon_applied_cents": coupon_applied_cents,
            "coupon_amount_cents": coupon_amount_cents,
            "coupon_threshold_cents": coupon_threshold_cents,
            "coupon_start_time": coupon_start.isoformat() if coupon_start else None,
            "coupon_end_time": coupon_end.isoformat() if coupon_end else None,
            "coupon_remain_count": remain_count,
            "event_start_time": event_start.isoformat() if event_start else None,
            "event_end_time": event_end.isoformat() if event_end else None,
            "coupon_state": coupon_state,
            "coupon_start_raw": item.get("coupon_start_time"),
            "coupon_end_raw": item.get("coupon_end_time"),
            "coupon_time_precision": "date" if any(re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(item.get(k) or "")) for k in ("coupon_start_time", "coupon_end_time")) else "time",
            "coupon_amount_basis": "stacking_unknown" if reserve_cents != promotion_cents else "single_coupon_conditional",
            "coupon_availability_state": "exhausted" if remain_count == 0 else "available_observed" if coupon_remaining else "unknown",
            "coupon_user_eligibility": "unknown",
            "coupon_rule_estimate_cents": (promotion_cents - coupon_amount_cents
                if coupon_eligible_by_threshold and coupon_amount_cents < promotion_cents else None),
            "coupon_rule_estimate_basis": "仅单券扣减算式；未证明叠加、账号资格、可领取或结算",
            "coupon_candidate": coupon_candidate,
            "price_drop_candidate": price_drop,
            "reference_drop_plausible": bool(price_drop_plausible),
            "reference_drop_reason": price_drop_reason,
            "promotion_path_candidate": bool(promotion_path),
            "promotion_path": promotion_path,
            "promotion_path_complete": bool(promotion_path and promotion_path.get("complete")),
            "promotion_path_known_estimate_cents": (promotion_path.get("known_estimate_cents")
                                                       if promotion_path else None),
            "promotion_path_unexplained_discount_cents": (promotion_path.get("unexplained_discount_cents")
                                                             if promotion_path else None),
            "rounding_up_price_cents": rounding_up_price_cents,
            "rounding_up_price_description": rounding_up_description,
            "unit": "single_item",
        "price_basis": ("taobao_alliance_coupon_conditional_estimate"
                        if quote_basis == "single_coupon_conditional_estimate"
                        else "taobao_alliance_public_promotion_price"),
        }
    }
    return (f"taobao:{stable_id}", title, url, snippet, None, metadata)


def validated_offer(metadata, now=None):
    """Rebuild from preserved input; cached flags cannot extend a coupon's life."""
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata or "{}")
        except (TypeError, ValueError):
            return None
    if not isinstance(metadata, dict):
        return None
    raw = metadata.get("taobao_alliance_source_fields")
    row = parse_item(raw, now=now) if isinstance(raw, dict) else None
    return row[5]["taobao_alliance_offer"] if row else None


def validated_offer_for_row(row, now=None):
    """Validate immutable source metadata once while one result row is rendered.

    A list request uses the same preserved Alliance material for marketplace
    attribution, quote construction, promotion detection and admission.  All
    of those consumers must use the same current validation, but re-running
    the parser for each consumer is pure duplicate CPU work.  The cache stays
    on the transient row dictionary, is never persisted, and is discarded at
    the end of the request.  An explicit ``now`` always revalidates so worker
    and test callers can keep their chosen clock.
    """
    metadata = row.get("metadata_json") if isinstance(row, dict) else None
    if not isinstance(row, dict) or now is not None:
        return validated_offer(metadata, now=now)
    cached = row.get("_taobao_alliance_offer_cache")
    if isinstance(cached, tuple) and len(cached) == 2 and cached[0] == metadata:
        return cached[1]
    offer = validated_offer(metadata)
    row["_taobao_alliance_offer_cache"] = (metadata, offer)
    return offer


def detail_item_identity(offer, product_url):
    """Return the official material item identity accepted by the detailed API.

    The current upgraded detail API accepts the material's new item ID in its
    dynamic-ID scene.  A promotion short link is the shopping route, not a
    prerequisite for looking up SKU evidence.  We retain it separately as the
    user-facing official promotion entry.
    """
    if not isinstance(offer, dict):
        return None
    item_id = str(offer.get("item_id") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", item_id):
        return None
    return {
        "evidence_kind": "taobao_alliance_material_identity",
        "field_state": "resolved",
        "resolution_method": "material_item_id_dynamic_scene",
        "item_id": item_id,
        "link_kind": offer.get("link_kind"),
        "promotion_link": str(product_url or "").strip(),
    }



def _upgrade_item(item):
    """Map documented V2 fields; never infer a checkout price or coupon eligibility."""
    if not isinstance(item, dict):
        return item
    basic = item.get("item_basic_info")
    price = item.get("price_promotion_info")
    publish = item.get("publish_info")
    if not all(isinstance(part, dict) for part in (basic, price, publish)):
        return {}
    # The upgraded item_id can be opaque. Only an official direct item link
    # with a numeric ID yields a product identity usable by this workbench.
    click_url = str(publish.get("click_url") or "").strip()
    if click_url.startswith("//"):
        click_url = "https:" + click_url
    parsed = urlparse(click_url)
    ids = parse_qs(parsed.query).get("id", [])
    numeric_id = ids[0] if len(ids) == 1 and ids[0].isdigit() else ""
    new_id = str(item.get("item_id") or "").strip()
    income = publish.get("income_info")
    income = income if isinstance(income, dict) else {}
    normalized = {
        "num_iid": numeric_id,
        "item_id": new_id,
        "item_url": click_url,
        "title": basic.get("title"),
        "user_type": basic.get("user_type"),
        "shop_title": basic.get("shop_title"),
        "reserve_price": price.get("reserve_price"),
        "zk_final_price": price.get("zk_final_price"),
        "final_promotion_price": price.get("final_promotion_price"),
        "predict_rounding_up_price": price.get("predict_rounding_up_price"),
        "final_promotion_path": (price.get("final_promotion_path_list") or {}).get("final_promotion_path_map_data")
            if isinstance(price.get("final_promotion_path_list"), dict) else None,
        "predict_rounding_up_price_desc": price.get("predict_rounding_up_price_desc"),
        # The documented V2 response keeps the concrete commission amount
        # under publish_info.income_info. Preserve it as returned; callers
        # must not derive an amount from a rate.
        "commission_amount": income.get("commission_amount"),
        "commission_rate": income.get("commission_rate"),
        "income_rate": publish.get("income_rate"),
        "commission_type": publish.get("commission_type"),
    }
    presale = item.get("presale_info")
    if isinstance(presale, dict):
        normalized.update({key: presale[key] for key in (
            "presale_deposit", "presale_start_time", "presale_end_time",
            "presale_tail_start_time", "presale_tail_end_time") if key in presale})
    return normalized


def _rows_from_payload(payload, now=None, report=None):
    if not isinstance(payload, dict):
        raise TaobaoAllianceAPIError("淘宝联盟 API 响应不是 JSON 对象", "schema_mismatch")
    wrapped = payload.get("tbk_dg_material_optional_upgrade_response", payload.get("tbk_dg_material_optional_response", payload))
    if not isinstance(wrapped, dict):
        raise TaobaoAllianceAPIError("淘宝联盟 API 响应结构异常", "schema_mismatch")
    result_list = wrapped.get("result_list")
    items = result_list.get("map_data") if isinstance(result_list, dict) else None
    if items is None and isinstance(result_list, list):
        items = result_list
    if not isinstance(items, list):
        raise TaobaoAllianceAPIError("淘宝联盟 API 未返回 result_list.map_data", "schema_mismatch")
    rows = []
    if report is not None:
        report["raw_items"] = items
        report["rejections"] = []
    if len(items) > 100:
        raise TaobaoAllianceAPIError("淘宝联盟单页条目超过100，响应契约异常", "schema_mismatch")
    for item in items:
        if "tbk_dg_material_optional_upgrade_response" in payload:
            item = _upgrade_item(item)
        row = parse_item(item, now=now)
        if row:
            rows.append(row)
        elif report is not None:
            reason = ("invalid_item" if not isinstance(item, dict) else
                      "unsupported_marketplace" if not marketplace_from_seller_type(item.get("user_type")) else
                      "presale_requires_plan" if _is_presale(item) else
                      "invalid_product_identity" if not _product_url(item) else
                      "missing_or_invalid_price" if not _money_cents(item.get("zk_final_price")) else
                      "invalid_title_or_activity_window")
            report["rejections"].append({"index": len(rows) + len(report["rejections"]), "reason": reason})
    return rows


def _signature(parameters, secret):
    source = "".join(f"{key}{parameters[key]}" for key in sorted(parameters))
    return hmac.new(secret.encode("utf-8"), source.encode("utf-8"), hashlib.md5).hexdigest().upper()


def _request_top_payload(method, business_params, *, http_get=None, env=None, now=None, report=None):
    """Call one signed TOP endpoint and retain request-neutral diagnostics.

    The caller supplies only documented business parameters.  Credentials are
    read from the local environment, used for the signature, and never copied
    into reports or persisted campaign metadata.
    """
    env = os.environ if env is None else env
    missing = configuration_error(env)
    if missing:
        raise TaobaoAllianceConfigurationError(missing)
    now = now or datetime.now(CHINA_TZ)
    params = {
        "method": method,
        "app_key": str(env["TAOBAO_ALLIANCE_APP_KEY"]).strip(),
        "sign_method": "hmac",
        "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
        "format": "json",
        "v": "2.0",
        "adzone_id": str(env["TAOBAO_ALLIANCE_ADZONE_ID"]).strip(),
        **business_params,
    }
    params["sign"] = _signature(params, str(env["TAOBAO_ALLIANCE_APP_SECRET"]))
    get = http_get or requests.get
    try:
        response = get(API_ENDPOINT, params=params, timeout=(5, 20),
                       allow_redirects=False, stream=True)
    except requests.RequestException as exc:
        raise TaobaoAllianceAPIError(f"淘宝联盟 API 网络请求失败（{type(exc).__name__}）", "network_error") from exc
    try:
        if report is not None:
            report["http_status"] = response.status_code
        if response.status_code in (301, 302, 303, 307, 308):
            raise TaobaoAllianceAPIError("淘宝联盟 API 地址发生跳转，已拒绝跟随")
        if response.status_code >= 400:
            raise TaobaoAllianceAPIError(f"淘宝联盟 API 返回 HTTP {response.status_code}",
                "access_restricted" if response.status_code in (401,403) else "rate_limited" if response.status_code == 429 else "http_error")
        chunks, size = [], 0
        for chunk in response.iter_content(65536):
            size += len(chunk)
            if size > 4_000_000:
                raise TaobaoAllianceAPIError("淘宝联盟 API 响应超过 4 MB 限制")
            chunks.append(chunk)
        body = b"".join(chunks)
        if report is not None:
            report["response_sha256"] = hashlib.sha256(body).hexdigest()
            report["response_bytes"] = len(body)
        try:
            payload = json.loads(body.decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError) as exc:
            restricted = any(marker in body.lower() for marker in (b'login.taobao.com', b'captcha', '请登录'.encode()))
            raise TaobaoAllianceAPIError("淘宝联盟返回登录/验证页" if restricted else "淘宝联盟 API 返回内容不是合法 JSON",
                                        "access_restricted" if restricted else "invalid_response") from exc
        if isinstance(payload, dict) and payload.get("error_response"):
            error = payload["error_response"]
            code = re.sub(r"[^a-zA-Z0-9_.-]", "", str(error.get("code", "")))[:30] if isinstance(error, dict) else ""
            sub_code = re.sub(r"[^a-zA-Z0-9_.-]", "", str(error.get("sub_code", "")))[:80] if isinstance(error, dict) else ""
            if report is not None:
                report["top_error_code"] = code
                report["top_sub_code"] = sub_code
            reason = "api_package_unavailable" if sub_code == "isv.permission-api-package-limit" else "api_error"
            raise TaobaoAllianceAPIError(f"淘宝联盟 API 返回错误（code={code}, sub_code={sub_code}）", reason, sub_code)
        if not isinstance(payload, dict):
            raise TaobaoAllianceAPIError("淘宝联盟 API 响应不是 JSON 对象", "schema_mismatch")
        return payload
    except requests.RequestException as exc:
        raise TaobaoAllianceAPIError(f"淘宝联盟 API 读取失败（{type(exc).__name__}）", "network_error") from exc
    finally:
        response.close()


def _fetch_page(source_url, *, http_get=None, env=None, now=None, report=None):
    """Fetch one bounded official API page and return scanner-compatible rows."""
    query = search_config(source_url)
    query.pop("max_pages", None)
    query.setdefault("has_coupon", "false")
    payload = _request_top_payload(API_METHOD, query, http_get=http_get, env=env, now=now, report=report)
    response_body = payload.get("tbk_dg_material_optional_upgrade_response")
    if not isinstance(response_body, dict):
        response_body = payload.get("tbk_dg_material_optional_response")
    if isinstance(response_body, dict) and report is not None:
        try:
            total_results = int(response_body.get("total_results"))
        except (TypeError, ValueError):
            total_results = None
        if total_results is not None and 0 <= total_results <= 1_000_000:
            report["official_total_results"] = total_results
    return _rows_from_payload(payload, now=now, report=report)


def _official_activity_link(value):
    value = str(value or "").strip()
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if (parsed.scheme == "https" and parsed.path and
            (host == "taobao.com" or host.endswith(".taobao.com")
             or host == "tmall.com" or host.endswith(".tmall.com"))):
        return value
    return ""


def fetch_activity_info(activity_material_id, *, http_get=None, env=None, now=None, report=None):
    """Read one official activity hall; this never turns a hall into a product.

    ``activity_material_id`` belongs only to the activity-info endpoint.  The
    optional product-feed ``material_id`` remains a separate source setting.
    """
    activity_material_id = str(activity_material_id or "").strip()
    if not re.fullmatch(r"[0-9]{1,24}", activity_material_id):
        raise ValueError("官方活动 ID 必须为 1 到 24 位数字")
    if report is not None:
        report.update(endpoint=API_ENDPOINT, method=ACTIVITY_INFO_METHOD,
                      activity_material_id=activity_material_id,
                      captured_at=(now or datetime.now(CHINA_TZ)).isoformat())
    payload = _request_top_payload(
        ACTIVITY_INFO_METHOD, {"activity_material_id": activity_material_id},
        http_get=http_get, env=env, now=now, report=report,
    )
    response = payload.get("tbk_activity_info_get_response")
    data = response.get("data") if isinstance(response, dict) else None
    if not isinstance(data, dict):
        raise TaobaoAllianceAPIError("淘宝联盟活动 API 未返回 data", "schema_mismatch")
    return {
        "activity_material_id": activity_material_id,
        "page_name": str(data.get("page_name") or "").strip()[:256],
        "page_start_time": str(data.get("page_start_time") or "").strip()[:64],
        "page_end_time": str(data.get("page_end_time") or "").strip()[:64],
        "promotion_url": _official_activity_link(data.get("click_url")),
        "short_promotion_url": _official_activity_link(data.get("short_click_url")),
        "raw_payload": payload,
    }


def fetch_promoter_material_ids(*, material_type=1, http_get=None, env=None, now=None, report=None):
    """List promotion materials by documented type: 1 product or 2 entitlement.

    These IDs are not activity hall IDs. Only type 1 may become a product feed.
    """
    if material_type not in (1, 2):
        raise ValueError("官方物料类型仅支持商品或权益")
    page_size = 100
    page_no = 1
    materials = []
    seen = set()
    total_count = None
    while page_no <= 100:
        query = {"page_no": page_no, "subject": 1, "material_type": material_type,
                 "page_size": page_size}
        payload = _request_top_payload(
            MATERIAL_IDS_METHOD,
            {"material_query": json.dumps(query, separators=(",", ":"))},
            http_get=http_get, env=env, now=now, report=report,
        )
        response = payload.get("tbk_optimus_tou_material_ids_get_response")
        data = response.get("data") if isinstance(response, dict) else None
        page = data.get("tou_materials") if isinstance(data, dict) else None
        try:
            total_count = int(response.get("total_count"))
        except (TypeError, ValueError, AttributeError):
            total_count = None
        if page is None and total_count == 0:
            page = []
        if not isinstance(page, list):
            raise TaobaoAllianceAPIError("淘宝联盟物料列表未返回 tou_materials", "schema_mismatch")
        accepted = 0
        for item in page:
            if not isinstance(item, dict):
                continue
            material_id = str(item.get("material_id") or "").strip()
            if (not re.fullmatch(r"[0-9]{1,24}", material_id)
                    or material_id in seen
                    or str(item.get("material_type")) != str(material_type)
                    or str(item.get("subject")) != "1"):
                continue
            seen.add(material_id)
            accepted += 1
            material = {
                "material_id": material_id,
                "material_name": re.sub(r"\s+", " ", str(item.get("material_name") or "")).strip()[:160],
                "material_type": material_type,
                "subject": 1,
                "start_time": item.get("start_time"),
                "end_time": item.get("end_time"),
            }
            for key in ("start_time", "end_time"):
                try:
                    stamp = int(material[key])
                    material[key] = datetime.fromtimestamp(stamp, CHINA_TZ).strftime("%Y-%m-%d %H:%M:%S")
                except (TypeError, ValueError, OverflowError, OSError):
                    material[key] = ""
            materials.append(material)
        if not page or len(page) < page_size or (total_count is not None and page_no * page_size >= total_count):
            break
        page_no += 1
    if report is not None:
        report.update(endpoint=API_ENDPOINT, method=MATERIAL_IDS_METHOD,
                      subject=1, material_type=material_type, pages=page_no,
                      total_count=total_count, returned_count=len(materials))
    return materials

def fetch_rows(source_url, *, http_get=None, env=None, now=None, report=None):
    """Bounded pages; preserve rejected input and partial success for diagnosis."""
    from urllib.parse import urlencode
    query = search_config(source_url)
    max_pages = query.pop("max_pages", "all")
    start = int(query["page_no"])
    last_page = 1000 if max_pages == "all" else min(start + int(max_pages) - 1, 1000)
    pages, rows, seen, raw_item_keys = [], [], set(), set()
    official_total_results = None
    official_last_page = None
    if report is not None:
        report.update(pages=pages, status="running", raw_count=None, parsed_count=None,
                      promotion_count=None, duplicate_count=0, rejected_count=None,
                      parser_version="taobao-alliance-upgrade-2026-10-10", request_query=query,
                      endpoint=API_ENDPOINT, method=API_METHOD,
                      max_pages=max_pages, captured_at=(now or datetime.now(CHINA_TZ)).isoformat())
    for page_no in range(start, last_page + 1):
        page = {"page_no": page_no}
        try:
            result = _fetch_page(API_ENDPOINT + "?" + urlencode(dict(query, page_no=str(page_no))),
                                 http_get=http_get, env=env, now=now, report=page)
        except (TaobaoAllianceAPIError, TaobaoAllianceConfigurationError) as exc:
            if report is not None:
                report.update(status="partial" if pages else "failed", failed_page=page_no,
                              next_page=page_no, failed_page_evidence=page, error_code=getattr(exc, "code", type(exc).__name__))
            if pages:
                return rows
            raise
        pages.append(page)
        page_total = page.get("official_total_results")
        if isinstance(page_total, int) and page_total >= 0:
            official_total_results = max(official_total_results or 0, page_total)
            official_last_page = min(1000, max(start, (official_total_results + int(query["page_size"]) - 1)
                                                // int(query["page_size"])))
            if report is not None:
                # The official count is diagnostic only. A short page remains
                # the stronger completion signal for this application's access.
                report["official_total_results"] = official_total_results
        page_raw_keys = {
            str(item.get("item_id") or item.get("num_iid") or "").strip()
            for item in page["raw_items"] if isinstance(item, dict)
        }
        page_raw_keys.discard("")
        repeated_page = bool(page_raw_keys) and page_raw_keys.issubset(raw_item_keys)
        raw_item_keys.update(page_raw_keys)
        for row in result:
            if row[0] in seen:
                if report is not None:
                    report["duplicate_count"] += 1
                continue
            seen.add(row[0]); rows.append(row)
        if report is not None:
            report.update(raw_count=sum(len(p["raw_items"]) for p in pages), parsed_count=len(rows),
                          rejected_count=sum(len(p["rejections"]) for p in pages),
                          promotion_count=sum(bool(r[5]["taobao_alliance_offer"]["price_drop_candidate"] or
                                                   r[5]["taobao_alliance_offer"]["coupon_candidate"] or
                                                   r[5]["taobao_alliance_offer"]["promotion_path_candidate"]) for r in rows))
        if (len(page["raw_items"]) < int(query["page_size"])
                or repeated_page
                or (official_last_page is not None and page_no >= official_last_page)):
            break
    if report is not None:
        report["status"] = "complete"
        if repeated_page:
            report["stop_reason"] = "repeated_page"
            report["next_page"] = None
        elif len(page["raw_items"]) < int(query["page_size"]):
            report["stop_reason"] = "page_exhausted"
            report["next_page"] = None
        elif official_last_page is not None and page_no >= official_last_page:
            report["stop_reason"] = "official_total_exhausted"
            report["official_total_results"] = official_total_results
            report["next_page"] = None
        elif max_pages == "all":
            report["stop_reason"] = "api_page_ceiling"
            report["next_page"] = None
        else:
            report["stop_reason"] = "page_limit"
            report["next_page"] = page_no + 1 if page_no < 1000 else None
    return rows
