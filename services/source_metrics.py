"""Read-only, freshness-bounded evidence yield metrics for configured sources."""
from datetime import datetime, timezone

from autoreview import classify_catalog_observation


def current_source_yield(db):
    """Count current source records by evidence stage without implying verification.

    The window is based on a recently successful enabled source and recently
    seen events. Original publication time is measured as its own evidence
    stage, so missing/old/future publication timestamps remain visible.
    """
    rows = db.execute("""SELECT s.id AS source_id,
        COUNT(o.id) AS reviewed_count,
        SUM(CASE WHEN a.state IN ('observed','conditional') THEN 1 ELSE 0 END) AS quote_extracted_count,
        SUM(CASE WHEN s.parser IN ('apple','mi','suning','lenovo','honor','kongfz') THEN 1 ELSE 0 END) AS catalog_current_count,
        SUM(CASE WHEN s.parser IN ('apple','mi','suning','lenovo','honor','kongfz')
                  AND a.state IN ('observed','conditional') THEN 1 ELSE 0 END) AS catalog_price_count,
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
    metrics = {row['source_id']: dict(row) for row in rows}
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    catalog_rows = db.execute("""SELECT o.*,e.snippet,e.metadata_json,e.published_at,e.last_seen_at,
        s.id AS source_id,s.parser AS source_parser,s.method AS source_method,
        s.enabled,s.status AS source_status,s.interval_minutes,s.last_success,
        a.state AS stored_state,a.detail_json,a.detail_checked_at,a.detail_error,
        EXISTS(SELECT 1 FROM opportunities newer WHERE newer.source_id=o.source_id
               AND newer.url=o.url AND newer.id>o.id) AS duplicate
        FROM opportunities o JOIN events e ON e.id=o.event_id
        JOIN sources s ON s.id=o.source_id LEFT JOIN auto_reviews a ON a.opportunity_id=o.id
        WHERE s.parser IN ('apple','mi','suning','lenovo','honor','kongfz')
          AND s.enabled=1 AND s.status='healthy'
          AND s.last_success>=datetime('now','-' || MIN(2*s.interval_minutes+15,120) || ' minutes')
          AND e.last_seen_at>=datetime('now','-' || MIN(2*s.interval_minutes+15,120) || ' minutes')""").fetchall()
    catalog_states = {}
    for row in catalog_rows:
        result = classify_catalog_observation(dict(row), now, bool(row['duplicate']))
        state = result['state']
        source_metrics = catalog_states.setdefault(row['source_id'], {
            'quote_extracted_count': 0, 'catalog_price_count': 0,
            'conditional_count': 0, 'missing_price_count': 0,
            'missing_time_count': 0, 'stale_count': 0, 'activity_count': 0,
            'conflict_count': 0, 'excluded_count': 0, 'queued_count': 0,
            'retry_count': 0, 'source_unavailable_count': 0,
            'catalog_recomputed_count': 0,
        })
        state_fields = {
            'conditional': 'conditional_count', 'missing_price': 'missing_price_count',
            'missing_time': 'missing_time_count', 'stale': 'stale_count',
            'activity': 'activity_count', 'conflict': 'conflict_count',
            'excluded': 'excluded_count', 'queued': 'queued_count',
            'retry': 'retry_count', 'source_unavailable': 'source_unavailable_count',
        }
        if state in ('observed', 'conditional'):
            source_metrics['quote_extracted_count'] += 1
        if state == 'observed' and result['advertised_cents'] is not None:
            source_metrics['catalog_price_count'] += 1
        if state in state_fields:
            source_metrics[state_fields[state]] += 1
        if row['stored_state'] != state:
            source_metrics['catalog_recomputed_count'] += 1
    for source_id, values in catalog_states.items():
        source_metrics = metrics.setdefault(source_id, {'source_id': source_id})
        source_metrics.update(values)
        source_metrics['catalog_current_count'] = sum(1 for row in catalog_rows if row['source_id'] == source_id)
    return metrics
