"""Non-destructive query funnel summaries for current product search."""
from collections import Counter


_STATE_EXCLUSIONS = {"stale", "source_unavailable", "retry", "excluded"}


def summarize_search_funnel(matched_rows, current_rows):
    """Explain current-view reduction for the same database-backed query.

    ``matched_rows`` are the user's normal SQL filters before freshness gates;
    ``current_rows`` are the rows that passed the existing current-view query
    and live topic classifier. This reports gate outcomes without changing them.
    """
    states = Counter()
    matched = 0
    for row in matched_rows:
        states[row.get("auto_state") or "queued"] += 1
        matched += 1
    current = len(current_rows)
    stale = states["stale"]
    unavailable = states["source_unavailable"]
    retry = states["retry"]
    excluded = states["excluded"]
    state_excluded = stale + unavailable + retry + excluded
    current_quotes = sum(bool(row.get("display_quote"))
                         and row['display_quote'].get('kind') == 'source_claim' for row in current_rows)
    promotion_candidates = sum(bool(row.get('promotional_candidate')) for row in current_rows)
    catalog_references = sum(bool(row.get('catalog_reference')) for row in current_rows)
    plain_price_claims = sum(bool(row.get('display_quote'))
                              and row['display_quote'].get('kind') == 'source_claim'
                              and not row.get('promotional_candidate') for row in current_rows)
    current_ids = {row.get("id") for row in current_rows}
    return {
        "matched_count": matched,
        "current_count": current,
        "current_quote_count": current_quotes,
        "current_promotion_candidate_count": promotion_candidates,
        "current_catalog_reference_count": catalog_references,
        "current_plain_price_claim_count": plain_price_claims,
        "current_unpriced_count": max(0, current - current_quotes - catalog_references),
        "stale_count": stale,
        "source_unavailable_count": unavailable,
        "retry_count": retry,
        "excluded_state_count": excluded,
        "other_freshness_count": max(0, matched - state_excluded - len(current_ids)),
    }
