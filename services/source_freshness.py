"""Shared freshness rules for post feeds and live merchant catalogue pages."""
from datetime import datetime, timedelta, timezone


CATALOG_LISTING_PARSERS = frozenset({"apple", "mi", "suning", "lenovo", "honor", "kongfz"})


def is_catalog_listing(row):
    return row.get("source_parser") in CATALOG_LISTING_PARSERS


def moment(value):
    try:
        parsed = datetime.fromisoformat(value or "")
        return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed
    except (ValueError, TypeError):
        return None


def source_snapshot_is_current(row, now=None):
    """A current catalogue listing is timestamped by successful observation, not post time."""
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    ttl = timedelta(minutes=min(2 * (row.get("interval_minutes") or 60) + 15, 120))
    seen, success = moment(row.get("last_seen_at")), moment(row.get("last_success"))
    return bool(
        is_catalog_listing(row)
        and row.get("enabled")
        and row.get("source_status") == "healthy"
        and seen and success
        and timedelta(0) <= now - seen <= ttl
        and timedelta(0) <= now - success <= ttl
    )


def publication_is_current(row, now=None):
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    published = moment(row.get("published_at"))
    if published and timedelta(0) <= now - published <= timedelta(hours=2):
        return True
    return source_snapshot_is_current(row, now)
