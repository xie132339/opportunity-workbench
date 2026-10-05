"""Coupon and activity entry collection; entry discovery is not discount validation."""
import ipaddress,json,re,unicodedata
from datetime import datetime,timezone,timedelta
from decimal import Decimal
from urllib.parse import urlparse
import requests
from bs4 import BeautifulSoup
from db import connect
from offer import resource_kind,RESOURCE_LABELS
from link_resolution import supported,activity_target,canonical_target,resolve,enrich
from comparison import merchant_identity

KINDS={'coupon','points','trial','campaign','free_claim','lottery'}
SHORT_HOSTS={'m.tb.cn','tb3.cn','s.click.taobao.com','p.pinduoduo.com','kurl07.cn','163cn.tv','m.duanqu.com'}


def candidate_url(url):
    """Keep public HTTPS evidence even before an adapter knows how to read it."""
    try:
        parsed=urlparse(url);port=parsed.port
        if parsed.scheme!='https' or not parsed.hostname or port not in (None,443):return False
        if parsed.username or parsed.password or parsed.hostname=='localhost' or parsed.hostname.endswith('.local'):return False
        try:
            if ipaddress.ip_address(parsed.hostname).is_private:return False
        except ValueError:
            pass
        return True
    except (TypeError,ValueError):
        return False

def initial_state(url,kind):
    """Classify without throwing away the raw link or pretending it is a benefit."""
    if canonical_target(url):return 'product','公开地址是商品页，不作为优惠资源展示'
    if activity_target(url):return 'activity','公开地址是已支持的活动入口'
    if supported(url):return 'pending','公开短链等待自动识别'
    parsed=urlparse(url);host=(parsed.hostname or '').lower();location=(parsed.path+'?'+parsed.query).lower()
    benefit_hint=bool(re.search(r'coupon|coupons|active|activity|campaign|promo|sale|event|trial|try|points|exchange|rights|seckill|voucher|benefit',location))
    if host in SHORT_HOSTS or benefit_hint or kind in KINDS:
        return 'unsupported','优惠候选已保存，当前没有自动解析适配器'
    return 'non_benefit','已保存原始链接；目前没有优惠机制或入口特征'

def claims(text):
    text=unicodedata.normalize('NFKC',text or '')
    pairs=set(re.findall(r'(\d+(?:\.\d{1,2})?)\s*[-－]\s*(\d+(?:\.\d{1,2})?)\s*[^\d\s]{0,6}[券卷劵]',text))
    label='门槛/面额未明确'
    if len(pairs)==1:
        a,b=next(iter(pairs))
        if Decimal(a)>=Decimal(b)>0:label=f'声称满{a}减{b}，适用条件未核实'
    elif len(pairs)>1:label='提及多个门槛/面额，尚未拆分'
    times=re.findall(r'(?<!\d)([0-2]?\d)点',text)
    schedule='、'.join(t+'点' for t in dict.fromkeys(times) if int(t)<24)
    terms=[t for t in ('APP','PLUS','积分','特价版','宠物','医疗器械','全品') if t.casefold() in text.casefold()]
    return dict(discount=label,schedule=(schedule+'（日期与场次未确认）') if schedule else '领取时间未明确',terms='、'.join(terms) or '资格与适用商品未明确')

def add(title,url,source_type='collected',source_url='',source_opportunity_id=None,origin_text='',kind=None,published_at=None):
    if not candidate_url(url):return False
    kind=kind or resource_kind(title,origin_text)
    if kind not in KINDS:kind='pending'
    state,reason=initial_state(url,kind)
    source_url=source_url or (url if source_type in ('user_supplied','manual') else '')
    source_key=(f'opportunity:{source_opportunity_id}' if source_opportunity_id is not None
                else f'{source_type}:{source_url or url}')
    with connect() as c:
        c.execute('''INSERT INTO benefit_resources
            (url,title,kind,state,reason,source_type,source_url,source_opportunity_id,origin_text,published_at)
            VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET
            last_seen_at=CURRENT_TIMESTAMP,
            title=CASE WHEN benefit_resources.source_type='user_supplied' THEN benefit_resources.title ELSE excluded.title END,
            kind=CASE WHEN benefit_resources.kind='pending' THEN excluded.kind ELSE benefit_resources.kind END,
            state=CASE WHEN benefit_resources.state IN ('pending','unsupported','non_benefit') THEN excluded.state ELSE benefit_resources.state END,
            reason=CASE WHEN benefit_resources.state IN ('pending','unsupported','non_benefit') THEN excluded.reason ELSE benefit_resources.reason END''',
            (url,title,kind,state,reason,source_type,source_url,source_opportunity_id,origin_text or title,published_at))
        resource_id=c.execute('SELECT id FROM benefit_resources WHERE url=?',(url,)).fetchone()[0]
        c.execute('''INSERT INTO benefit_resource_sources
            (resource_id,source_key,source_type,source_url,source_opportunity_id,source_title,origin_text,published_at)
            VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(resource_id,source_key) DO UPDATE SET
            last_seen_at=CURRENT_TIMESTAMP,source_title=excluded.source_title,
            origin_text=excluded.origin_text,published_at=COALESCE(excluded.published_at,benefit_resource_sources.published_at)''',
            (resource_id,source_key,source_type,source_url,source_opportunity_id,title,origin_text or title,published_at))
        if source_opportunity_id is not None:
            c.execute('''INSERT OR IGNORE INTO benefit_product_relations
                (resource_id,opportunity_id,state,basis,evidence_url)
                VALUES(?,?,'source_linked','优惠入口与商品出现在同一条来源记录中',?)''',
                (resource_id,source_opportunity_id,source_url))
    return True

def import_authorized_record(record):
    """Store one normalized result from a platform-authorized coupon connector.

    This is an ingestion boundary, not a connector: platform-specific OAuth,
    signatures and response parsing belong to the authorized provider adapter.
    Only explicit eligible product identity keys are searchable here.
    """
    if not isinstance(record,dict):raise ValueError('优惠记录必须是对象')
    provider=str(record.get('provider','')).strip()[:80]
    title=str(record.get('title','')).strip()[:300]
    coupon_url=str(record.get('coupon_url','')).strip()
    source_url=str(record.get('source_url','')).strip()
    observed_at=str(record.get('observed_at','')).strip()
    if not provider or not title:raise ValueError('缺授权数据提供方或优惠标题')
    if not candidate_url(coupon_url) or not candidate_url(source_url):raise ValueError('优惠入口和来源依据必须是公开 HTTPS 链接')
    try:
        observed=datetime.fromisoformat(observed_at.replace('Z','+00:00'))
        if observed.tzinfo is None:raise ValueError
        observed=observed.astimezone(timezone.utc)
    except ValueError:raise ValueError('缺少带时区的接口观察时间')
    if observed>datetime.now(timezone.utc)+timedelta(minutes=5):
        raise ValueError('接口观察时间在未来，拒绝作为当前证据')
    platform=str(record.get('platform','')).strip().lower()
    if not re.fullmatch(r'[a-z][a-z0-9_]{0,31}',platform):
        raise ValueError('不支持的商品平台标识')
    identity_prefix=platform+':'
    identities=record.get('eligible_product_keys',[])
    if not isinstance(identities,list):raise ValueError('适用商品必须是商品ID列表')
    identities=list(dict.fromkeys(str(x).strip() for x in identities if isinstance(x,str)))
    pattern=re.compile(r'^[a-z][a-z0-9_]{0,31}:[0-9]+(?::[0-9]+)?$')
    if any(not pattern.fullmatch(x) or not x.startswith(identity_prefix) for x in identities):
        raise ValueError('适用商品 ID 格式与平台不一致')
    scope=str(record.get('scope_type','unknown')).strip().lower()
    if scope not in ('item','sku','store','category','platform','unknown'):
        raise ValueError('优惠范围类型无效')
    if scope in ('store','category','platform','unknown') and not identities:
        # Keep broad resources searchable in the coupon ledger, but never infer
        # that every product on a store/category/platform is eligible.
        pass
    def cents(name):
        value=record.get(name)
        if value is None:return None
        if isinstance(value,bool) or not isinstance(value,int) or value<0:
            raise ValueError(f'{name} 必须是非负整数分')
        return value
    valid_from=str(record.get('valid_from') or '').strip() or None
    valid_until=str(record.get('valid_until') or '').strip() or None
    for name,value in (('valid_from',valid_from),('valid_until',valid_until)):
        if value:
            try:
                parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
                if parsed.tzinfo is None:raise ValueError
            except ValueError:raise ValueError(f'{name} 必须是带时区的 ISO 时间')
    if valid_from and valid_until and datetime.fromisoformat(valid_from.replace('Z','+00:00'))>datetime.fromisoformat(valid_until.replace('Z','+00:00')):
        raise ValueError('优惠有效期结束时间早于开始时间')
    sku_ids=record.get('sku_ids',[])
    if not isinstance(sku_ids,list) or any(not isinstance(x,str) for x in sku_ids):
        raise ValueError('sku_ids 必须是字符串列表')
    evidence={
        'provider':provider,'platform':platform,'scope_type':scope,
        'eligible_product_keys':identities,'sku_ids':list(dict.fromkeys(sku_ids)),
        'source_url':source_url,
        'discount_cents':cents('discount_cents'),'threshold_cents':cents('threshold_cents'),
        'valid_from':valid_from,'valid_until':valid_until,'observed_at':observed.isoformat(),
        'eligibility':str(record.get('eligibility') or '未提供账号/会员资格范围')[:500],
        'region':str(record.get('region') or '未提供地区范围')[:300],
        'stackable':str(record.get('stackable') or 'unknown')[:40],
        'raw_rules':str(record.get('raw_rules') or '')[:2000],
        'note':'本地接入器提交的结构化接口线索；此处未验证提供方授权状态，未证明当前账号可领或可叠加。',
    }
    if not add(title,coupon_url,source_type='authorized_api:'+provider,
               source_url=source_url,origin_text=evidence['raw_rules'],kind='coupon'):
        raise ValueError('优惠入口 URL 无效')
    with connect() as c:
        row=c.execute('SELECT id,evidence_json FROM benefit_resources WHERE url=?',(coupon_url,)).fetchone()
        old=json.loads(row['evidence_json'] or '{}')
        scopes=old.get('authorized_scopes',[])
        if not isinstance(scopes,list):scopes=[]
        signature=(provider,tuple(identities),evidence['observed_at'])
        if not any((x.get('provider'),tuple(x.get('eligible_product_keys',[])),x.get('observed_at'))==signature for x in scopes if isinstance(x,dict)):
            scopes.append(evidence)
        old['authorized_scopes']=scopes[-20:]
        c.execute('UPDATE benefit_resources SET evidence_json=? WHERE id=?',
                  (json.dumps(old,ensure_ascii=False),row['id']))
    return row['id']

def sync():
    # Full scan is deliberate at the current data size: resource coverage matters
    # more than saving a small local query. Add a cursor only after measured growth.
    with connect() as c:
        rows=c.execute('''SELECT o.id,o.title,o.url,e.snippet,e.metadata_json,e.published_at,a.detail_json
            FROM opportunities o JOIN events e ON e.id=o.event_id
            LEFT JOIN auto_reviews a ON a.opportunity_id=o.id
            ORDER BY o.id DESC''').fetchall()
    added=0
    for r in rows:
        meta=json.loads(r['metadata_json'] or '{}');detail=json.loads(r['detail_json'] or '{}')
        body=detail.get('conditions') or r['snippet'] or '';kind=resource_kind(r['title'],body)
        links=list(dict.fromkeys(meta.get('activity_links',[])+detail.get('activity_links',[])))
        # The opportunity URL is often an article that reports the benefit. Keep
        # it as provenance when a concrete activity link was extracted, rather
        # than creating a second false benefit resource for the article itself.
        if kind in KINDS and (not links or supported(r['url']) or activity_target(r['url'])):
            links.insert(0,r['url'])
        for url in links:
            if candidate_url(url):
                added+=int(add(r['title'] if kind in KINDS else '待识别入口：'+r['title'],url,
                    source_url=r['url'],source_opportunity_id=r['id'],origin_text=body,
                    kind=kind if kind in KINDS else 'pending',published_at=r['published_at']))
    return added

def reclassify():
    """Reapply structural classification and reuse current shared link evidence."""
    changed=0;now=datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    with connect() as c:
        rows=c.execute('SELECT id,url,kind,state,target_url,reason FROM benefit_resources').fetchall()
        cache={r['url']:dict(r) for r in c.execute('SELECT * FROM link_resolutions')}
    for row in rows:
        state,reason=initial_state(row['url'],row['kind']);target=row['target_url']
        cached=cache.get(row['url'])
        cache_current=(cached and cached['checked_at'] and cached['next_check_at']
                       and cached['checked_at']<=now<cached['next_check_at'])
        if cache_current and cached['state']=='resolved' and canonical_target(cached['target_url']):
            state,reason,target='product','共享短链证据表明该入口落到商品页',cached['target_url']
        elif cache_current and cached['state']=='activity' and activity_target(cached['target_url']):
            state,reason,target='activity','共享短链证据表明该入口落到活动页',cached['target_url']
        elif canonical_target(target):
            state,reason='product','已有解析证据表明该入口落到商品页'
        elif activity_target(target):
            state,reason='activity','已有解析证据表明该入口落到活动页'
        if row['state'] in ('activity','product') and state not in ('activity','product'):
            continue
        if state!=row['state'] or target!=row['target_url']:
            with connect() as c:c.execute('UPDATE benefit_resources SET state=?,reason=?,target_url=? WHERE id=?',(state,reason,target,row['id']))
            changed+=1
    return changed

def inspect_page(target):
    if not activity_target(target):return dict(state='unsupported',note='尚未取得支持读取的活动地址')
    try:
        with requests.get(target,timeout=(3,7),allow_redirects=False,stream=True) as r:
            if 300<=r.status_code<400:
                host=urlparse(r.headers.get('location','')).hostname or ''
                return dict(state='login_required' if host in ('plogin.m.jd.com','passport.jd.com') else 'redirect',note='公开入口转到登录，尚未取得权益内容' if host in ('plogin.m.jd.com','passport.jd.com') else '页面要求进一步跳转，未跟随',http_status=r.status_code)
            if r.status_code in (401,403,429):return dict(state='blocked',note='页面限制访问，尚未取得规则',http_status=r.status_code)
            r.raise_for_status();body=b''
            for chunk in r.iter_content(65536):
                body+=chunk
                if len(body)>2000000:return dict(state='limited',note='页面超过读取上限，未作为完整规则')
        soup=BeautifulSoup(body,'html.parser');title=soup.title.get_text(' ',strip=True) if soup.title else ''
        for tag in soup(['script','style']):tag.decompose()
        text=re.sub(r'[\u200b-\u200f\ufeff]','',soup.get_text(' ',strip=True)).strip()
        notices=[x for x in ('已抢光','抢完了','活动已结束','活动已过期') if x in text]
        times=re.findall(r'\d{1,2}:\d{2}再来',text)
        note='页面提示：'+'；'.join(notices+times)+'。仅表示所读页面提示，未核实原文券规则。' if notices else ('已读取活动页面，券适用范围与可领取性仍未确认' if text else '公开响应无可读规则，需要动态页面或APP数据')
        return dict(state='read' if text else 'dynamic',page_title=title,excerpt=text[:900],note=note,http_status=r.status_code)
    except requests.RequestException:return dict(state='retry',note='公开页面暂时访问失败')

def run_cycle(limit=4):
    sync()
    reclassify()
    with connect() as c:rows=c.execute("""SELECT * FROM benefit_resources
        WHERE state IN ('pending','activity','retry','blocked','unresolved')
          AND (next_check_at IS NULL OR next_check_at<=CURRENT_TIMESTAMP)
        ORDER BY next_check_at IS NOT NULL,next_check_at,source_type='user_supplied' DESC,id LIMIT ?""",(limit,)).fetchall()
    counts={}
    for r in rows:
        resolution=(resolve(r['url']) if supported(r['url']) else
                    dict(state='activity',target_url=r['url'],reason='原文活动地址') if activity_target(r['url']) else
                    dict(state='unsupported',target_url=None,reason='入口已保存，当前没有自动解析适配器'))
        checked=datetime.now(timezone.utc);stamp=checked.strftime('%Y-%m-%d %H:%M:%S')
        if supported(r['url']):
            with connect() as c:c.execute('''INSERT INTO link_resolutions(url,target_url,state,reason,checked_at,next_check_at) VALUES(?,?,?,?,?,?)
                ON CONFLICT(url) DO UPDATE SET target_url=excluded.target_url,state=excluded.state,reason=excluded.reason,checked_at=excluded.checked_at,next_check_at=excluded.next_check_at''',
                (r['url'],resolution['target_url'],resolution['state'],resolution['reason'],stamp,(checked+timedelta(hours=1)).strftime('%Y-%m-%d %H:%M:%S')))
        evidence=inspect_page(resolution['target_url']) if resolution['state']=='activity' else dict(state=resolution['state'],note=resolution['reason'])
        counts[evidence['state']]=counts.get(evidence['state'],0)+1
        state=('product' if resolution['state']=='resolved' else
               'activity' if resolution['state']=='activity' else resolution['state'])
        kind='campaign' if state=='activity' and r['kind']=='pending' else r['kind']
        next_check=(None if state=='unsupported' else checked+timedelta(
            hours=24 if state=='product' else 1 if evidence['state']!='read' else 0.25))
        with connect() as c:
            current=c.execute('SELECT evidence_json FROM benefit_resources WHERE id=?',(r['id'],)).fetchone()
            try:
                authorized=json.loads(current['evidence_json'] or '{}').get('authorized_scopes',[])
                if authorized:evidence['authorized_scopes']=authorized
            except (TypeError,ValueError):pass
            c.execute('''UPDATE benefit_resources SET state=?,kind=?,reason=?,target_url=?,
                evidence_json=?,checked_at=?,next_check_at=? WHERE id=?''',
                (state,kind,resolution['reason'],resolution['target_url'],json.dumps(evidence,ensure_ascii=False),stamp,
                 next_check.strftime('%Y-%m-%d %H:%M:%S') if next_check else None,r['id']))
    return counts

def listing(c,query='',kind='',limit=100,offset=0):
    rows=c.execute('''SELECT * FROM benefit_resources WHERE state NOT IN ('product','non_benefit')
        AND (?='' OR title LIKE ? OR origin_text LIKE ?) AND (?='' OR kind=?)
        ORDER BY source_type='user_supplied' DESC,CASE WHEN source_type='user_supplied' THEN id END ASC,last_seen_at DESC,id DESC
        LIMIT ? OFFSET ?''',(query,'%'+query+'%','%'+query+'%',kind,kind,limit,offset)).fetchall()
    return _present(c,rows)


def _present(c,rows):
    observations={r['resource_url']:dict(r) for r in c.execute('SELECT * FROM benefit_observations ORDER BY checked_at,id')}
    source_rows=c.execute('''SELECT brs.*,s.platform,s.name FROM benefit_resource_sources brs
        LEFT JOIN opportunities o ON o.id=brs.source_opportunity_id
        LEFT JOIN sources s ON s.id=o.source_id ORDER BY brs.last_seen_at DESC,brs.id DESC''').fetchall()
    sources={}
    for row in source_rows:sources.setdefault(row['resource_id'],[]).append(dict(row))
    relation_rows=c.execute('SELECT * FROM benefit_product_relations ORDER BY id').fetchall()
    relations={}
    for row in relation_rows:relations.setdefault(row['resource_id'],[]).append(dict(row))
    result=[];now=datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    for row in rows:
        r=dict(row);r.update(claims(r['title']+'\n'+r['origin_text']));r['label']=RESOURCE_LABELS.get(r['kind'],'优惠会场')
        r['sources']=sources.get(r['id'],[])
        r['relations']=relations.get(r['id'],[])
        r['source_linked_opportunities']=len({x['opportunity_id'] for x in r['relations'] if x['state']=='source_linked'})
        r['confirmed_opportunities']=len({x['opportunity_id'] for x in r['relations'] if x['state']=='confirmed'})
        r['evidence']=json.loads(r['evidence_json'] or '{}')
        api_scopes=r['evidence'].get('authorized_scopes',[])
        api_current=False
        for scope in api_scopes if isinstance(api_scopes,list) else []:
            try:
                observed=datetime.fromisoformat(str(scope['observed_at']).replace('Z','+00:00')).astimezone(timezone.utc)
                api_current=api_current or timedelta(0)<=datetime.now(timezone.utc)-observed<=timedelta(hours=2)
            except (KeyError,TypeError,ValueError):pass
        r['old_check']=not api_current and (not r['checked_at'] or not r['checked_at']<=now<(r['next_check_at'] or ''))
        if api_scopes:
            latest=api_scopes[-1]
            if isinstance(latest,dict) and isinstance(latest.get('discount_cents'),int) and not isinstance(latest.get('discount_cents'),bool):
                r['discount']=f"接口数据声称减 ¥{Decimal(latest['discount_cents'])/Decimal(100):.2f}"
                if isinstance(latest.get('threshold_cents'),int) and not isinstance(latest.get('threshold_cents'),bool):
                    r['discount']+=f"，门槛 ¥{Decimal(latest['threshold_cents'])/Decimal(100):.2f}"
        observation=observations.get(r['url'])
        r['browser_observation']=json.loads(observation['evidence_json']) if observation else None
        if observation:
            age=datetime.now(timezone.utc)-datetime.fromisoformat(observation['checked_at']).replace(tzinfo=timezone.utc)
            r['observation_old']=not timedelta(0)<=age<=timedelta(minutes=15)
            r['observation_time']=observation['checked_at']
        result.append(r)
    return result


def stats(c):
    counts=dict(c.execute('SELECT state,COUNT(*) FROM benefit_resources GROUP BY state').fetchall())
    return dict(discovered=sum(counts.values()),
                total=sum(v for k,v in counts.items() if k not in ('product','non_benefit')),
                activity=counts.get('activity',0),
                pending=sum(counts.get(k,0) for k in ('pending','retry','blocked','unresolved')),
                unsupported=counts.get('unsupported',0),product=counts.get('product',0),
                non_benefit=counts.get('non_benefit',0),
                sources=c.execute('SELECT COUNT(*) FROM benefit_resource_sources').fetchone()[0])


def related(c,opportunity_id):
    """Return only benefits carried by this opportunity's own source record.

    This is provenance, not merchant confirmation that the benefit applies.
    Same-platform or keyword similarity is deliberately insufficient.
    """
    rows=c.execute('''SELECT br.*,r.state AS relation_state,r.basis AS relation_basis,
        r.evidence_url AS relation_evidence_url,r.checked_at AS relation_checked_at,
        r.valid_until AS relation_valid_until,r.stackable AS relation_stackable
        FROM benefit_resources br JOIN benefit_product_relations r ON r.resource_id=br.id
        WHERE r.opportunity_id=? AND br.state NOT IN ('product','non_benefit')
          AND br.kind IN ('pending','coupon','points','trial','campaign','free_claim','lottery')
        ORDER BY br.last_seen_at DESC,br.id DESC LIMIT 20''',(opportunity_id,)).fetchall()
    items=_present(c,rows)
    for item in items:
        item['relation_label']=('公开证据支持适用' if item['relation_state']=='confirmed'
                                else '原文关联，尚无足够证据证明适用')
    return items


def product_match_candidates(c, opportunity):
    """Find fresh benefit clues from other posts that name the exact same market item ID.

    This is candidate discovery only. It never marks a coupon as applicable,
    transferable, stackable, or verified.
    """
    identity=merchant_identity(opportunity)
    if identity['conflict'] or not re.fullmatch(r'[a-z][a-z0-9_]{0,31}:[0-9]+(?::[0-9]+)?',identity['key']):
        return []
    opportunity_id=opportunity.get('id')
    current_ids={r['resource_id'] for r in c.execute(
        'SELECT resource_id FROM benefit_product_relations WHERE opportunity_id=?',(opportunity_id,))}
    cache={r['url']:dict(r) for r in c.execute('SELECT * FROM link_resolutions')}
    rows=c.execute('''SELECT br.*,r.opportunity_id AS match_source_opportunity_id,
        o.title AS match_source_title,o.url AS match_source_url,s.platform AS match_source_platform,
        s.name AS match_source_name,e.metadata_json AS match_metadata_json,
        e.url AS match_event_url,e.snippet AS match_snippet,
        a.detail_json AS match_detail_json
        FROM benefit_product_relations r
        JOIN benefit_resources br ON br.id=r.resource_id
        JOIN opportunities o ON o.id=r.opportunity_id
        JOIN events e ON e.id=o.event_id
        JOIN sources s ON s.id=o.source_id
        LEFT JOIN auto_reviews a ON a.opportunity_id=o.id
        WHERE r.state IN ('source_linked','confirmed')
          AND br.state NOT IN ('product','non_benefit','ignored','expired')
          AND br.kind IN ('pending','coupon','points','trial','campaign','free_claim','lottery')
          AND o.id<>?
          AND o.status NOT IN ('ignored','expired')
          AND s.enabled=1 AND s.status='healthy'
          AND s.last_success>=datetime('now','-' || MIN(2*s.interval_minutes+15,120) || ' minutes')
          AND e.last_seen_at>=datetime('now','-' || MIN(2*s.interval_minutes+15,120) || ' minutes')
          AND e.published_at BETWEEN datetime('now','-2 hours') AND CURRENT_TIMESTAMP
        ORDER BY e.published_at DESC,br.id DESC LIMIT 500''',(opportunity_id,)).fetchall()
    result={}
    for raw in rows:
        if raw['id'] in current_ids or raw['id'] in result:
            continue
        row=dict(raw,metadata_json=raw['match_metadata_json'] or '{}',
                 detail_json=raw['match_detail_json'] or '{}',url=raw['match_event_url'] or '')
        resolved=enrich(row,cache)
        match=merchant_identity(resolved)
        if match['conflict'] or match['key']!=identity['key']:
            continue
        item=dict(raw)
        item.update(claims(item['title']+'\n'+item['origin_text']),
                    label=RESOURCE_LABELS.get(item['kind'],'优惠会场'),
                    relation_state='product_match_candidate',
                    relation_label='其他新鲜来源明确指向同一商城商品ID；优惠范围和可用性未核实',
                    match_product_label=identity['label'],
                    match_source= f"{item['match_source_platform']} · {item['match_source_name']}")
        result[item['id']]=item
    # Authorized connectors write only normalized provider evidence. Broad
    # store/category coupons are searchable only when the provider also returns
    # an explicit eligible-product list; SKU-only scopes need a known target SKU.
    api_rows=c.execute("""SELECT br.* FROM benefit_resources br
        WHERE br.kind IN ('coupon','campaign','points','trial','free_claim','lottery')
          AND br.state NOT IN ('product','non_benefit','ignored','expired')
        ORDER BY br.last_seen_at DESC,br.id DESC LIMIT 500""").fetchall()
    now=datetime.now(timezone.utc)
    for raw in api_rows:
        item=dict(raw)
        try:scopes=json.loads(item['evidence_json'] or '{}').get('authorized_scopes',[])
        except (TypeError,ValueError):continue
        for scope in scopes if isinstance(scopes,list) else []:
            if (not isinstance(scope,dict) or scope.get('scope_type') not in ('item','store','category','platform')
                    or scope.get('sku_ids')
                    or identity['key'] not in scope.get('eligible_product_keys',[])):continue
            try:
                observed=datetime.fromisoformat(str(scope.get('observed_at','')).replace('Z','+00:00'))
                if observed.tzinfo is None or not timedelta(0)<=now-observed.astimezone(timezone.utc)<=timedelta(hours=2):continue
                valid_from=datetime.fromisoformat(str(scope['valid_from']).replace('Z','+00:00')).astimezone(timezone.utc) if scope.get('valid_from') else None
                valid_until=datetime.fromisoformat(str(scope['valid_until']).replace('Z','+00:00')).astimezone(timezone.utc) if scope.get('valid_until') else None
            except (TypeError,ValueError,OverflowError):continue
            if valid_from and now<valid_from or valid_until and now>valid_until:continue
            if item['id'] in result:continue
            discount=scope.get('discount_cents')
            discount_label=(f"接口数据声称减 ¥{Decimal(discount)/Decimal(100):.2f}" if isinstance(discount,int) and not isinstance(discount,bool)
                            else '优惠金额未由接入接口明确返回')
            threshold=scope.get('threshold_cents')
            if isinstance(threshold,int) and not isinstance(threshold,bool):discount_label+=f"，门槛 ¥{Decimal(threshold)/Decimal(100):.2f}"
            item.update(claims(item['title']+'\n'+item['origin_text']),discount=discount_label,
                        label=RESOURCE_LABELS.get(item['kind'],'优惠会场'),
                        relation_state='product_match_candidate',
                        relation_label='接入接口声明该商城商品ID在优惠范围内；授权状态、账号资格、SKU与可用性未核实',
                        match_product_label=identity['label'],
                        match_source=f"接入接口 · {scope.get('provider','未标明')}",
                        evidence=dict(note=scope.get('note',''),provider=scope.get('provider'),
                                      source_url=scope.get('source_url'),
                                      eligibility=scope.get('eligibility'),region=scope.get('region'),
                                      valid_from=scope.get('valid_from'),valid_until=scope.get('valid_until'),
                                      stackable=scope.get('stackable'),raw_rules=scope.get('raw_rules')))
            result[item['id']]=item
    return list(result.values())[:20]


def linked_counts(c,opportunity_ids):
    if not opportunity_ids:return {}
    placeholders=','.join('?' for _ in opportunity_ids)
    rows=c.execute(f'''SELECT r.opportunity_id,
        COUNT(DISTINCT CASE WHEN r.state='confirmed' THEN r.resource_id END) AS confirmed,
        COUNT(DISTINCT CASE WHEN r.state='source_linked' THEN r.resource_id END) AS source_linked
        FROM benefit_product_relations r JOIN benefit_resources br ON br.id=r.resource_id
        WHERE r.opportunity_id IN ({placeholders})
          AND br.state NOT IN ('product','non_benefit')
          AND br.kind IN ('pending','coupon','points','trial','campaign','free_claim','lottery')
        GROUP BY r.opportunity_id''',opportunity_ids).fetchall()
    return {row['opportunity_id']:{'confirmed':row['confirmed'],'source_linked':row['source_linked']} for row in rows}


def record_browser_observation(observation):
    """Persist an actual external UI reading separately; HTTP polls cannot renew it."""
    url=observation['source_url']
    if not supported(url) and not activity_target(url):raise ValueError('Unsupported entry')
    checked=datetime.fromisoformat(observation['checked_at'].replace('Z','+00:00')).astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    with connect() as c:
        if not c.execute('SELECT 1 FROM benefit_resources WHERE url=?',(url,)).fetchone():raise ValueError('Entry missing')
        c.execute('INSERT OR IGNORE INTO benefit_observations(resource_url,checked_at,method,evidence_json) VALUES(?,?,?,?)',
            (url,checked,'browser',json.dumps(observation,ensure_ascii=False)))
