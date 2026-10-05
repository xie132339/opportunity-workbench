"""Read-only provenance and exact-text overlap audit for preserved source claims."""
import json
import unicodedata
from collections import defaultdict
from urllib.parse import urlparse


CLAIM_STATES = {"observed", "conditional", "stale"}


def _text_key(value):
    return " ".join(unicodedata.normalize("NFKC", value or "").split()).casefold()


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
