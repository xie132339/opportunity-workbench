"""Read-only public merchant short-link evidence. No JS, cookies, signatures or purchases."""
import json
import html
import re
import time
from datetime import datetime,timezone,timedelta
from urllib.parse import urlparse,urljoin,parse_qs
import requests
from db import connect
from services.merchant_product_page import (parse_product_page as parse_merchant_product_page,
                                            supports_url as supports_merchant_product_page)

PUBLIC_HEADERS={
    'User-Agent':'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/140 Safari/537.36',
    'Accept':'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language':'zh-CN,zh;q=0.9',
}


def supported(url):
    try:
        u=urlparse(url);port=u.port
    except (ValueError,TypeError):return False
    if u.scheme!='https' or port not in (None,443) or u.username or u.password or u.fragment:return False
    if u.hostname=='u.jd.com':return bool(re.fullmatch(r'/[A-Za-z0-9]{3,32}',u.path)) and not u.query
    if u.hostname=='m.tb.cn':return bool(re.fullmatch(r'/h\.[A-Za-z0-9]+',u.path))
    if u.hostname=='p.pinduoduo.com':return bool(re.fullmatch(r'/[A-Za-z0-9_-]+',u.path))
    if u.hostname in ('ppd2.cc','y-08.com'):return bool(re.fullmatch(r'/[A-Za-z0-9_-]+',u.path)) and not u.query
    if u.hostname=='guangdiu.com':return u.path=='/go.php' and len(parse_qs(u.query).get('id',[]))==1 and bool(re.fullmatch(r'\d+',parse_qs(u.query)['id'][0]))
    if u.hostname=='s.click.taobao.com':return u.path=='/t' and bool(parse_qs(u.query).get('e'))
    if u.hostname=='union-click.jd.com':return u.path=='/jdc' and bool(parse_qs(u.query).get('p'))
    return False


def intermediate_hop(url):
    """Allow only the documented public JD affiliate hand-off path inside a resolution chain."""
    try:
        u=urlparse(url);port=u.port
    except (ValueError,TypeError):return False
    return (u.scheme=='https' and not u.username and not u.password and port in (None,443)
            and u.hostname in ('u.jd.com','union-click.jd.com') and u.path=='/jda')


def canonical_target(url):
    try:
        u=urlparse(url);port=u.port
    except (ValueError,TypeError):return None
    if u.scheme!='https' or u.username or u.password or port not in (None,443):return None
    if u.hostname=='item.jd.com' and re.fullmatch(r'/\d+\.html',u.path):return 'https://item.jd.com'+u.path
    if u.hostname=='item.m.jd.com' and re.fullmatch(r'/product/\d+\.html',u.path):return 'https://item.m.jd.com'+u.path
    query=parse_qs(u.query)
    if u.hostname in ('item.taobao.com','detail.tmall.com','chaoshi.detail.tmall.com') and u.path=='/item.htm':
        values=query.get('id',[])
        if len(values)==1 and re.fullmatch(r'\d+',values[0]):return 'https://'+u.hostname+'/item.htm?id='+values[0]
    if u.hostname in ('m.taobao.com','h5.m.taobao.com') and u.path in ('/awp/core/detail.htm','/item.htm'):
        values=query.get('id',[])
        if len(values)==1 and re.fullmatch(r'\d+',values[0]):return 'https://'+u.hostname+u.path+'?id='+values[0]
    if u.hostname=='mobile.yangkeduo.com' and u.path in ('/goods.html','/goods2.html','/duo_coupon_landing.html'):
        values=query.get('goods_id',[])
        if len(values)==1 and re.fullmatch(r'\d+',values[0]):return 'https://mobile.yangkeduo.com/goods.html?goods_id='+values[0]
    if u.hostname=='uland.taobao.com' and u.path=='/coupon/edetail':
        values=[]
        for name in ('itemId','item_id','auctionId'):values+=query.get(name,[])
        values=[v for v in values if re.fullmatch(r'\d{5,}',v)]
        if len(set(values))==1:return 'https://item.taobao.com/item.htm?id='+values[0]
    if u.hostname=='product.suning.com':
        match=re.fullmatch(r'/(\d+)/(\d+)\.html',u.path)
        if match:return 'https://product.suning.com/'+match[1]+'/'+match[2]+'.html'
    if u.hostname=='detail.vip.com':
        match=re.fullmatch(r'/detail-(\d+)-(\d+)\.html',u.path)
        if match:return 'https://detail.vip.com/detail-'+match[1]+'-'+match[2]+'.html'
    return None


def login_return_target(url):
    """Extract only a canonical product URL from a known JD login return parameter."""
    try:
        u=urlparse(url);port=u.port
    except (ValueError,TypeError):return None
    allowed={
        'passport.jd.com':{'/new/login.aspx'},
        'plogin.m.jd.com':{'/login/login'},
    }
    if (u.scheme!='https' or u.username or u.password or port not in (None,443)
        or u.hostname not in allowed or u.path not in allowed[u.hostname]):return None
    values=[]
    for key,items in parse_qs(u.query).items():
        if key.casefold() in ('returnurl','return_url'):values.extend(items)
    if len(values)!=1:return None
    return canonical_target(values[0])


def activity_target(url):
    try:
        u=urlparse(url);port=u.port
    except (ValueError,TypeError):return None
    if u.scheme!='https' or u.username or u.password or port not in (None,443):return None
    if u.hostname in ('pro.m.jd.com','prodev.m.jd.com') and re.fullmatch(r'/(?:mall|jdlite)/active/[A-Za-z0-9]+/index.html',u.path):return url
    if u.hostname=='plus.m.jd.com' and u.path=='/page/oss/rights/points/index':return url
    if u.hostname=='coupon.m.jd.com' and u.path=='/coupons/show.action':return url
    if u.hostname=='uland.taobao.com' and u.path=='/coupon/edetail':return url
    return None


def resolve(url):
    direct_target=canonical_target(url)
    if direct_target:
        return dict(state='resolved',target_url=direct_target,
                    reason='原文直接提供商品页地址；仅识别地址格式，不代表商品内容、价格或库存已核实')
    if not supported(url):return dict(state='unsupported',target_url=None,reason='不是受支持的公开商品短链')
    current=url
    for _ in range(5):
        try:
            u=urlparse(current);port=u.port
        except (ValueError,TypeError):return dict(state='blocked',target_url=None,reason='无效的跳转地址')
        if u.scheme!='https' or port not in (None,443) or u.username or u.password or not (intermediate_hop(current) or supported(current)):
            return dict(state='blocked',target_url=None,reason='跳转不在允许的公开解析路径内')
        try:
            with requests.get(current,timeout=(3,6),allow_redirects=False,stream=True,headers=PUBLIC_HEADERS) as response:
                if response.status_code in (401,403,429):return dict(state='blocked',target_url=None,reason='来源要求验证或限制访问')
                if 300<=response.status_code<400:target=urljoin(current,response.headers.get('location',''))
                else:
                    response.raise_for_status();body=b''
                    for chunk in response.iter_content(32768):
                        body+=chunk
                        if len(body)>150000:return dict(state='blocked',target_url=None,reason='短链页超过读取上限')
                    text=body.decode('utf-8',errors='replace')
                    match=re.search(r"(?:var\s+(?:hrl|url|real_jump_address)\s*=|location(?:\.href)?\s*=)\s*['\"]([^'\"]+)['\"]",text)
                    if not match:match=re.search(r"var\s+[A-Za-z_$][\w$]*\s*=\s*['\"](https://[^'\"]+)['\"]",text)
                    if not match:match=re.search(r'<meta[^>]+http-equiv=["\']?refresh["\']?[^>]+url=([^"\' >]+)',text,re.I)
                    target=html.unescape(match[1]) if match else ''
        except requests.RequestException:
            return dict(state='retry',target_url=None,reason='公开短链暂时访问失败')
        canonical=canonical_target(target)
        login_canonical=login_return_target(target)
        if canonical or login_canonical:
            return dict(state='resolved',target_url=canonical or login_canonical,
                        reason=('由公开登录返回地址取得商品地址；未核实价格、优惠资格、运费或库存'
                                if login_canonical else '由公开短链跳转取得商品地址；未核实价格、资格或库存'))
        if activity_target(target):return dict(state='activity',target_url=target,reason='已解析京东活动入口；商品范围、面额、资格和时效尚未核实')
        if not target:return dict(state='unresolved',target_url=None,reason='没有可直接读取的商品跳转字段')
        t=urlparse(target)
        if not (supported(target) or intermediate_hop(target)):
            return dict(state='blocked',target_url=None,reason='跳转没有落到允许的商家商品页')
        current=target
    return dict(state='unresolved',target_url=None,reason='有限跳转内未取得商品地址')


def source_link_urls(row):
    """Return embedded links plus a direct product/short-link opportunity URL."""
    links=[]
    for field in ('metadata_json','detail_json'):
        try:
            value=json.loads(row.get(field) or '{}')
        except (TypeError,ValueError):
            value={}
        candidates=value.get('activity_links',[])
        if isinstance(candidates,list):
            links.extend(url for url in candidates if isinstance(url,str) and url)
    opportunity_url=row.get('url')
    if isinstance(opportunity_url,str) and (canonical_target(opportunity_url)
            or supported(opportunity_url) or activity_target(opportunity_url)):
        links.append(opportunity_url)
    return list(dict.fromkeys(links))


def _title_alignment(source_title, product_title):
    source=re.sub(r'[^\w]+','',str(source_title or '').casefold())
    product=re.sub(r'[^\w]+','',str(product_title or '').casefold())
    if not source or not product:
        return 'unknown'
    return 'overlap' if source in product or product in source else 'different'


def link_actions(row, cache, now=None):
    """Build safe direct product actions separately from source and activity links."""
    now=now or datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    raw_links=source_link_urls(row)
    products={};pending={};activities={};other={}

    def fresh(item):
        return bool(item and item.get('checked_at') and item.get('next_check_at')
                    and item['checked_at']<=now<item['next_check_at'])

    def page_evidence(item):
        try:
            page=json.loads(item.get('product_page_json') or '{}') if item else {}
        except (TypeError,ValueError):
            page={}
        if page:
            checked=page.get('checked_at','')
            page['current']=bool(checked and page.get('next_check_at')
                                 and checked<=now<page['next_check_at'])
        return page

    for source_url in dict.fromkeys(url for url in raw_links if isinstance(url,str) and url):
        item=cache.get(source_url)
        direct=canonical_target(source_url)
        resolved=bool(fresh(item) and item.get('state')=='resolved'
                      and canonical_target(item.get('target_url') or ''))
        target=(canonical_target(item['target_url']) if resolved else direct)
        if target:
            if resolved:
                label='短链已解析到商品页' if not direct else '原文商品链接'
            elif activity_target(source_url):
                label='优惠入口含商品ID，已提取商品页'
            else:
                label='原文商品页链接'
            page_item=item if item and item.get('target_url')==target else cache.get(target)
            page=page_evidence(page_item)
            alignment=_title_alignment(row.get('title'),page.get('title'))
            candidate=products.setdefault(target,dict(
                target_url=target,source_urls=[],label=label,
                resolution_state='resolved' if resolved else 'direct',
                resolution_reason=(item.get('reason','') if resolved else
                    '从原文商品链接提取；页面商品、规格、价格和库存尚未核实'),
                product_page=page,title_alignment=alignment))
            if source_url not in candidate['source_urls']:
                candidate['source_urls'].append(source_url)

        activity=activity_target(source_url)
        if not activity and fresh(item) and item.get('state')=='activity':
            activity=activity_target(item.get('target_url') or '')
        if activity:
            activities.setdefault(activity,dict(url=activity,source_url=source_url,
                label='原文优惠/活动入口',reason='可直接打开优惠或活动入口；不代表适用于本商品或账号'))

        if not target and supported(source_url):
            if item and not fresh(item):
                reason='短链解析结果已过期，等待后台复查'
            elif item:
                reason=item.get('reason') or '短链暂未解析到商品页'
            else:
                reason='等待后台解析短链；跳转商品尚未确认'
            pending.setdefault(source_url,dict(url=source_url,
                state=item.get('state') if item else 'queued',reason=reason))

        if not target and not activity and not supported(source_url):
            try:
                parsed=urlparse(source_url);port=parsed.port
            except (TypeError,ValueError):
                parsed=None;port=None
            if (parsed and parsed.scheme=='https' and parsed.hostname
                    and not parsed.username and not parsed.password and port in (None,443)):
                other.setdefault(source_url,dict(url=source_url,host=parsed.hostname,
                    label='原文链接，类型未识别'))

    return list(products.values()),list(pending.values()),list(activities.values()),list(other.values())


def enrich(row, cache):
    row=dict(row);metadata=json.loads(row.get('metadata_json') or '{}');detail=json.loads(row.get('detail_json') or '{}')
    links=metadata.get('activity_links',[])+detail.get('activity_links',[])
    now=datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    evidence=[]
    for url in dict.fromkeys(links):
        item=cache.get(url)
        if not item or item['state']!='resolved' or not item['checked_at']<=now<item['next_check_at']:
            continue
        item=dict(item,source_url=url)
        try:
            product_page=json.loads(item.get('product_page_json') or '{}')
        except (TypeError,ValueError):
            product_page={}
        if product_page:
            checked=product_page.get('checked_at','')
            product_page['current']=bool(checked and product_page.get('next_check_at')
                                         and checked<=now<product_page['next_check_at'])
        item['product_page']=product_page
        evidence.append(item)
    metadata['resolved_link_evidence']=evidence
    metadata['activity_links']=list(dict.fromkeys(metadata.get('activity_links',[])+[e['target_url'] for e in evidence]))
    row['metadata_json']=json.dumps(metadata,ensure_ascii=False)
    (row['product_links'],row['pending_purchase_links'],row['activity_targets'],
     row['unclassified_links'])=link_actions(row,cache,now)
    return row


def resolution_cache(db, rows):
    """Load only link evidence referenced by the supplied rows."""
    urls=set()
    for row in rows:
        for field in ('metadata_json','detail_json'):
            try:
                value=json.loads(row.get(field) or '{}')
            except (TypeError,ValueError):
                continue
            links=value.get('activity_links',[])
            if isinstance(links,list):
                urls.update(link for link in links if isinstance(link,str) and link)
        opportunity_url=row.get('url')
        if isinstance(opportunity_url,str) and (canonical_target(opportunity_url)
                or supported(opportunity_url) or activity_target(opportunity_url)):
            urls.add(opportunity_url)
    if not urls:
        return {}
    ordered=sorted(urls)
    placeholders=','.join('?' for _ in ordered)
    return {row['url']:dict(row) for row in db.execute(
        f'SELECT * FROM link_resolutions WHERE url IN ({placeholders})',ordered)}


def inspect_product_page(target_url):
    """Read only public, structured product-page evidence; never infer a price from page text."""
    checked=datetime.now(timezone.utc)
    stamp=checked.strftime('%Y-%m-%d %H:%M:%S')
    if not supports_merchant_product_page(target_url):
        return dict(state='unsupported',checked_at=stamp,next_check_at=(checked+timedelta(hours=12)).strftime('%Y-%m-%d %H:%M:%S'),
                    reason='商品页不在公开结构化页面适配范围')
    try:
        from scanner import _fetch
        detail=parse_merchant_product_page(target_url,_fetch(target_url,timeout=(3,8)))
        if detail.get('availability_status')=='out_of_stock':
            state='out_of_stock'
        elif detail.get('advertised_cents') is not None:
            state='public_price'
        elif detail.get('page_amount_cents') is not None:
            state='page_amount_only'
        else:
            state='no_public_price'
        next_hours=6 if state in ('public_price','page_amount_only') else 12
        return dict(state=state,checked_at=stamp,
                    next_check_at=(checked+timedelta(hours=next_hours)).strftime('%Y-%m-%d %H:%M:%S'),
                    title=detail.get('title',''),product_identity=detail.get('product_identity',''),
                    advertised_cents=detail.get('advertised_cents'),page_amount_cents=detail.get('page_amount_cents'),
                    page_amount_label=detail.get('page_amount_label',''),specification=detail.get('specification',''),
                    currency=detail.get('currency',''),price_evidence=detail.get('price_evidence',''),
                    availability_status=detail.get('availability_status',''),reason=detail.get('conditions',''),
                    evidence=detail.get('evidence',''))
    except PermissionError as exc:
        state='blocked'
        reason=str(exc)
    except requests.HTTPError as exc:
        response=getattr(exc,'response',None)
        state='unreadable'
        reason=f"商品页返回 HTTP {response.status_code if response is not None else '错误'}；未解析价格"
    except requests.RequestException:
        state='retry'
        reason='商品頁公開請求暫時失敗；未解析價格'
    except Exception as exc:
        state='unreadable'
        reason=str(exc)[:240]
    delay=timedelta(minutes=30) if state=='retry' else timedelta(hours=12)
    return dict(state=state,checked_at=stamp,next_check_at=(checked+delay).strftime('%Y-%m-%d %H:%M:%S'),reason=reason)


def probe_resolved_pages(limit=2, source_urls=None):
    """Probe a small number of fresh resolved merchant links and replace only their current cache result."""
    limit=max(0,int(limit))
    if not limit:
        return {}
    now=datetime.now(timezone.utc).replace(tzinfo=None)
    with connect() as db:
        if source_urls is None:
            rows=db.execute("""SELECT url,target_url,product_page_json FROM link_resolutions
                WHERE state='resolved' AND target_url IS NOT NULL
                AND checked_at<=CURRENT_TIMESTAMP AND next_check_at>CURRENT_TIMESTAMP
                ORDER BY checked_at,url LIMIT 100""").fetchall()
        elif not source_urls:
            rows=[]
        else:
            values=list(dict.fromkeys(source_urls))
            marks=','.join('?' for _ in values)
            rows=db.execute(f"""SELECT url,target_url,product_page_json FROM link_resolutions
                WHERE state='resolved' AND target_url IS NOT NULL
                AND checked_at<=CURRENT_TIMESTAMP AND next_check_at>CURRENT_TIMESTAMP
                AND url IN ({marks}) ORDER BY checked_at,url""",values).fetchall()
    due=[]
    for row in rows:
        try:
            previous=json.loads(row['product_page_json'] or '{}')
            checked=datetime.fromisoformat(previous.get('next_check_at',''))
            if checked.tzinfo:
                checked=checked.astimezone(timezone.utc).replace(tzinfo=None)
            if checked>now:
                continue
        except (TypeError,ValueError):
            pass
        if supports_merchant_product_page(row['target_url']):
            due.append(row)
        if len(due)>=max(0,limit):
            break
    counts={}
    for row in due:
        result=inspect_product_page(row['target_url'])
        with connect() as db:
            db.execute("""UPDATE link_resolutions SET product_page_json=?
                WHERE url=? AND target_url=? AND state='resolved'""",
                (json.dumps(result,ensure_ascii=False),row['url'],row['target_url']))
        counts[result['state']]=counts.get(result['state'],0)+1
    return counts


def run_cycle(limit=10, urls=None):
    started=time.monotonic()
    requested_urls=urls
    resolved_urls=[]
    with connect() as c:
        cache={r['url']:dict(r) for r in c.execute('SELECT * FROM link_resolutions')}
        if urls is None:
            rows=c.execute("""SELECT o.url,e.metadata_json,a.detail_json FROM opportunities o JOIN events e ON e.id=o.event_id
                JOIN sources s ON s.id=o.source_id LEFT JOIN auto_reviews a ON a.opportunity_id=o.id
                WHERE s.enabled=1 AND o.status NOT IN ('ignored','expired')
                AND e.published_at BETWEEN datetime('now','-2 hours') AND CURRENT_TIMESTAMP
                ORDER BY e.published_at ASC,o.id ASC""").fetchall()
            urls=[]
            for row in rows:
                urls.extend(source_link_urls(dict(row)))
    now=datetime.now(timezone.utc);stamp=now.strftime('%Y-%m-%d %H:%M:%S');counts={}
    selected=[u for u in dict.fromkeys(urls) if (supported(u) or canonical_target(u)) and (u not in cache or cache[u]['next_check_at']<=stamp)][:limit]
    for url in selected:
        if time.monotonic()-started>45:break
        result=resolve(url);counts[result['state']]=counts.get(result['state'],0)+1
        if result['state']=='resolved':resolved_urls.append(url)
        checked=datetime.now(timezone.utc);nxt=checked+timedelta(hours=24 if result['state']=='resolved' else 1)
        with connect() as c:c.execute('''INSERT INTO link_resolutions(url,target_url,state,reason,checked_at,next_check_at) VALUES(?,?,?,?,?,?)
            ON CONFLICT(url) DO UPDATE SET target_url=excluded.target_url,state=excluded.state,reason=excluded.reason,checked_at=excluded.checked_at,next_check_at=excluded.next_check_at,
            product_page_json=CASE WHEN link_resolutions.target_url IS excluded.target_url THEN link_resolutions.product_page_json ELSE '{}' END''',
            (url,result['target_url'],result['state'],result['reason'],checked.strftime('%Y-%m-%d %H:%M:%S'),nxt.strftime('%Y-%m-%d %H:%M:%S')))
    # In the scheduled worker, only recheck product pages referenced by fresh,
    # currently enabled sources. The global resolution cache also contains
    # archived links from paused sources and must not keep them active.
    page_counts=probe_resolved_pages(limit=2,source_urls=resolved_urls if requested_urls is not None else urls)
    for state,count in page_counts.items():
        counts[f'product_page_{state}']=count
    return counts
