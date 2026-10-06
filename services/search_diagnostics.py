"""Non-destructive query funnel summaries for current product search."""
from collections import Counter


_STATE_EXCLUSIONS = {"stale", "source_unavailable", "retry", "excluded"}


def summarize_search_funnel(matched_rows, current_rows):
    """Explain current-view reduction for the same database-backed query.

    ``matched_rows`` are the user's normal SQL filters before freshness gates;
    ``current_rows`` are the rows that passed the existing current-view query
    and live topic classifier. This reports gate outcomes without changing them.
    """
    states = Counter((row.get("auto_state") or "queued") for row in matched_rows)
    matched = len(matched_rows)
    current = len(current_rows)
    stale = states["stale"]
    unavailable = states["source_unavailable"]
    retry = states["retry"]
    excluded = states["excluded"]
    state_excluded = stale + unavailable + retry + excluded
    current_quotes = sum(bool(row.get("display_quote")) for row in current_rows)
    current_ids = {row.get("id") for row in current_rows}
    return {
        "matched_count": matched,
        "current_count": current,
        "current_quote_count": current_quotes,
        "current_unpriced_count": max(0, current - current_quotes),
        "stale_count": stale,
        "source_unavailable_count": unavailable,
        "retry_count": retry,
        "excluded_state_count": excluded,
        "other_freshness_count": max(0, matched - state_excluded - len(current_ids)),
    }
