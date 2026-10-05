"""Bounded read-only checks of public merchant product pages.

This records what a public HTML page explicitly exposes. It never uses a login,
browser session, cart, coupon claim, purchase flow, or a value as account checkout.
"""
import json
import re
import time
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from urllib.parse import urlparse

import requests

from db import connect
from link_resolution import canonical_target

MAX_BYTES = 500_000
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (compatible; OpportunityWorkbench/1.0; +http://127.0.0.1)',
    'Accept': 'text/html,application/xhtml+xml',
    'Accept-Language': 'zh-CN,zh;q=0.9',
}


class _PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta = {}
        self.title = []
        self.in_title = False
        self.scripts = []
        self.script_type = ''
        self.script_data = []

    def handle_starttag(self, tag, attrs):
        attrs = {k.lower(): v for k, v in attrs if k}
        if tag == 'meta':
            key = (attrs.get('property') or attrs.get('name') or '').lower()
            if key and attrs.get('content'):
                self.meta[key] = attrs['content'].strip()
        elif tag == 'title':
            self.in_title = True
        elif tag == 'script':
            self.script_type = attrs.get('type', '').lower()
            self.script_data = []

    def handle_endtag(self, tag):
        if tag == 'title':
            self.in_title = False
        elif tag == 'script':
            if 'ld+json' in self.script_type and self.script_data:
                self.scripts.append(''.join(self.script_data))
            self.script_type = ''
            self.script_data = []

    def handle_data(self, data):
        if self.in_title:
            self.title.append(data)
        if self.script_type:
            self.script_data.append(data)


def _walk(value):
    if isinstance(value, dict):
        yield value
        graph = value.get('@graph')
        if graph is not None:
            yield from _walk(graph)
        for key, item in value.items():
            if key != '@graph':
                yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def _is_product(item):
    kinds = item.get('@type', [])
    if isinstance(kinds, str):
        kinds = [kinds]
    return any(str(kind).split('/')[-1].casefold() in ('product', 'individualproduct') for kind in kinds)


def _price_cents(value):
    if isinstance(value, (dict, list, bool)) or value is None:
        return None
    text = str(value).strip().replace(',', '')
    if not re.fullmatch(r'\d+(?:\.\d{1,2})?', text):
        return None
    try:
        amount = Decimal(text) * 100
    except InvalidOperation:
        return None
    return int(amount) if amount >= 0 else None


def extract_page(html_text):
    parser = _PageParser()
    parser.feed(html_text)
    title = ' '.join(' '.join(parser.title).split())[:240]
    # A merchant URL can serve a login/challenge document with HTTP 200.
    # Such a response is not evidence that the product page was read.
    if re.match(r'^(?:请先)?(?:登录|登陆|安全验证|访问验证|人机验证|验证码)(?:\b|\s|[-_|｜－：:])', title, re.I):
        return dict(state='access_gate', reason='商家地址返回登录或验证页，未读取到商品报价', page_title=title)
    found = []
    for raw in parser.scripts:
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            continue
        for item in _walk(payload):
            if not _is_product(item):
                continue
            offers = item.get('offers', [])
            if isinstance(offers, dict):
                offers = [offers]
            for offer in offers if isinstance(offers, list) else []:
                if not isinstance(offer, dict):
                    continue
                amount = _price_cents(offer.get('price'))
                currency = str(offer.get('priceCurrency') or '').upper()
                if amount is not None:
                    found.append((amount, currency, str(offer.get('availability') or '')))
    # Open Graph fallback requires an explicit currency too.
    if not found:
        amount = _price_cents(parser.meta.get('product:price:amount') or parser.meta.get('og:price:amount'))
        currency = str(parser.meta.get('product:price:currency') or parser.meta.get('og:price:currency') or '').upper()
        if amount is not None:
            found.append((amount, currency, ''))
    amounts = {(amount, currency) for amount, currency, _ in found}
    if len(amounts) != 1:
        return dict(state='ambiguous' if amounts else 'page_read_no_price', reason=(
            '页面存在多个互相冲突的公开报价，不能选取其一' if amounts else
            'HTTP 页面无唯一且明确币种的结构化商品报价；可能由浏览器动态加载，不能据此核实价格'), page_title=title)
    amount, currency = next(iter(amounts))
    availability = next(iter({a for _, _, a in found})) if len({a for _, _, a in found}) == 1 else ''
    if currency not in ('CNY', 'RMB', 'CNH'):
        return dict(state='unsupported_currency', reason='页面报价缺少人民币币种或币种不是人民币',
                    page_title=title, price_cents=None, currency=currency, availability=availability)
    return dict(state='public_price_observed', reason='读取到公开商品页结构化标价；页面未提供账号结算、运费、券资格或库存数据',
                page_title=title, price_cents=amount, currency=currency, availability=availability)


def check_page(url):
    # Strictly refuse redirects: no second host can be reached by an implicit hop.
    if not url or canonical_target(url) != url:
        return dict(state='unsupported_url', reason='目标不是允许列表中的规范商家商品页')
    parsed = urlparse(url)
    if parsed.scheme != 'https' or parsed.port not in (None, 443) or parsed.username or parsed.password:
        return dict(state='unsupported_url', reason='商品页地址不符合公开 HTTPS 安全规则')
    try:
        with requests.get(url, headers=HEADERS, timeout=(3, 6), allow_redirects=False,
                          stream=True) as response:
            if response.status_code in (401, 403, 429):
                return dict(state='blocked', reason=f'商家公开页拒绝或限制访问（HTTP {response.status_code}）')
            if 300 <= response.status_code < 400:
                return dict(state='redirect', reason='商品页要求跳转；为避免访问未校验地址，本次停止')
            response.raise_for_status()
            content_type = response.headers.get('Content-Type', '').lower()
            if 'html' not in content_type:
                return dict(state='not_html', reason='商家入口返回的不是 HTML 商品页')
            body = bytearray()
            for chunk in response.iter_content(16_384):
                body.extend(chunk)
                if len(body) > MAX_BYTES:
                    return dict(state='too_large', reason='商品页超过 500 KB 读取上限')
            encoding = response.encoding or 'utf-8'
            return extract_page(bytes(body).decode(encoding, errors='replace'))
    except requests.RequestException:
        return dict(state='retry', reason='商家公开页暂时不可读取；保留原记录并按退避复查')


def targets_for_row(row, resolved_links=None):
    urls = [row.get('url', '')]
    try:
        meta = json.loads(row.get('metadata_json') or '{}')
    except (ValueError, TypeError):
        meta = {}
    try:
        detail = json.loads(row.get('detail_json') or '{}')
    except (ValueError, TypeError):
        detail = {}
    stamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    evidence_by_url = {e.get('source_url'): e for e in meta.get('resolved_link_evidence', []) or []}
    for source_url in list(meta.get('activity_links', []) or []) + list(detail.get('activity_links', []) or []):
        evidence = evidence_by_url.get(source_url) or (resolved_links or {}).get(source_url)
        if (evidence and evidence.get('state') == 'resolved'
                and evidence.get('checked_at', '') <= stamp < evidence.get('next_check_at', '')):
            urls.append(evidence.get('target_url', ''))
    for source in (meta, detail):
        for url in source.get('activity_links', []) or []:
            if canonical_target(url) == url:
                urls.append(url)
    return list(dict.fromkeys(url for url in urls if canonical_target(url) == url))


def summarize(row, checks):
    targets = targets_for_row(row)
    candidates = [checks.get((row['id'], url)) for url in targets]
    candidates = [item for item in candidates if item]
    if candidates:
        candidates.sort(key=lambda item: (item.get('checked_at') or ''), reverse=True)
        selected = dict(candidates[0])
        try:
            checked_at = datetime.strptime(selected.get('checked_at', ''), '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
            next_check = datetime.strptime(selected.get('next_check_at', ''), '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            if checked_at > now or now >= next_check:
                selected.update(state='stale', price_cents=None,
                                reason='上次公开页核验已过期或时间异常，不能作为当前报价')
        except (TypeError, ValueError):
            selected.update(state='stale', price_cents=None, reason='缺少有效的公开页核验时效')
        return selected
    try:
        meta = json.loads(row.get('metadata_json') or '{}')
    except (ValueError, TypeError):
        meta = {}
    links = meta.get('activity_links', []) or []
    if any(str(url).startswith(('https://u.jd.com/', 'https://m.tb.cn/', 'https://p.pinduoduo.com/')) for url in links):
        state, reason = 'awaiting_link', '已发现平台短链；需先安全解析到商品页'
    elif links:
        state, reason = 'unsupported_or_activity', '目前只发现活动入口或不支持的商品链接，未取得可读取的商品页'
    else:
        state, reason = 'no_product_link', '来源未提供可解析的商家商品页链接'
    return dict(state=state, reason=reason, checked_at=None, price_cents=None, currency='', page_title='', product_url='')


def run_cycle(limit=6):
    started = time.monotonic()
    with connect() as db:
        resolved_links = {r['url']: dict(r) for r in db.execute('SELECT * FROM link_resolutions')}
        rows = db.execute("""SELECT o.id,o.title,e.url,e.metadata_json,e.published_at,a.detail_json
            FROM opportunities o JOIN events e ON e.id=o.event_id
            JOIN sources s ON s.id=o.source_id
            LEFT JOIN auto_reviews a ON a.opportunity_id=o.id
            WHERE s.enabled=1 AND s.status='healthy' AND o.status NOT IN ('ignored','expired')
              AND s.last_success>=datetime('now','-' || MIN(2*s.interval_minutes+15,120) || ' minutes')
              AND e.last_seen_at>=datetime('now','-' || MIN(2*s.interval_minutes+15,120) || ' minutes')
              AND e.published_at BETWEEN datetime('now','-2 hours') AND CURRENT_TIMESTAMP
            ORDER BY o.id DESC LIMIT 400""").fetchall()
        candidates = []
        for row in rows:
            for url in targets_for_row(dict(row), resolved_links):
                old = db.execute("SELECT next_check_at FROM merchant_page_checks WHERE opportunity_id=? AND product_url=?",
                                 (row['id'], url)).fetchone()
                if not old or old['next_check_at'] <= datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'):
                    candidates.append((row['id'], url))
        # Keep sampling a product page after its original deal post has aged out.
        # Price history belongs to the merchant URL, not to the feed post's 2-hour TTL.
        due_products = db.execute("""SELECT m.opportunity_id,m.product_url
            FROM merchant_page_checks m JOIN opportunities o ON o.id=m.opportunity_id
            WHERE m.state='public_price_observed' AND m.next_check_at<=CURRENT_TIMESTAMP
              AND o.status NOT IN ('ignored','expired')
            ORDER BY m.next_check_at LIMIT 400""").fetchall()
        candidates.extend((row['opportunity_id'], row['product_url']) for row in due_products)
        selected = list(dict.fromkeys(candidates))[:max(0, limit)]
    counts = {}
    for opportunity_id, url in selected:
        if time.monotonic() - started > 35:
            break
        result = check_page(url)
        checked = datetime.now(timezone.utc)
        minutes = 5 if result['state'] == 'public_price_observed' else 20
        next_check = checked + timedelta(minutes=minutes)
        result.update(opportunity_id=opportunity_id, product_url=url,
                      checked_at=checked.strftime('%Y-%m-%d %H:%M:%S'),
                      next_check_at=next_check.strftime('%Y-%m-%d %H:%M:%S'))
        with connect() as db:
            db.execute("""INSERT INTO merchant_page_checks
                (opportunity_id,product_url,state,reason,page_title,price_cents,currency,availability,checked_at,next_check_at)
                VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(opportunity_id,product_url) DO UPDATE SET
                state=excluded.state,reason=excluded.reason,page_title=excluded.page_title,
                price_cents=excluded.price_cents,currency=excluded.currency,availability=excluded.availability,
                checked_at=excluded.checked_at,next_check_at=excluded.next_check_at""",
                (opportunity_id,url,result['state'],result.get('reason',''),result.get('page_title',''),
                 result.get('price_cents'),result.get('currency',''),result.get('availability',''),
                 result['checked_at'],result['next_check_at']))
            if (result['state'] == 'public_price_observed' and result.get('price_cents') is not None
                    and result.get('currency') in ('CNY', 'RMB', 'CNH')):
                observation_hour = result['checked_at'][:13]
                existing = db.execute("""SELECT id FROM public_price_observations
                    WHERE product_url=? AND observation_hour=? ORDER BY id DESC LIMIT 1""",
                                      (url,observation_hour)).fetchone()
                values = (opportunity_id,result['price_cents'],result['currency'],
                          result.get('availability',''),result['checked_at'])
                if existing:
                    db.execute("""UPDATE public_price_observations SET opportunity_id=?,price_cents=?,
                        currency=?,availability=?,checked_at=? WHERE id=?""", values+(existing['id'],))
                else:
                    db.execute("""INSERT INTO public_price_observations
                        (opportunity_id,product_url,price_cents,currency,availability,checked_at,observation_hour)
                        VALUES(?,?,?,?,?,?,?)""", (opportunity_id,url,result['price_cents'],
                                                     result['currency'],result.get('availability',''),
                                                     result['checked_at'],observation_hour))
        counts[result['state']] = counts.get(result['state'], 0) + 1
    return counts
