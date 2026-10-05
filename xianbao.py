"""Read-only public activity feeds. Does not claim, join, sign in or send messages."""
import re
from datetime import datetime, timezone
from urllib.parse import urlparse, urljoin

import requests
from bs4 import BeautifulSoup

BASE = 'https://new.ixbk.net'
CHANNELS = {'push':'综合优惠', 'push_16':'赚客吧', 'push_18':'新赚吧',
            'push_10':'微博线报', 'push_23':'豆瓣线报', 'push_17':'酷安线报'}


def validate_url(url):
    allowed = {BASE + '/plus/json/' + key + '.json' for key in CHANNELS}
    if url not in allowed:
        raise ValueError('线报接口须选择已适配的 new.ixbk.net 公开 JSON 栏目')


def extract_links(html):
    soup = BeautifulSoup(str(html or ''), 'html.parser')
    candidates = [a.get('href','') for a in soup.select('a[href]')]
    candidates += re.findall(r'https://[^\s<>"“”]+', soup.get_text(' ',strip=True))
    links = []
    for value in candidates:
        href = urljoin(BASE, value).rstrip('，。；;）)')
        u = urlparse(href)
        if u.scheme == 'https' and u.hostname and not u.username and not u.password and href not in links:
            links.append(href)
    return links[:20]


def parse_items(data):
    if not isinstance(data, list):
        raise ValueError('线报接口未返回条目列表')
    result = []
    for item in data[:500]:
        if not isinstance(item,dict): continue
        path = str(item.get('url') or '')
        title = ' '.join(str(item.get('title') or '').split())[:300]
        if not title or not re.fullmatch(r'/[a-zA-Z0-9_-]+/\d+\.html',path): continue
        html = BeautifulSoup(str(item.get('content_html') or ''),'html.parser')
        body = BeautifulSoup(str(item.get('content') or ''),'html.parser').get_text(' ',strip=True)
        if not body: body = html.get_text(' ',strip=True)
        links = extract_links(str(html))
        published = None
        try:
            raw = item.get('shijianchuo')
            if raw is not None and not isinstance(raw,bool):
                published = datetime.fromtimestamp(float(raw),timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        except (ValueError,TypeError,OverflowError,OSError): pass
        metadata = {'aggregator':'线报酷','source_category':str(item.get('catename') or '')[:200],
                    'source_price':item.get('price'), 'activity_links':links[:20],
                    'body_truncated':len(body)>12000}
        result.append((path,title,BASE+path,body[:12000],published,metadata))
    return result


def fetch_rows(url):
    validate_url(url)
    r = requests.get(url,timeout=(5,15),allow_redirects=False,
                     headers={'User-Agent':'OpportunityWorkbench/0.1 (public activity research)'})
    if r.status_code in (401,403,429):
        raise PermissionError(f'线报接口 HTTP {r.status_code}，等待来源恢复')
    if r.is_redirect: raise RuntimeError('线报接口跳转，需核对新的来源地址')
    r.raise_for_status()
    if len(r.content)>2_000_000: raise ValueError('线报响应超过2MB')
    return parse_items(r.json())
