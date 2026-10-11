"""Exact-SKU evidence from the two documented Taobao Alliance detail APIs."""
import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import requests

from services.taobao_alliance import (
    API_ENDPOINT, TaobaoAllianceAPIError, TaobaoAllianceConfigurationError,
    _signature, configuration_error,
)


# Both documented detailed endpoints accept the official material's dynamic
# item ID in ``biz_scene_id=1`` and expose SKU, stock and SKU-price fields.
# The legacy detailed method is a peer diagnostic path, never a product-level
# fallback.  The simple item-info API remains deliberately excluded because it
# cannot produce a price bound to a selected SKU.
DETAIL_METHOD = "taobao.tbk.item.details.upgrade.get"
LEGACY_DETAIL_METHOD = "taobao.tbk.item.details.get"
SKU_DETAIL_METHODS = (DETAIL_METHOD, LEGACY_DETAIL_METHOD)
ACCESS_RETRY_HOURS = 24
CHINA_TZ = timezone(timedelta(hours=8))


def _cents(value):
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not amount.is_finite() or amount <= 0 or amount > Decimal("10000000"):
        return None
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _list(container, key):
    value = container.get(key) if isinstance(container, dict) else None
    if isinstance(value, list):
        return value
    if not isinstance(value, dict):
        return []
    nested = value.get(key)
    return nested if isinstance(nested, list) else [nested] if isinstance(nested, dict) else []


def _millisecond_time(value):
    try:
        return datetime.fromtimestamp(int(value) / 1000, CHINA_TZ)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def parse_detail_payload(payload, requested_item_id, now=None):
    """Return exact-SKU evidence only when those fields are really present."""
    now = (now or datetime.now(CHINA_TZ)).astimezone(CHINA_TZ)
    wrapped = payload if isinstance(payload, dict) else None
    if isinstance(wrapped, dict):
        wrapped = (wrapped.get("tbk_item_details_upgrade_get_response")
                   or wrapped.get("tbk_item_details_get_response")
                   or wrapped)
    if not isinstance(wrapped, dict):
        raise TaobaoAllianceAPIError("淘宝联盟商品详情响应结构异常", "schema_mismatch")
    details = _list(wrapped.get("results"), "tbk_item_detail")
    # In the dynamic-ID scene, the material's new item ID is echoed in
    # ``input_item_iid`` while ``item_id`` may be a canonical ID. Both are
    # official response-bound identities; a title is never used for matching.
    matched = [item for item in details if str(item.get("item_id") or "") == str(requested_item_id)
               or str(item.get("input_item_iid") or "") == str(requested_item_id)]
    if len(matched) != 1:
        return _unavailable(requested_item_id, "官方详情未返回与请求商品ID唯一匹配的商品记录")
    item = matched[0]
    raw_skus = [raw for raw in _list(item.get("sku_list"), "tbk_item_detail_sku")
                if isinstance(raw, dict)]
    # A one-SKU item can legitimately have no selectable property list.  Its
    # SKU id still binds price and stock, but it cannot safely be treated as a
    # comparable market specification.  Multi-SKU records need property
    # labels, otherwise a parser would be guessing which variant was priced.
    sole_sku = len(raw_skus) == 1
    candidates = []
    for raw in raw_skus:
        sku_id = str(raw.get("sku_id") or "").strip()
        try:
            quantity = int(raw.get("quantity"))
        except (TypeError, ValueError):
            quantity = 0
        labels = []
        for prop in _list(raw.get("property_list"), "tbk_item_detail_sku_prop"):
            if not isinstance(prop, dict):
                continue
            name = re.sub(r"\s+", " ", str(prop.get("property_text") or "")).strip()
            value = re.sub(r"\s+", " ", str(prop.get("value_alias_text") or prop.get("value_text") or "")).strip()
            if name and value:
                labels.append(f"{name}：{value}")
        price = raw.get("sku_price_promotion_info")
        sale_cents = _cents(price.get("sku_zk_final_price")) if isinstance(price, dict) else None
        estimate_cents = _cents(price.get("sku_final_promotion_price")) if isinstance(price, dict) else None
        paths = _list(price.get("sku_final_promotion_path_list"), "sku_final_promotion_path_map_data") if isinstance(price, dict) else []
        current_paths, total_discount = [], 0
        for path in paths:
            if not isinstance(path, dict):
                continue
            fee = _cents(path.get("promotion_fee"))
            start, end = _millisecond_time(path.get("promotion_start_time")), _millisecond_time(path.get("promotion_end_time"))
            if not fee or not start or not end or not start <= now < end:
                continue
            total_discount += fee
            current_paths.append({
                "title": str(path.get("promotion_title") or "")[:80],
                "description": str(path.get("promotion_desc") or "")[:160],
                "discount_cents": fee, "start_at": start.isoformat(), "end_at": end.isoformat(),
            })
        legacy_offer_cents = _cents(raw.get("sku_final_price"))
        legacy_reserve_cents = _cents(raw.get("sku_reserve_price"))
        if not sale_cents and legacy_offer_cents:
            sale_cents = legacy_offer_cents
        promotion_bound = bool(sale_cents and estimate_cents and estimate_cents < sale_cents
                               and current_paths and total_discount == sale_cents - estimate_cents)
        direct_discount = bool(legacy_offer_cents and legacy_reserve_cents
                               and legacy_offer_cents < legacy_reserve_cents)
        specification = "；".join(labels)
        specification_comparable = bool(labels)
        if sku_id and quantity > 0 and sale_cents and (labels or sole_sku):
            if not specification:
                specification = "官方唯一 SKU（未返回规格属性）"
            candidates.append({
                "sku_id": sku_id, "quantity_observed": quantity,
                "specification": specification,
                "specification_comparable": specification_comparable,
                "sku_binding_mode": "property_selected" if labels else "sole_sku",
                "public_sale_cents": sale_cents,
                "promotion_estimate_cents": estimate_cents if promotion_bound else None,
                "promotion_path": current_paths if promotion_bound else [],
                "reference_cents": legacy_reserve_cents if direct_discount else None,
                "promotional_candidate": promotion_bound or direct_discount,
            })
    if not candidates:
        basic = item.get("item_basic_info") or {}
        price = item.get("price_promotion_info") or {}
        product_level_prices = {
            "public_sale_cents": _cents(price.get("zk_final_price")),
            "promotion_estimate_cents": _cents(price.get("final_promotion_price")),
            "reference_cents": _cents(price.get("reserve_price")),
        }
        detail = _unavailable(
            requested_item_id,
            "官方商品详情接口仅返回商品级信息，未提供可绑定的SKU、规格和库存；商品级优惠不作为可用报价",
        )
        detail.update({
            "item_id": str(item.get("item_id") or ""),
            "title": str(basic.get("title") or ""),
            "product_level_price_observed": any(value is not None for value in product_level_prices.values()),
            "product_level_prices": product_level_prices,
        })
        return detail
    promotional = [sku for sku in candidates if sku["promotional_candidate"]]
    selected = min(promotional or candidates,
                   key=lambda sku: ((sku["promotion_estimate_cents"] or sku["public_sale_cents"]), sku["sku_id"]))
    promoted = selected["promotional_candidate"]
    conditions = "；".join(filter(None, [
        "、".join(path["title"] + path["description"] for path in selected["promotion_path"]),
        ("官方接口为预估到手价，账号资格、运费和结算结果仍需下单页确认"
         if selected["promotion_estimate_cents"] else
         "官方接口返回同一SKU一口价与折扣价；库存和价格为本次公开观察值" if promoted else
         "官方详情仅返回SKU销售价，未形成SKU级优惠证据"),
    ]))
    return {
        "evidence_kind": "taobao_alliance_sku_detail", "field_state": "bound",
        "field_reason": "已由官方详细接口绑定商品ID、SKU、规格、库存和价格",
        "requested_item_id": str(requested_item_id), "item_id": str(item.get("item_id")),
        "title": str((item.get("item_basic_info") or {}).get("title") or ""),
        "sku_id": selected["sku_id"], "specification": selected["specification"],
        "specification_comparable": selected["specification_comparable"],
        "sku_binding_mode": selected["sku_binding_mode"],
        # Keep the comparable field separate from the display text.  The text
        # above is an honest statement that this is the only returned SKU, not
        # a claim about package size, colour, model or other product attributes.
        "comparison_specification": (selected["specification"]
                                     if selected["specification_comparable"] else ""),
        # A direct SKU price drop has no estimated checkout field.  Keep the
        # documented SKU sale price in that case; choosing a missing estimate
        # would erase a valid amount even though the same response binds it to
        # this SKU and its reference price.
        "advertised_cents": (selected["promotion_estimate_cents"]
                              if selected["promotion_estimate_cents"] is not None
                              else selected["public_sale_cents"]),
        "public_sale_cents": selected["public_sale_cents"],
        "promotion_estimate_cents": selected["promotion_estimate_cents"],
        "reference_cents": selected["reference_cents"],
        "promotion_path": selected["promotion_path"], "quantity_observed": selected["quantity_observed"],
        "promotional_candidate": promoted, "conditions": conditions, "published_at": None,
        "candidate_sku_count": len(candidates),
    }


def _unavailable(item_id, reason):
    return {
        "evidence_kind": "taobao_alliance_sku_detail", "field_state": "field_not_provided",
        "field_reason": reason, "requested_item_id": str(item_id), "title": "", "advertised_cents": None,
        "specification": "", "conditions": "", "published_at": None,
        "promotional_candidate": False,
    }


def _request_params(item_id, env, now, method=DETAIL_METHOD):
    params = {
        "app_key": str(env["TAOBAO_ALLIANCE_APP_KEY"]).strip(),
        "sign_method": "hmac", "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
        "format": "json", "v": "2.0", "item_id": item_id, "method": method,
    }
    params.update({"biz_scene_id": "1", "get_topn_rate": "0", "get_tlj_info": "0"})
    params["sign"] = _signature(params, str(env["TAOBAO_ALLIANCE_APP_SECRET"]))
    return params


def _api_error(payload):
    error = payload.get("error_response") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return None
    code = re.sub(r"[^a-zA-Z0-9_.-]", "", str(error.get("sub_code") or error.get("code") or ""))[:80]
    return TaobaoAllianceAPIError(
        f"淘宝联盟商品详情接口错误（{code}）",
        "api_package_unavailable" if "permission" in code else "api_error",
        code,
    )


def _request_detail(item_id, method, *, get, env, now):
    """Make one bounded call to one SKU-capable TOP detail method."""
    response = None
    try:
        params = _request_params(item_id, env, now, method)
        response = get(API_ENDPOINT, params=params, timeout=(5, 20), allow_redirects=False, stream=True)
        if response.status_code >= 400:
            raise TaobaoAllianceAPIError(f"淘宝联盟商品详情返回 HTTP {response.status_code}", "http_error")
        body = b"".join(response.iter_content(65536))
        if len(body) > 4_000_000:
            raise TaobaoAllianceAPIError("淘宝联盟商品详情响应超过 4 MB 限制", "schema_mismatch")
        payload = json.loads(body.decode("utf-8-sig"))
        error = _api_error(payload)
        if error:
            raise error
        return parse_detail_payload(payload, item_id, now=now), body
    finally:
        if response is not None:
            response.close()


def _attempt_from_error(method, error):
    return {
        "method": method,
        "state": error.code,
        "remote_code": error.remote_code,
    }


def _evidence(result):
    return json.dumps({key: result.get(key) for key in (
        "requested_item_id", "item_id", "sku_id", "specification", "public_sale_cents",
        "promotion_estimate_cents", "promotion_path", "quantity_observed",
        "product_level_price_observed", "product_level_prices", "detail_api_attempts",
    )}, ensure_ascii=False)


def fetch_item_detail(item_id, *, http_get=None, env=None, now=None, report=None):
    env = os.environ if env is None else env
    missing = configuration_error(env)
    if missing:
        raise TaobaoAllianceConfigurationError(missing)
    item_id = str(item_id or "").strip()
    # The current detailed endpoint supports the official material's new
    # opaque item ID in ``biz_scene_id=1``. A title or arbitrary URL is still
    # never accepted as an identity.
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", item_id):
        raise ValueError("淘宝商品ID格式无效")
    now = now or datetime.now(CHINA_TZ)
    get = http_get or requests.get
    attempts = []
    response_bodies = []
    errors = []
    last_result = None
    for method in SKU_DETAIL_METHODS:
        try:
            result, body = _request_detail(item_id, method, get=get, env=env, now=now)
            response_bodies.append(body)
            attempts.append({"method": method, "state": "returned"})
            # A product-level or identity-mismatched response does not satisfy
            # this method's contract. Probe the documented detailed peer before
            # declaring the SKU evidence unavailable.
            if result.get("field_state") == "bound":
                result["detail_api_attempts"] = attempts
                if report is not None:
                    report.update(method=method, attempts=attempts,
                                  response_sha256=hashlib.sha256(body).hexdigest(), response_bytes=len(body),
                                  field_state=result["field_state"], sku_count=result.get("candidate_sku_count", 0))
                result["evidence"] = _evidence(result)
                return result
            last_result = result
        except TaobaoAllianceAPIError as exc:
            errors.append(exc)
            attempts.append(_attempt_from_error(method, exc))
        except (requests.RequestException, UnicodeDecodeError, json.JSONDecodeError) as exc:
            error = TaobaoAllianceAPIError(
                f"淘宝联盟商品详情读取失败（{type(exc).__name__}）", "network_error")
            errors.append(error)
            attempts.append(_attempt_from_error(method, error))
    if last_result is not None:
        last_result["detail_api_attempts"] = attempts
        if report is not None:
            report.update(method=attempts[-1]["method"], attempts=attempts,
                          response_sha256=(hashlib.sha256(response_bodies[-1]).hexdigest() if response_bodies else ""),
                          response_bytes=(len(response_bodies[-1]) if response_bodies else 0),
                          field_state=last_result["field_state"], sku_count=last_result.get("candidate_sku_count", 0))
        last_result["evidence"] = _evidence(last_result)
        return last_result
    if errors:
        # A concrete package denial must stay visible even if the peer call
        # emits a less-specific API error. Every underlying response is kept in
        # ``attempts`` so that the UI never pretends both calls had one cause.
        terminal = next((error for error in errors if error.code == "api_package_unavailable"), errors[-1])
        terminal.attempts = attempts
        raise terminal
    raise TaobaoAllianceAPIError("淘宝联盟详细接口未返回可解析响应", "api_error")
