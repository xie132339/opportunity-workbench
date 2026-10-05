"""Read-only, freshness-bounded evidence yield metrics for configured sources."""


def current_source_yield(db):
    """Count current source records by evidence stage without implying verification.

    The window is based on a recently successful enabled source and recently
    seen events. Original publication time is measured as its own evidence
    stage, so missing/old/future publication timestamps remain visible.
    """
    rows = db.execute("""SELECT s.id AS source_id,
        COUNT(o.id) AS reviewed_count,
        SUM(CASE WHEN a.state IN ('observed','conditional') THEN 1 ELSE 0 END) AS quote_extracted_count,
        SUM(CASE WHEN a.state='conditional' THEN 1 ELSE 0 END) AS conditional_count,
        SUM(CASE WHEN a.state='missing_price' THEN 1 ELSE 0 END) AS missing_price_count,
        SUM(CASE WHEN a.state='missing_time' THEN 1 ELSE 0 END) AS missing_time_count,
        SUM(CASE WHEN a.state='stale' THEN 1 ELSE 0 END) AS stale_count,
        SUM(CASE WHEN a.state='activity' THEN 1 ELSE 0 END) AS activity_count,
        SUM(CASE WHEN a.state='conflict' THEN 1 ELSE 0 END) AS conflict_count,
        SUM(CASE WHEN a.state='excluded' THEN 1 ELSE 0 END) AS excluded_count,
        SUM(CASE WHEN a.state='queued' THEN 1 ELSE 0 END) AS queued_count,
        SUM(CASE WHEN a.state='retry' THEN 1 ELSE 0 END) AS retry_count,
        SUM(CASE WHEN a.state='source_unavailable' THEN 1 ELSE 0 END) AS source_unavailable_count,
        SUM(CASE WHEN a.opportunity_id IS NULL THEN 1 ELSE 0 END) AS not_reviewed_count,
        SUM(CASE WHEN a.opportunity_id IS NOT NULL AND a.checked_at<e.last_seen_at THEN 1 ELSE 0 END) AS review_behind_count
        FROM sources s
        JOIN events e ON e.source_id=s.id
        LEFT JOIN opportunities o ON o.event_id=e.id
        LEFT JOIN auto_reviews a ON a.opportunity_id=o.id
        WHERE s.enabled=1 AND s.status='healthy'
          AND s.last_success>=datetime('now','-' || MIN(2*s.interval_minutes+15,120) || ' minutes')
          AND e.last_seen_at>=datetime('now','-' || MIN(2*s.interval_minutes+15,120) || ' minutes')
        GROUP BY s.id""").fetchall()
    return {row['source_id']: dict(row) for row in rows}
