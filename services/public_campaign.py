"""Parse explicit public campaign prices and date windows from source text.

This records what a source page states. It does not imply stock, account eligibility,
checkout price, or that the claimed reference price is a market benchmark.
"""
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation


CHINA = timezone(timedelta(hours=8))
UTC = timezone.utc
MAX_UPCOMING_DAYS = 30

_DATE = (
    r"(?P<year>20\d{2})\s*(?:年|[-/])\s*(?P<month>\d{1,2})\s*(?:月|[-/])\s*"
    r"(?P<day>\d{1,2})\s*日?(?:\s*(?P<hour>\d{1,2}):(?P<minute>\d{2})"
    r"(?::(?P<second>\d{2}))?)?"
)
_START = re.compile(r"(?P<label>众筹|活动)\s*(?:开始|开启|开售)\s*[:：]?\s*" + _DATE)
_END = re.compile(r"(?P<label>众筹|活动)\s*(?:结束|截止)\s*[:：]?\s*" + _DATE)
_PRICE = re.compile(r"众筹价\s*[:：]?\s*[¥￥]?\s*(\d+(?:\.\d{1,2})?)\s*元?")
_RETAIL = re.compile(r"建议零售价\s*[:：]?\s*[¥￥]?\s*(\d+(?:\.\d{1,2})?)\s*元?")


def _china_time(match, end=False):
    values = match.groupdict()
    hour = int(values.get("hour") or (23 if end else 0))
    minute = int(values.get("minute") or (59 if end else 0))
    second = int(values.get("second") or (59 if end else 0))
    try:
        value = datetime(int(values["year"]), int(values["month"]), int(values["day"]),
                         hour, minute, second, tzinfo=CHINA)
    except (TypeError, ValueError):
        return None
    return value.astimezone(UTC).replace(tzinfo=None)


def parse_campaign_window(text):
    """Return a window only when one explicit matching start/end pair exists."""
    text = unicodedata.normalize("NFKC", text or "")
    starts, ends = list(_START.finditer(text)), list(_END.finditer(text))
    start_values={(m.group('label'),_china_time(m)) for m in starts}
    end_values={(m.group('label'),_china_time(m,end=True)) for m in ends}
    if len(start_values)!=1 or len(end_values)!=1:
        return None
    start_label,start_at=next(iter(start_values))
    end_label,end_at=next(iter(end_values))
    if not start_at or not end_at or start_label!=end_label or end_at<start_at:
        return None
    return {"start_at": start_at, "end_at": end_at, "label": start_label}


def parse_public_campaign(text):
    """Extract a single explicitly labeled crowdfunding price plus its campaign window.

    The normal purchase tier is deliberately parsed independently from any
    separate “1元支持抽奖” tier in the same article.
    """
    text = unicodedata.normalize("NFKC", text or "")
    prices = list(_PRICE.finditer(text))
    window = parse_campaign_window(text)
    if not prices or not window:
        return None
    try:
        price_values={int(Decimal(match.group(1))*100) for match in prices}
    except (InvalidOperation, ValueError):
        return None
    if len(price_values)!=1:
        return None
    cents=next(iter(price_values))
    if cents <= 0:
        return None
    retail_values = set()
    for match in _RETAIL.finditer(text):
        try:
            retail_values.add(int(Decimal(match.group(1)) * 100))
        except (InvalidOperation, ValueError):
            continue
    return {"price_cents": cents, "suggested_retail_cents": next(iter(retail_values))
            if len(retail_values) == 1 else None, **window}


def campaign_state(campaign, now, max_upcoming_days=MAX_UPCOMING_DAYS):
    """Classify a source-stated campaign window using UTC-naive review timestamps."""
    if not campaign:
        return "unknown"
    if campaign["end_at"] < now:
        return "expired"
    if campaign["start_at"] > now:
        if campaign["start_at"] - now <= timedelta(days=max_upcoming_days):
            return "upcoming"
        return "outside_horizon"
    return "active"


def china_label(value):
    if not isinstance(value, datetime):
        return ""
    return value.replace(tzinfo=UTC).astimezone(CHINA).strftime("%Y-%m-%d %H:%M")
