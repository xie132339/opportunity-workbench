"""Fail-closed, category-configured product identity and unit normalization."""
import json
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from services.category_policy import match_category

RULE_VERSION = 1
IDENTITY_MODES = {"merchant_or_exact_title", "merchant_id_only"}
UNIT_MODES = {"exact_spec", "mass", "volume", "count"}
COUNT_UNITS = {"抽", "卷", "片", "个", "件", "包", "瓶", "盒", "袋", "张", "支", "罐", "提", "箱"}
_UNITS = {
    "mass": {"g": Decimal(1), "克": Decimal(1), "kg": Decimal(1000), "千克": Decimal(1000), "斤": Decimal(500)},
    "volume": {"ml": Decimal(1), "毫升": Decimal(1), "l": Decimal(1000), "升": Decimal(1000)},
}
_PACK_UNITS = {"包", "袋", "瓶", "盒", "卷", "提", "箱", "组", "件", "个", "罐", "支"}


def default_rule():
    return {"schema_version": RULE_VERSION, "identity_mode": "merchant_or_exact_title",
            "unit_mode": "exact_spec", "count_unit": ""}


def validate_rule(value):
    if not isinstance(value, dict) or set(value) != {"schema_version", "identity_mode", "unit_mode", "count_unit"}:
        raise ValueError("商品身份/计价规则结构不完整")
    if value["schema_version"] != RULE_VERSION:
        raise ValueError("商品身份/计价规则版本不受支持")
    if value["identity_mode"] not in IDENTITY_MODES:
        raise ValueError("商品身份模式无效")
    if value["unit_mode"] not in UNIT_MODES:
        raise ValueError("计价单位模式无效")
    count_unit = value["count_unit"]
    if not isinstance(count_unit, str) or (value["unit_mode"] == "count" and count_unit not in COUNT_UNITS):
        raise ValueError("明确计数单位必须从支持列表中选择")
    if value["unit_mode"] != "count" and count_unit:
        raise ValueError("非计数模式不得设置计数单位")
    return value


def encode_rule(value):
    return json.dumps(validate_rule(value), ensure_ascii=False, sort_keys=True)


def decode_rule(raw):
    if not raw:
        return default_rule(), None
    try:
        return validate_rule(json.loads(raw)), None
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        return default_rule(), str(exc)


def resolve_category_rule(title, topic, categories):
    """Read the machine rule from the same unique leaf-category match as policy."""
    category = match_category(title, topic, categories)
    if category is None:
        return default_rule(), None, None
    rule, error = decode_rule(category.get("comparison_rule_json"))
    return rule, category.get("name"), error


def _token_pattern(units):
    choices = "|".join(re.escape(unit) for unit in sorted(units, key=len, reverse=True))
    # Chinese product titles commonly attach units directly to CJK words,
    # e.g. "抽纸100抽6包". Guard numeric/Latin boundaries without requiring
    # whitespace or an ASCII word boundary before the number.
    return re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*(" + choices + r")(?![A-Za-z])", re.I)


def _measurement(text, rule):
    mode = rule["unit_mode"]
    if mode == "exact_spec":
        return None
    if (not text or re.search(r"[-~～至+]", text)
            or re.search(r"任选|多规格|多款|随机|任选|待选择|请选择", text)):
        return None
    if mode == "count":
        units = {rule["count_unit"]: Decimal(1)}
        canonical = rule["count_unit"]
    else:
        units = _UNITS[mode]
        canonical = "g" if mode == "mass" else "ml"
    matches = list(_token_pattern(units).finditer(unicodedata.normalize("NFKC", text)))
    if len(matches) != 1:
        return None
    value = Decimal(matches[0].group(1)) * units[matches[0].group(2).lower()]
    remainder = text[:matches[0].start()] + " " + text[matches[0].end():]
    # Multiply only by explicit package/count tokens in the selected spec.
    package_pattern = _token_pattern(_PACK_UNITS)
    packages = list(package_pattern.finditer(remainder))
    for match in packages:
        try:
            value *= Decimal(match.group(1))
        except InvalidOperation:
            return None
    remainder = package_pattern.sub(" ", remainder)
    if value <= 0:
        return None
    return value, canonical


def normalized_spec(title, selected_spec, rule):
    """Return strict residual variant, normalized amount, and base unit.

    Only removes configured measure tokens and explicit package multipliers. All
    other title/spec tokens stay identity evidence; this is not fuzzy matching.
    """
    measured = _measurement(selected_spec, rule)
    if rule["unit_mode"] == "exact_spec":
        return (re.sub(r"[\s，,；;]+", "", selected_spec or "").casefold(), None, None, None)
    if measured is None:
        return "", None, None, "选中规格不能按已配置单位无歧义换算"
    title_measure = _measurement(product_identity_text(title), rule)
    if title_measure is not None and title_measure[0] != measured[0]:
        return "", None, None, "标题计价规格与选中报价规格冲突"
    mode = rule["unit_mode"]
    units = ({rule["count_unit"]: Decimal(1)} if mode == "count" else _UNITS[mode])
    target_pattern = _token_pattern(units)
    package_pattern = _token_pattern(_PACK_UNITS)
    residual_title = target_pattern.sub(" ", unicodedata.normalize("NFKC", product_identity_text(title)))
    residual_title = package_pattern.sub(" ", residual_title)
    residual_spec = target_pattern.sub(" ", unicodedata.normalize("NFKC", selected_spec or ""))
    residual_spec = package_pattern.sub(" ", residual_spec)
    clean = lambda value: re.sub(r"[\s×xX*·，,、；;:：/]+", "", value).casefold()
    title_identity = clean(residual_title)
    spec_variant = clean(residual_spec)
    if not title_identity:
        return "", None, None, "计价归一后缺少商品身份文本"
    return title_identity, spec_variant, measured[0], None


def product_identity_text(title):
    """Remove only a trailing explicit source price before building title identity."""
    text = unicodedata.normalize("NFKC", title or "").lower()
    return re.sub(r"\s+(?:券后)?\d+(?:\.\d+)?元.*$", "", text)
