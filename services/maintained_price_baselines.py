"""Operator-maintained price bands for exact, explainable deal comparisons.

These bands are local product policy. They are deliberately separate from source
claims and do not assert an account checkout price or a market-wide lowest price.
"""
from __future__ import annotations

import json
import re
import unicodedata
from datetime import date
from fractions import Fraction
from typing import Any

from services.category_comparison_rules import (COUNT_UNITS, UNIT_MODES,
                                                normalized_spec, validate_rule)


MAX_TERMS = 12
MAX_BASE_QUANTITY = 1_000_000
MAX_PRICE_CENTS = 100_000_000


def _text(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).casefold()


def _spec_text(value: Any) -> str:
    """Normalize only spelling separators for an operator-entered fixed spec."""
    return re.sub(r"[\s×xX*·，,、；;:/]+", "", _text(value))


def _round_half_up(value: Fraction) -> int:
    """Keep displayed and compared unit prices on the integer-cent money scale."""
    if value < 0:
        raise ValueError("维护基准单价不能为负数")
    return (value.numerator * 2 + value.denominator) // (2 * value.denominator)


def parse_terms(raw: str) -> list[str]:
    """Return ordered, normalized required title terms from a form field."""
    terms: list[str] = []
    for value in re.split(r"[,，\n]+", raw or ""):
        value = unicodedata.normalize("NFKC", value).strip()
        if value and value not in terms:
            terms.append(value)
    if not 2 <= len(terms) <= MAX_TERMS or any(len(term) > 50 for term in terms):
        raise ValueError("匹配词须包含 2 到 12 个品牌/品名/规格必要词，每项不超过 50 字")
    return terms


def decode_terms(raw: str) -> list[str]:
    try:
        terms = json.loads(raw or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(terms, list) or not all(isinstance(term, str) and term.strip() for term in terms):
        return []
    return terms


def unit_label(baseline: dict[str, Any]) -> str:
    mode = baseline["unit_mode"]
    amount = baseline["base_quantity"]
    if mode == "count":
        return f"每 {amount} {baseline['count_unit']}"
    if mode == "mass":
        return f"每 {amount} 克"
    if mode == "volume":
        return f"每 {amount} 毫升"
    return "每件固定规格"


def validate_baseline(value: dict[str, Any]) -> dict[str, Any]:
    required = {
        "category_id", "name", "match_terms", "unit_mode", "count_unit", "base_quantity",
        "exact_specification", "bargain_cents", "low_cents", "typical_cents", "high_cents",
        "effective_from", "enabled", "notes",
    }
    if set(value) != required:
        raise ValueError("维护基准字段不完整")
    if not isinstance(value["category_id"], int) or value["category_id"] <= 0:
        raise ValueError("请选择有效品类")
    if not isinstance(value["name"], str) or not 2 <= len(value["name"].strip()) <= 120:
        raise ValueError("基准名称须为 2 到 120 个字")
    terms = value["match_terms"]
    if (not isinstance(terms, list) or not 2 <= len(terms) <= MAX_TERMS
            or any(not isinstance(term, str) or not term.strip() or len(term.strip()) > 50 for term in terms)):
        raise ValueError("匹配词须包含 2 到 12 个品牌/品名/规格必要词")
    mode = value["unit_mode"]
    if mode not in UNIT_MODES:
        raise ValueError("计价模式无效")
    count_unit = value["count_unit"]
    if not isinstance(count_unit, str) or (mode == "count" and count_unit not in COUNT_UNITS):
        raise ValueError("计数模式须选择明确单位")
    if mode != "count" and count_unit:
        raise ValueError("非计数模式不能填写计数单位")
    quantity = value["base_quantity"]
    if isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= MAX_BASE_QUANTITY:
        raise ValueError("基准单位数量须为 1 到 1000000 的整数")
    specification = value["exact_specification"].strip()
    if mode == "exact_spec":
        if quantity != 1 or not specification or len(specification) > 200:
            raise ValueError("固定规格按件比较时，单位数量必须为 1 且须填写固定规格")
    elif specification:
        raise ValueError("只有固定规格按件比较可填写固定规格")
    prices = [value[key] for key in ("bargain_cents", "low_cents", "typical_cents", "high_cents")]
    if any(isinstance(price, bool) or not isinstance(price, int) or not 1 <= price <= MAX_PRICE_CENTS for price in prices):
        raise ValueError("所有价格带必须是正数")
    if not prices[0] <= prices[1] <= prices[2] <= prices[3]:
        raise ValueError("价格带必须满足：可捡漏阈值 ≤ 常规低位 ≤ 常规中位 ≤ 常规高位")
    try:
        effective_from = date.fromisoformat(value["effective_from"])
    except (TypeError, ValueError) as exc:
        raise ValueError("生效日期格式无效") from exc
    if not isinstance(value["enabled"], bool):
        raise ValueError("启用状态无效")
    if not isinstance(value["notes"], str) or len(value["notes"]) > 500:
        raise ValueError("维护说明不得超过 500 字")
    return dict(value, name=value["name"].strip(), match_terms=[term.strip() for term in terms],
                exact_specification=specification, effective_from=effective_from.isoformat(),
                notes=value["notes"].strip())


def baseline_rule(baseline: dict[str, Any]) -> dict[str, Any]:
    return validate_rule({
        "schema_version": 1,
        "identity_mode": "merchant_or_exact_title",
        "unit_mode": baseline["unit_mode"],
        "count_unit": baseline["count_unit"],
    })


def load_baselines(db) -> list[dict[str, Any]]:
    rows = db.execute("""SELECT b.*,c.name AS category_name,c.topic_key,
        c.match_terms_json AS category_match_terms_json
        FROM maintained_price_baselines b JOIN benchmark_categories c ON c.id=b.category_id
        WHERE b.enabled=1 AND c.enabled=1
        ORDER BY c.name,b.name,b.id""").fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["match_terms"] = decode_terms(item.pop("match_terms_json", "[]"))
        item["category_match_terms"] = decode_terms(item.pop("category_match_terms_json", "[]"))
        result.append(item)
    return result


def _base_result(state: str, label: str, note: str) -> dict[str, Any]:
    return {
        "state": state, "label": label, "note": note, "rank": 0,
        "baseline_id": None, "baseline_name": "", "baseline_version": None,
        "unit_label": "", "current_unit_cents": None, "bargain_cents": None,
        "low_cents": None, "typical_cents": None, "high_cents": None,
        "typical_delta_cents": None, "matched_by": "",
    }


def assess_maintained_baseline(row: dict[str, Any], brief: dict[str, Any], quote: dict[str, Any] | None,
                                baselines: list[dict[str, Any]], today: date | None = None) -> dict[str, Any]:
    """Return one fail-closed maintained-band assessment for a displayed offer."""
    if not quote or quote.get("kind") != "official_promotion_observation":
        return _base_result("not_official_promotion", "未进入维护基准判断", "仅淘宝联盟官方推广价可进入本期维护基准判断。")
    amount = quote.get("amount_cents")
    if not isinstance(amount, int) or amount <= 0:
        return _base_result("missing_price", "未进入维护基准判断", "官方推广价缺失或无效。")
    today = today or date.today()
    topic = row.get("display_topic") or row.get("topic") or "other"
    title = row.get("title") or brief.get("title") or ""
    normalized_title = _text(title)
    candidates: list[tuple[dict[str, Any], int, str]] = []
    measurement_failures = 0
    for baseline in baselines:
        if baseline.get("topic_key") != topic:
            continue
        try:
            active_from = date.fromisoformat(baseline["effective_from"])
        except (TypeError, ValueError):
            continue
        if active_from > today or not baseline.get("match_terms"):
            continue
        normalized_terms = [_text(term) for term in baseline["match_terms"]]
        if not all(term in normalized_title for term in normalized_terms):
            continue
        category_terms = [_text(term) for term in baseline.get("category_match_terms", [])]
        if category_terms and not any(term in normalized_title for term in category_terms):
            continue
        if baseline["unit_mode"] == "exact_spec":
            if _spec_text(baseline["exact_specification"]) not in _spec_text(title):
                continue
            current_unit = amount
            matched_by = "标题必要词与固定规格完全命中"
        else:
            _, _, measured, measurement_error = normalized_spec(title, title, baseline_rule(baseline))
            if measurement_error or measured is None:
                measurement_failures += 1
                continue
            current_unit = _round_half_up(Fraction(amount * baseline["base_quantity"], int(measured)))
            matched_by = (f"标题必要词命中，按标题明确数量归一为 {unit_label(baseline)}"
                          "（不足 1 分按四舍五入计）")
        candidates.append((baseline, current_unit, matched_by))
    if not candidates:
        if measurement_failures:
            return _base_result("unit_unavailable", "维护基准未判定", "已命中商品必要词，但标题中的计价数量不能按该基准无歧义换算。")
        return _base_result("no_match", "尚无匹配维护基准", "没有启用且已生效的品牌、规格和单位基准命中此商品。")
    if len(candidates) > 1:
        return _base_result("ambiguous", "维护基准冲突", "有多条维护基准同时命中；请收紧品牌、型号或规格必要词后再判断。")
    baseline, current_unit, matched_by = candidates[0]
    if current_unit <= baseline["bargain_cents"]:
        state, label, rank = "maintained_bargain", "按维护基准：可捡漏", 5
    elif current_unit <= baseline["low_cents"]:
        state, label, rank = "maintained_low", "按维护基准：优惠", 4
    elif current_unit <= baseline["typical_cents"]:
        state, label, rank = "maintained_below_typical", "按维护基准：低于常规", 3
    elif current_unit <= baseline["high_cents"]:
        state, label, rank = "maintained_regular", "按维护基准：常规价格", 2
    else:
        state, label, rank = "maintained_high", "按维护基准：价格偏高", 1
    return {
        "state": state, "label": label,
        "note": "该结论仅按本工作台维护的价格带计算，不代表账号结算价或全网最低价。",
        "rank": rank, "baseline_id": baseline["id"], "baseline_name": baseline["name"],
        "baseline_version": baseline["version"], "effective_from": baseline["effective_from"],
        "unit_label": unit_label(baseline), "current_unit_cents": current_unit,
        "bargain_cents": baseline["bargain_cents"], "low_cents": baseline["low_cents"],
        "typical_cents": baseline["typical_cents"], "high_cents": baseline["high_cents"],
        "typical_delta_cents": baseline["typical_cents"] - current_unit,
        "matched_by": matched_by,
    }
