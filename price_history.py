"""Conservative historical-price classification for one exact merchant product URL."""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from statistics import median

WINDOW_DAYS = 90
MIN_OBSERVATION_DAYS = 30


def _utc(value):
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value or '').replace('Z', '+00:00'))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _in_stock(value):
    state = str(value or '').rstrip('/').rsplit('/', 1)[-1].casefold()
    return state in ('instock', 'limitedavailability')


def assess(current, observations, now=None):
    """Return a historical *public list-price* signal, never an account-savings claim.

    `current` needs price_cents, currency, checked_at and next_check_at. Each history
    observation needs price_cents, currency and checked_at. Repeated reads on a day
    are reduced to that day's median so a high polling rate cannot inflate sample size.
    """
    now = _utc(now or datetime.now(timezone.utc))
    if not current or current.get('price_cents') is None:
        return dict(state='no_current_price', reason='没有当前可读取的商家公开标价；来源帖子价不能代替商家页价格',
                    sample_days=0, current_cents=None, median_cents=None, p10_cents=None, reference_delta_cents=None)
    checked = _utc(current.get('checked_at'))
    valid_until = _utc(current.get('next_check_at'))
    if (current.get('state') != 'public_price_observed' or current.get('currency') not in ('CNY', 'RMB', 'CNH')
            or not checked or not valid_until or checked > now or now >= valid_until):
        return dict(state='stale_current_price', reason='商家公开页价格已过期、币种不符或当前读取状态无效',
                    sample_days=0, current_cents=None, median_cents=None, p10_cents=None, reference_delta_cents=None)
    if not _in_stock(current.get('availability')):
        return dict(state='availability_unconfirmed', reason='商家公开页没有明确显示当前有货；不能判为可执行低价',
                    sample_days=0, current_cents=int(current['price_cents']), median_cents=None,
                    p10_cents=None, reference_delta_cents=None)

    cutoff = now - timedelta(days=WINDOW_DAYS)
    by_day = defaultdict(list)
    for item in observations:
        observed = _utc(item.get('checked_at'))
        amount = item.get('price_cents')
        if (not observed or observed < cutoff or observed >= checked or observed > now
                or amount is None or item.get('currency') not in ('CNY', 'RMB', 'CNH')
                or not _in_stock(item.get('availability'))):
            continue
        by_day[observed.date().isoformat()].append(int(amount))
    daily = sorted(int(median(values)) for values in by_day.values())
    sample_days = len(daily)
    base = dict(sample_days=sample_days, current_cents=int(current['price_cents']),
                median_cents=None, p10_cents=None, reference_delta_cents=None)
    if sample_days < MIN_OBSERVATION_DAYS:
        base.update(state='collecting_history',
                    reason=f'近{WINDOW_DAYS}天只有{sample_days}个有效观察日，至少需要{MIN_OBSERVATION_DAYS}天；当前价不是捡漏结论')
        return base

    middle = int(median(daily))
    # Nearest-rank P10, deterministic for small and even-sized samples.
    p10 = daily[max(0, (sample_days + 9) // 10 - 1)]
    current_cents = int(current['price_cents'])
    low = current_cents <= p10 and current_cents < middle
    base.update(median_cents=middle, p10_cents=p10,
                reference_delta_cents=max(0, middle - current_cents),
                state='historical_low_candidate' if low else 'not_historically_low',
                reason=(f'当前公开标价不高于近{WINDOW_DAYS}天同商品日价P10，且低于中位价；属于历史低价候选'
                        if low else f'当前公开标价未同时达到“≤近{WINDOW_DAYS}天P10且低于中位价”'))
    return base


def for_product(db, product_url, current, now=None):
    if not product_url:
        return assess(None, (), now)
    instant = _utc(now or datetime.now(timezone.utc))
    cutoff = (instant - timedelta(days=WINDOW_DAYS)).strftime('%Y-%m-%d %H:%M:%S')
    rows = db.execute("""SELECT price_cents,currency,checked_at
        FROM public_price_observations WHERE product_url=? AND checked_at>=? AND checked_at<=?
        ORDER BY checked_at""", (product_url,cutoff,instant.strftime('%Y-%m-%d %H:%M:%S'))).fetchall()
    return assess(current, [dict(row) for row in rows], instant)
