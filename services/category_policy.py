"""Versioned, data-driven comparison policy shared by product categories.

This policy can tighten source-claim comparison. It never promotes a source claim
into merchant or account checkout evidence; benchmark gates stay disabled until
an explicit reference basis and calibrated policy are configured.
"""
import json
import unicodedata

POLICY_VERSION = 1
PRICE_BASES = {"merchant_public", "independent_index"}


def default_policy():
    return {
        "schema_version": POLICY_VERSION,
        "max_source_age_minutes": 120,
        "minimum_comparable_offers": 2,
        "reference": {
            "enabled": False,
            "price_basis": "merchant_public",
            "window_days": None,
            "minimum_independent_sources": None,
            "below_reference_percent": None,
        },
    }


def validate_policy(value):
    if not isinstance(value, dict) or set(value) != {
        "schema_version", "max_source_age_minutes", "minimum_comparable_offers", "reference"
    }:
        raise ValueError("规则结构不完整或包含不支持的字段")
    if value["schema_version"] != POLICY_VERSION:
        raise ValueError("规则版本不受支持")
    freshness = value["max_source_age_minutes"]
    peers = value["minimum_comparable_offers"]
    if isinstance(freshness, bool) or not isinstance(freshness, int) or not 1 <= freshness <= 120:
        raise ValueError("来源最大时效须为 1 到 120 分钟；配置只能收紧现有采集时效")
    if isinstance(peers, bool) or not isinstance(peers, int) or not 2 <= peers <= 20:
        raise ValueError("同口径候选数须为 2 到 20")
    reference = value["reference"]
    if not isinstance(reference, dict) or set(reference) != {
        "enabled", "price_basis", "window_days", "minimum_independent_sources", "below_reference_percent"
    }:
        raise ValueError("参考价规则字段不完整")
    if not isinstance(reference["enabled"], bool):
        raise ValueError("参考价启用状态无效")
    if reference["enabled"]:
        raise ValueError("独立行情基准计算器尚未接入，当前不能启用基准降幅判断")
    if reference["price_basis"] not in PRICE_BASES:
        raise ValueError("参考价只能使用商家公开价或独立行情索引，不能用来源线报声称价")
    if any(reference[key] is not None for key in (
        "window_days", "minimum_independent_sources", "below_reference_percent"
    )):
        raise ValueError("参考价计算器未接入，观察窗口、独立来源数和阈值必须留空")
    return value


def decode_policy(raw):
    """Return the validated persisted policy, or a fail-closed default plus error."""
    if not raw:
        return default_policy(), None
    try:
        value = validate_policy(json.loads(raw))
        return value, None
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        return default_policy(), str(exc)


def encode_policy(value):
    return json.dumps(validate_policy(value), ensure_ascii=False, sort_keys=True)


def match_category(title, topic, categories):
    """Pick a unique most-specific leaf category; ambiguous matches fall back safely."""
    normalized = unicodedata.normalize("NFKC", title or "").casefold()
    matches = []
    for category in categories:
        if not category.get("enabled", 1) or category.get("topic_key") != (topic or "other"):
            continue
        try:
            terms = json.loads(category.get("match_terms_json") or "[]")
        except (TypeError, ValueError):
            continue
        if not isinstance(terms, list) or not all(isinstance(term, str) for term in terms):
            continue
        found = [unicodedata.normalize("NFKC", term).casefold() for term in terms
                 if term and unicodedata.normalize("NFKC", term).casefold() in normalized]
        if found:
            matches.append((max(map(len, found)), len(set(found)), category))
    if not matches:
        return None
    best_specificity = max(item[:2] for item in matches)
    best = [category for length, count, category in matches
            if (length, count) == best_specificity]
    # Do not let row order or IDs silently decide between equally specific rules.
    return best[0] if len(best) == 1 else None


def resolve_category_policy(title, topic, categories, topic_policy_raw=None):
    """Use a configured leaf policy when keywords match, otherwise the broad-topic rule."""
    category = match_category(title, topic, categories)
    if category:
        policy, error = decode_policy(category.get("policy_json"))
        return policy, category.get("name"), error, "category"
    policy, error = decode_policy(topic_policy_raw)
    return policy, None, error, "topic"
