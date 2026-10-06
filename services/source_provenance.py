"""Read-only provenance and exact-text overlap audit for preserved source claims."""
import json
import re
import unicodedata
from collections import defaultdict
from urllib.parse import urlparse

from services.benefit_claims import parse_discount_claims


CLAIM_STATES = {"observed", "conditional", "stale"}


def _text_key(value):
    if not isinstance(value, str):
        return ""
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _host(url):
    if not isinstance(url, str):
        return ""
    try:
        parsed = urlparse(url)
    except ValueError:
        return ""
    host = (parsed.hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _safe_https_url(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = urlparse(value)
    except ValueError:
        return None
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password):
        return None
    return value


def _comparison_url(value):
    """Normalize only URL syntax we can compare without guessing identity."""
    if not isinstance(value, str):
        return ""
    try:
        parsed = urlparse(value.strip())
        port = parsed.port
    except ValueError:
        return ""
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        return ""
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if port and not ((parsed.scheme.lower() == "http" and port == 80)
                     or (parsed.scheme.lower() == "https" and port == 443)):
        host = f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    return f"{parsed.scheme.lower()}://{host}{path}?{parsed.query}".rstrip("?")


def _activity_links(metadata_json):
    try:
        metadata = json.loads(metadata_json or "{}")
    except (TypeError, ValueError):
        return ()
    links = metadata.get("activity_links", []) if isinstance(metadata, dict) else []
    if not isinstance(links, list):
        return ()
    return tuple(dict.fromkeys(key for item in links if (key := _comparison_url(item))))


def _discount_signature(text):
    fields = ("threshold_cents", "discount_cents", "threshold_quantity", "face_value_cents",
              "discount_rate_basis_points", "subsidy_rate_basis_points")
    claims = parse_discount_claims(text or "", evidence_type="source_post")
    return tuple(sorted({
        (claim["mechanism"], *(claim.get(field) for field in fields))
        for claim in claims
        if claim.get("mechanism") in {
            "threshold_discount_claim", "quantity_threshold_discount_claim",
            "fixed_reduction_claim", "coupon_fixed_claim", "subsidy_reduction_claim",
            "subsidy_rate_claim", "percentage_reduction_claim", "pay_rate_claim",
        }
    }))


def _search_source_keys(row):
    title = _text_key(row.get("title"))
    snippet = _text_key(row.get("snippet"))
    page = _comparison_url(row.get("event_url") or row.get("url"))
    targets = _activity_links(row.get("metadata_json"))
    amount = row.get("advertised_cents")
    text = row.get("snippet") or row.get("auto_conditions") or ""
    discount = _discount_signature(text) if any(mark in text for mark in ("减", "券", "劵", "折扣", "补贴")) else ()
    keys = []
    if title and snippet:
        keys.append(("same_text", title, snippet))
    if page:
        keys.append(("same_page", page))
    keys.extend(("same_target", target) for target in targets)
    if title and amount is not None and discount:
        keys.append(("same_claim", title, int(amount), discount))
    return keys


def annotate_search_sources(rows, corpus=None):
    """Attach auditable source relationships without merging or dropping rows.

    Exact page/text/target overlap is stronger than a title-price-condition
    signature. Every relationship remains a candidate: no relationship proves
    that two channels copied one another or that their prices are independent.
    ``corpus`` lets a paginated result show matching sources outside its page.
    """
    rows = [dict(row) for row in rows]
    corpus_rows = rows if corpus is None else corpus
    indexes_by_key = defaultdict(dict)
    for row in corpus_rows:
        for key in _search_source_keys(row):
            source_id = row.get("source_id")
            if source_id is not None:
                indexes_by_key[key][source_id] = row

    reason_labels = {
        "same_text": "标题与正文完全相同",
        "same_page": "原文页面链接相同",
        "same_target": "商品/活动目标链接相同",
        "same_claim": "标题、来源金额与优惠声称相同",
    }
    for index, row in enumerate(rows):
        origins = {row.get("source_id"): row} if row.get("source_id") is not None else {}
        reasons = set()
        correlation_keys = set()
        for key in _search_source_keys(row):
            group = indexes_by_key.get(key, {})
            if len(group) > 1:
                for source_id, peer in group.items():
                    if source_id != row.get("source_id"):
                        origins[source_id] = peer
                reasons.add(key[0])
                if key[0] in {"same_text", "same_page", "same_claim"}:
                    correlation_keys.add(json.dumps(key, ensure_ascii=True, separators=(",", ":")))
        ordered = sorted(origins.values(), key=lambda item: (
            item.get("source_id") != row.get("source_id"),
            str(item.get("platform") or ""), str(item.get("source_name") or "")))
        row["source_origins"] = [{
            "source_id": item.get("source_id"),
            "label": (f'{item.get("platform")} · {item.get("source_name")}'
                      if item.get("platform") and item.get("source_name")
                      and item.get("platform") != item.get("source_name")
                      else item.get("platform") or item.get("source_name") or "人工录入"),
            "url": _comparison_url(item.get("event_url") or item.get("url")) or None,
        } for item in ordered]
        row["source_channels"] = list(dict.fromkeys(origin["label"] for origin in row["source_origins"]))
        row["source_count"] = len(origins)
        row["source_provenance_reasons"] = [reason_labels[key] for key in (
            "same_page", "same_text", "same_target", "same_claim") if key in reasons]
        row["source_provenance_label"] = (
            "关联线索；来源独立性未证" if row["source_count"] > 1 else "单一采集入口")
        row["source_correlation_keys"] = sorted(correlation_keys)
        row["source_page_identity_key"] = _comparison_url(row.get("event_url") or row.get("url"))
    return rows


def _group(rows):
    grouped = defaultdict(list)
    for row in rows:
        key = (_text_key(row["title"]), _text_key(row["snippet"]))
        if key != ("", ""):
            grouped[key].append(row)
    return [items for items in grouped.values()
            if len({row["source_id"] for row in items}) > 1]


def _record(row):
    try:
        metadata = json.loads(row["metadata_json"] or "{}")
    except (TypeError, ValueError):
        metadata = {}
    links = metadata.get("activity_links", []) if isinstance(metadata, dict) else []
    if not isinstance(links, list):
        links = []
    safe_urls = list(dict.fromkeys(url for item in links
                                   if (url := _safe_https_url(item))))
    cents = row["advertised_cents"]
    price = None if cents is None else f"¥{cents // 100}.{cents % 100:02d}"
    return {
        "event_id": row["event_id"],
        "source_id": row["source_id"],
        "collector": f'{row["platform"]} · {row["source_name"]}',
        "method": row["method"],
        "collector_url": row["collector_url"],
        "content_url": _safe_https_url(row["content_url"]),
        "content_host": _host(row["content_url"]) or "未知",
        "outbound_links": [{"url": url, "host": _host(url) or "外部链接"}
                           for url in safe_urls],
        "published_at": row["published_at"],
        "first_seen_at": row["observed_at"],
        "last_seen_at": row["last_seen_at"],
        "state": row["state"],
        "price": price,
        "specification": row["specification"] or "未提取",
        "conditions": row["conditions"] or "未提取",
        "parse_evidence": (row["evidence"] or "未保留解析依据")[:500],
    }


def source_provenance_evidence(db):
    """Return an auditable snapshot; text overlap is never treated as proof.

    The source URL is the configured acquisition route. ``events.url`` is the
    stored source item URL, not automatically a merchant product page. Links
    extracted from the body are shown as destinations to inspect, not verified
    products. No rows are written, joined, hidden, or deleted by this report.
    """
    as_of = db.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0]
    rows = db.execute("""SELECT e.id AS event_id,e.source_id,e.title,e.url AS content_url,
        e.snippet,e.metadata_json,e.published_at,e.observed_at,e.last_seen_at,
        s.platform,s.name AS source_name,s.method,s.url AS collector_url,
        a.state,a.advertised_cents,a.specification,a.conditions,a.evidence
        FROM events e JOIN sources s ON s.id=e.source_id
        JOIN opportunities o ON o.event_id=e.id
        LEFT JOIN auto_reviews a ON a.opportunity_id=o.id
        WHERE e.published_at IS NOT NULL AND e.published_at<=?
        ORDER BY e.published_at DESC,e.id DESC""", (as_of,)).fetchall()

    channel_groups = _group(rows)
    differently_labelled = [group for group in channel_groups
                            if len({row["platform"] for row in group}) > 1]
    eligible = [row for row in rows if row["advertised_cents"] is not None
                and row["state"] in CLAIM_STATES]
    claim_groups = _group(eligible)
    detailed_groups = []
    for group in sorted(claim_groups,
                        key=lambda items: (-len(items), _text_key(items[0]["title"]))):
        records = [_record(row) for row in group]
        detailed_groups.append({
            "title": group[0]["title"],
            "record_count": len(records),
            "collector_count": len({row["source_id"] for row in group}),
            "collectors": sorted({f'{row["platform"]} · {row["source_name"]}' for row in group}),
            "content_hosts": sorted({_host(row["content_url"]) or "未知" for row in group}),
            "records": records,
            "label": "疑似同文；不证明转载、同一商品或独立报价",
        })

    host_counts = defaultdict(int)
    for row in eligible:
        host_counts[_host(row["content_url"]) or "未知"] += 1
    host_coverage = [dict(host=host, claim_rows=count)
                     for host, count in sorted(host_counts.items(),
                                              key=lambda item: (-item[1], item[0]))]
    return {
        "as_of": as_of,
        "published_event_rows": len(rows),
        "cross_collector_text_groups": len(channel_groups),
        "cross_collector_text_rows": sum(map(len, channel_groups)),
        "cross_platform_label_groups": len(differently_labelled),
        "cross_platform_label_rows": sum(map(len, differently_labelled)),
        "eligible_claim_rows": len(eligible),
        "eligible_overlap_groups": len(claim_groups),
        "eligible_overlap_rows": sum(map(len, claim_groups)),
        "eligible_overlap_same_host_groups": sum(
            len({_host(row["content_url"]) or "未知" for row in group}) == 1
            for group in claim_groups),
        "content_host_coverage": host_coverage,
        "overlap_groups": detailed_groups,
    }
