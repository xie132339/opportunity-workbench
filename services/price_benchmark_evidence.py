"""Read-only coverage for prices claimed by the workbench's existing public sources."""

TOPIC_LABELS = {
    "home": "家居日用（主题识别）", "food": "食品餐饮（主题识别）",
    "baby": "母婴（主题识别）", "pet": "宠物（主题识别）",
    "beauty": "美妆个护（主题识别）", "digital": "数字权益（主题识别）",
    "electronics": "数码家电（主题识别）", "travel": "出行（主题识别）",
    "other": "其他（主题识别）",
}


def price_claim_evidence(db):
    """Summarize preserved source claims; never calculate market price bands here.

    Event rows are source snapshots, not independent sellers or checkout receipts.
    Only records with an explicit parsed amount, source publication time, and a
    non-conflict/non-excluded review state enter this diagnostic.
    """
    source_counts = db.execute("""SELECT COUNT(*) AS total,
        SUM(CASE WHEN enabled=1 THEN 1 ELSE 0 END) AS enabled,
        SUM(CASE WHEN enabled=1 AND status='healthy' THEN 1 ELSE 0 END) AS healthy,
        MAX(CASE WHEN enabled=1 THEN last_success END) AS latest_enabled_success
        FROM sources""").fetchone()
    claim_rows = db.execute("""SELECT o.topic,a.state,e.published_at,s.platform
        FROM auto_reviews a JOIN opportunities o ON o.id=a.opportunity_id
        JOIN events e ON e.id=o.event_id JOIN sources s ON s.id=o.source_id
        WHERE a.advertised_cents IS NOT NULL AND e.published_at IS NOT NULL
          AND a.state IN ('observed','conditional','stale')
          AND e.published_at <= CURRENT_TIMESTAMP""").fetchall()
    future_excluded_rows = db.execute("""SELECT COUNT(*)
        FROM auto_reviews a JOIN opportunities o ON o.id=a.opportunity_id
        JOIN events e ON e.id=o.event_id
        WHERE a.advertised_cents IS NOT NULL AND e.published_at > CURRENT_TIMESTAMP""").fetchone()[0]
    platform_rows = {}
    for source in db.execute("""SELECT platform,COUNT(*) AS configured_sources,
        SUM(CASE WHEN enabled=1 THEN 1 ELSE 0 END) AS enabled_sources,
        SUM(CASE WHEN enabled=1 AND status='healthy' THEN 1 ELSE 0 END) AS healthy_sources
        FROM sources GROUP BY platform"""):
        name = source["platform"] or "未知入口"
        platform_rows[name] = {
            "platform": name, "configured_sources": source["configured_sources"],
            "enabled_sources": source["enabled_sources"] or 0,
            "healthy_sources": source["healthy_sources"] or 0,
            "claim_rows": 0, "observed": 0, "conditional": 0, "stale": 0,
            "days": set(),
        }
    buckets = {}
    states = {"observed": 0, "conditional": 0, "stale": 0}
    days = set()
    platforms = set()
    for row in claim_rows:
        topic = row["topic"] or "other"
        bucket = buckets.setdefault(topic, {
            "topic": topic, "label": TOPIC_LABELS.get(topic, "其他（主题识别）"),
            "claim_rows": 0, "observed": 0, "conditional": 0, "stale": 0,
            "platforms": set(), "days": set(),
        })
        state = row["state"]
        bucket["claim_rows"] += 1
        bucket[state] += 1
        states[state] += 1
        platform = row["platform"] or "未知入口"
        bucket["platforms"].add(platform)
        platforms.add(platform)
        platform_bucket = platform_rows.setdefault(platform, {
            "platform": platform, "configured_sources": 0, "enabled_sources": 0,
            "healthy_sources": 0, "claim_rows": 0, "observed": 0,
            "conditional": 0, "stale": 0, "days": set(),
        })
        platform_bucket["claim_rows"] += 1
        platform_bucket[state] += 1
        day = row["published_at"][:10]
        platform_bucket["days"].add(day)
        bucket["days"].add(day)
        days.add(day)
    topics = []
    for bucket in buckets.values():
        bucket["platform_count"] = len(bucket.pop("platforms"))
        bucket["published_day_count"] = len(bucket.pop("days"))
        topics.append(bucket)
    topics.sort(key=lambda item: (-item["claim_rows"], item["label"]))
    platform_coverage = []
    for bucket in platform_rows.values():
        bucket["published_day_count"] = len(bucket.pop("days"))
        platform_coverage.append(bucket)
    platform_coverage.sort(key=lambda item: (-int(item["enabled_sources"] > 0), -item["claim_rows"], item["platform"]))
    quote_count = db.execute("SELECT COUNT(*) FROM quotes").fetchone()[0]
    return {
        "configured_sources": source_counts["total"] or 0,
        "enabled_sources": source_counts["enabled"] or 0,
        "healthy_enabled_sources": source_counts["healthy"] or 0,
        "latest_enabled_success": source_counts["latest_enabled_success"],
        "future_excluded_rows": future_excluded_rows,
        "claim_rows": len(claim_rows), "observed_rows": states["observed"],
        "conditional_rows": states["conditional"], "stale_rows": states["stale"],
        "platform_count": len(platforms), "published_day_count": len(days),
        "external_quote_rows": quote_count,
        "topics": topics,
        "platforms": platform_coverage,
    }
