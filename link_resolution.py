"""Read-only public merchant short-link evidence. No JS, cookies, signatures or purchases."""
import json
import html
import re
import time
from datetime import datetime,timezone,timedelta
from urllib.parse import urlparse,urljoin,parse_qs
import requests
from db import connect

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
    if u.hostname=='m.taobao.com' and u.path in ('/awp/core/detail.htm','/item.htm'):
        values=query.get('id',[])
        if len(values)==1 and re.fullmatch(r'\d+',values[0]):return 'https://m.taobao.com'+u.path+'?id='+values[0]
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


def enrich(row, cache):
    row=dict(row);metadata=json.loads(row.get('metadata_json') or '{}');detail=json.loads(row.get('detail_json') or '{}')
    links=metadata.get('activity_links',[])+detail.get('activity_links',[])
    now=datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    evidence=[dict(cache[u],source_url=u) for u in dict.fromkeys(links) if u in cache and cache[u]['state']=='resolved' and cache[u]['checked_at']<=now<cache[u]['next_check_at']]
    metadata['resolved_link_evidence']=evidence
    metadata['activity_links']=list(dict.fromkeys(metadata.get('activity_links',[])+[e['target_url'] for e in evidence]))
    row['metadata_json']=json.dumps(metadata,ensure_ascii=False)
    return row


def run_cycle(limit=6, urls=None):
    started=time.monotonic()
    with connect() as c:
        cache={r['url']:dict(r) for r in c.execute('SELECT * FROM link_resolutions')}
        if urls is None:
            rows=c.execute("""SELECT e.metadata_json,a.detail_json FROM opportunities o JOIN events e ON e.id=o.event_id
                JOIN sources s ON s.id=o.source_id LEFT JOIN auto_reviews a ON a.opportunity_id=o.id
                WHERE s.enabled=1 AND o.status NOT IN ('ignored','expired')
                AND e.published_at BETWEEN datetime('now','-2 hours') AND CURRENT_TIMESTAMP ORDER BY o.id DESC""").fetchall()
            urls=[]
            for row in rows:
                for field in ('metadata_json','detail_json'):urls.extend(json.loads(row[field] or '{}').get('activity_links',[]))
    now=datetime.now(timezone.utc);stamp=now.strftime('%Y-%m-%d %H:%M:%S');counts={}
    selected=[u for u in dict.fromkeys(urls) if supported(u) and (u not in cache or cache[u]['next_check_at']<=stamp)][:limit]
    for url in selected:
        if time.monotonic()-started>35:break
        result=resolve(url);counts[result['state']]=counts.get(result['state'],0)+1
        checked=datetime.now(timezone.utc);nxt=checked+timedelta(hours=24 if result['state']=='resolved' else 1)
        with connect() as c:c.execute('''INSERT INTO link_resolutions(url,target_url,state,reason,checked_at,next_check_at) VALUES(?,?,?,?,?,?)
            ON CONFLICT(url) DO UPDATE SET target_url=excluded.target_url,state=excluded.state,reason=excluded.reason,checked_at=excluded.checked_at,next_check_at=excluded.next_check_at''',
            (url,result['target_url'],result['state'],result['reason'],checked.strftime('%Y-%m-%d %H:%M:%S'),nxt.strftime('%Y-%m-%d %H:%M:%S')))
    return counts
