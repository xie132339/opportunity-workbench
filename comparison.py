"""Compare collected source claims, not account checkout or proven SKU matches."""
import json
import re
import unicodedata
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from urllib.parse import urlparse, parse_qs
from autoreview import offer_summary, moment, structured_spec
from link_resolution import enrich
from services.source_freshness import publication_is_current
from services.category_policy import decode_policy, default_policy, resolve_category_policy
from services.category_comparison_rules import default_rule, normalized_spec, resolve_category_rule


def product_key(title):
    text=unicodedata.normalize('NFKC',title or '').lower()
    text=re.sub(r'^清洁收纳[:：]\s*','',text)
    text=re.sub(r'\s+(?:券后)?\d+(?:\.\d+)?元.*$','',text)
    text=re.sub(r'[*×x]\s*\d+件\s*$','',text)
    text=text.replace('×','*')
    return re.sub(r'\s+','',text)


def specification_key(value):
    """Return a conservative key for an explicitly selected product variant."""
    text=unicodedata.normalize('NFKC',value or '').strip()
    if not text or re.search(r'任选|多规格|多款|随机|待选择|请选择|未选择',text,re.I):
        return ''
    text=re.sub(r'^(?:(?:该价格商品规格|商品规格|套餐类型|规格|型号|颜色分类|颜色|尺码|净含量)\s*[:：]\s*)+','',text,flags=re.I)
    return re.sub(r'[\s，,；;]+','',text).casefold()


def comparison_group_key(row, identity, brief, title_keys_for_merchant=(), title_platforms=(),
                         comparable_title_key=None, identity_mode="merchant_or_exact_title"):
    """Group exact-title, explicitly selected variants as cross-source candidates.

    Merchant IDs remain the strongest identity. When two marketplaces necessarily
    have different IDs, an exact normalized title plus an explicit selected spec
    can form a candidate group; it is never described as a verified shared SKU.
    """
    merchant_id = identity['key'].startswith(('jd:','taobao:','pdd:','suning:','vip:'))
    title_key = comparable_title_key or product_key(row.get('title',''))
    if identity_mode == 'merchant_id_only' and not merchant_id:
        return 'unmatched:'+str(row.get('id'))
    if (merchant_id and title_key and specification_key(brief.get('selected_spec'))
            and len(title_keys_for_merchant)==1 and len(title_platforms)>=2):
        return 'title-candidate:'+title_key
    return identity['key']


def merchant_identity(row):
    """Read explicit merchant product IDs; never infer them from coupon/shop/tracking IDs."""
    metadata=json.loads(row.get('metadata_json') or '{}')
    detail=json.loads(row.get('detail_json') or '{}')
    links=[row.get('url','')]+metadata.get('activity_links',[])+detail.get('activity_links',[])
    ids=set()
    for link in links:
        u=urlparse(link)
        if u.scheme!='https' or u.username or u.password or u.port not in (None,443):continue
        host=(u.hostname or '').lower();match=None;key=None
        if host=='item.jd.com':
            match=re.fullmatch(r'/(\d+)\.html',u.path)
            if match:key='jd:'+match[1]
        elif host=='item.m.jd.com':
            match=re.fullmatch(r'/product/(\d+)\.html',u.path)
            if match:key='jd:'+match[1]
            if u.path=='/ware/view.action':
                value=parse_qs(u.query).get('wareId',[])
                if len(value)==1 and re.fullmatch(r'\d+',value[0]):
                    key='jd:'+value[0]
        elif host in ('item.taobao.com','detail.tmall.com','chaoshi.detail.tmall.com') and u.path=='/item.htm':
            value=parse_qs(u.query).get('id',[])
            if len(value)==1 and re.fullmatch(r'\d+',value[0]):key='taobao:'+value[0]
        elif host=='m.taobao.com' and u.path in ('/awp/core/detail.htm','/item.htm'):
            value=parse_qs(u.query).get('id',[])
            if len(value)==1 and re.fullmatch(r'\d+',value[0]):key='taobao:'+value[0]
        elif host=='mobile.yangkeduo.com' and u.path in ('/goods.html','/goods2.html'):
            value=parse_qs(u.query).get('goods_id',[])
            if len(value)==1 and re.fullmatch(r'\d+',value[0]):key='pdd:'+value[0]
        elif host=='product.suning.com':
            match=re.fullmatch(r'/(\d+)/(\d+)\.html',u.path)
            if match:key='suning:'+match[1]+':'+match[2]
        elif host=='detail.vip.com':
            match=re.fullmatch(r'/detail-(\d+)-(\d+)\.html',u.path)
            if match:key='vip:'+match[1]+':'+match[2]
        if key:
            ids.add(key)
    if len(ids)==1:
        key=next(iter(ids));platform,raw=key.split(':',1)
        label={'jd':'京东','taobao':'淘宝/天猫','pdd':'拼多多','suning':'苏宁','vip':'唯品会'}[platform]
        return dict(key=key,label=label+'商品ID '+raw.replace(':',' / '),conflict=False)
    if len(ids)>1:return dict(key='ambiguous:'+str(row['id']),label='原文含多个商品ID，需先拆分方案',conflict=True)
    title=row.get('title','');key=product_key(title);spec=structured_spec(title)
    model=re.search(r'(?<![A-Za-z0-9])(?:[A-Za-z]{1,12}[ -]?)?\d{1,4}[A-Za-z][A-Za-z0-9+-]*|[A-Za-z]{2,12}\d{1,4}[A-Za-z0-9+-]*',title)
    ambiguous=re.search(r'任选|多规格|多款|随机|NB\s*/\s*S\s*/\s*M\s*/\s*L|S\s*/\s*M\s*/\s*L',title,re.I)
    if key and not ambiguous and (spec or model):
        return dict(key='catalog:'+key,label='标题型号/规格指纹（非商家SKU）'+((' · '+spec) if spec else ''),conflict=False)
    return dict(key='title:'+key,label='仅同标题候选，缺稳定型号或规格',conflict=False)


def assess_readiness(row, comparison=None, brief=None):
    """One evidence policy for source discovery and same-basis public quote comparisons."""
    brief = brief or offer_summary(row.get('title',''), row.get('url',''), row.get('snippet',''),
                                   metadata=row.get('metadata_json') or '{}',
                                   detail_json=row.get('detail_json'))
    identity = merchant_identity(row)
    plan = brief['audit']['plan']
    current = next((item for item in (comparison or {}).get('items',[])
                    if item['id'] == row.get('id')), None)
    identity_ok = not identity['key'].startswith(('title:','ambiguous:')) and not identity['conflict']
    merchant_id_ok = identity['key'].startswith(('jd:','taobao:','pdd:','suning:','vip:')) and not identity['conflict']
    selected_spec_ok = bool(specification_key(brief.get('selected_spec'))) and not brief.get('error')
    amount_ok = brief.get('total_cents') is not None and bool(brief.get('quantity')) and not brief.get('error')
    current_ok = current is not None and current.get('source_current') is True
    conditional = (row.get('auto_state') == 'conditional' or bool(brief['promotions'])
                   or bool(brief['qualifications']) or bool(brief['audit']['risks']))
    arithmetic_ok = not conditional or plan['state'] == 'conditional_match'
    conditional_product_binding_ok = not conditional or merchant_id_ok
    checks = dict(product=brief['kind']=='purchase', identity=identity_ok,
                  merchant_product_id=merchant_id_ok, selected_spec=selected_spec_ok,
                  order_amount=amount_ok, current=current_ok,
                  conditional_arithmetic=arithmetic_ok,
                  conditional_product_binding=conditional_product_binding_ok)
    search_keys=('product','identity','selected_spec','order_amount','current',
                 'conditional_arithmetic','conditional_product_binding')
    checks['search_ready'] = all(checks[key] for key in search_keys)
    required_offers=(comparison or {}).get('required_comparable_offers',
                     default_policy()['minimum_comparable_offers'])
    checks['two_comparable_offers'] = checks['search_ready'] and (comparison or {}).get('peers',0) >= required_offers
    checks['decision'] = (checks['two_comparable_offers'] and (comparison or {}).get('best_id') == row.get('id'))
    messages = {
        'product':'不是带明确商品报价的购买方案',
        'identity':'缺可复用的商品ID或严格型号/规格指纹',
        'selected_spec':'选中规格尚未结构化核对',
        'order_amount':'整单价或购买件数缺失、冲突',
        'current':'时效、来源状态或原文核验未通过',
        'conditional_arithmetic':'优惠条件无法复算到来源声称的整单价',
        'conditional_product_binding':'条件优惠缺商家商品ID，无法证明优惠与该商品绑定',
        'two_comparable_offers':f'缺少 {required_offers} 个证据完整的同口径购买方案',
        'decision':'当前来源报价不是同口径样本中的最低价或并列最低价',
    }
    return dict(checks=checks, search_ready=checks['search_ready'], comparable=checks['two_comparable_offers'],
                # This is only a low source-claim rank. It is not proven saving or a bargain.
                source_claim_low=checks['decision'], identity_label=identity['label'],
                search_failures=[messages[key] for key in search_keys if not checks[key]],
                decision_failures=[messages[key] for key in ('two_comparable_offers','decision') if not checks[key]],
                failures=[messages[key] for key,value in checks.items() if not value and key in messages],
                conditional=conditional, plan_state=plan['state'])


def quantity_options(items, target):
    """Pareto frontier of observed offers; does not invent repeatable orders or coupon stacks."""
    if target.get('unit_mode','exact_spec') != 'exact_spec':
        return dict(count=0,lowest_total=None,lowest_unit=None,frontier=[],
                    reason='启用了跨包装计价归一，不能把不同包装误作同款数量方案')
    eligible=[i for i in items if not i['problems'] and not i['optimization_gaps']
              and i['partition'][:2]==target['partition'][:2]]
    if not eligible:return dict(count=0,lowest_total=None,lowest_unit=None,frontier=[],reason='没有字段足够且当前可比的数量方案')
    # Without known fees compare goods amount only, never fill unknown shipping with zero.
    includes_shipping=all(i['shipping_cents'] is not None for i in eligible)
    def total(i):return i['total_cents']+(i['shipping_cents'] if includes_shipping else 0)
    def unit(i):return Fraction(total(i),i['quantity'])
    frontier=[]
    for i in eligible:
        if not any(total(j)<=total(i) and unit(j)<=unit(i) and (total(j)<total(i) or unit(j)<unit(i)) for j in eligible):
            frontier.append(i['id'])
    return dict(count=len(eligible),lowest_total=min(eligible,key=lambda i:(total(i),-i['id']))['id'],
                lowest_unit=min(eligible,key=lambda i:(unit(i),-i['id']))['id'],frontier=frontier,
                reason='按来源声称的含运费金额比较，资格仍未确认' if includes_shipping else '按商品金额比较；运费不全，不能确定最低到手价')


def load_comparisons(db):
    rows=db.execute("""SELECT o.*,e.snippet,e.metadata_json,e.published_at,e.last_seen_at,
        s.platform,s.parser AS source_parser,s.enabled,s.status AS source_status,s.interval_minutes,s.last_success,
        a.state AS auto_state,a.advertised_cents,a.detail_json,br.policy_json AS category_policy_json
        FROM opportunities o JOIN events e ON e.id=o.event_id JOIN sources s ON s.id=o.source_id
        LEFT JOIN auto_reviews a ON a.opportunity_id=o.id
        LEFT JOIN benchmark_topic_rules br ON br.topic_key=o.topic ORDER BY o.id DESC""").fetchall()
    cache={r['url']:dict(r) for r in db.execute('SELECT * FROM link_resolutions')}
    rows=[enrich(r,cache) for r in rows]
    categories=[dict(r) for r in db.execute("SELECT * FROM benchmark_categories WHERE enabled=1")]
    for row in rows:
        policy, category_name, error, policy_scope=resolve_category_policy(
            row.get('title'), row.get('topic'), categories, row.get('category_policy_json'))
        row['resolved_category_policy']=policy
        row['resolved_category_name']=category_name
        row['resolved_category_policy_error']=error
        row['resolved_policy_scope']=policy_scope
        rule, rule_category_name, rule_error=resolve_category_rule(
            row.get('title'), row.get('topic'), categories)
        row['resolved_comparison_rule']=rule
        row['resolved_comparison_rule_category']=rule_category_name
        row['resolved_comparison_rule_error']=rule_error
    return comparison_index(rows)


def comparison_index(rows, now=None):
    now=now or datetime.now(timezone.utc).replace(tzinfo=None)
    groups=defaultdict(list);by_id={};seen=set()
    rows=sorted(rows,key=lambda r:r['id'],reverse=True)
    merchant_title_keys=defaultdict(set);title_platforms=defaultdict(set);prepared={}
    for row in rows:
        identity=merchant_identity(row)
        policy=row.get('resolved_category_policy')
        if not isinstance(policy,dict):
            policy, _=decode_policy(row.get('category_policy_json'))
        rule=row.get('resolved_comparison_rule') or default_rule()
        body=json.loads(row.get('detail_json') or '{}').get('conditions') or row.get('snippet') or ''
        brief=offer_summary(row.get('title',''),row.get('url',''),body,metadata=row.get('metadata_json') or '{}')
        title_identity, variant, measure, measure_error=normalized_spec(
            row.get('title',''),brief.get('selected_spec'),rule)
        comparable_title_key=title_identity if rule['unit_mode']!='exact_spec' and not measure_error else product_key(row.get('title',''))
        prepared[row['id']]=(brief,rule,title_identity,variant,measure,measure_error,comparable_title_key)
        if identity['key'].startswith(('jd:','taobao:','pdd:','suning:','vip:')):
            merchant_title_keys[identity['key']].add(comparable_title_key)
            title_platforms[comparable_title_key].add(identity['key'].split(':',1)[0])
    for row in rows:
        identity=merchant_identity(row)
        policy=row.get('resolved_category_policy')
        policy_error=row.get('resolved_category_policy_error')
        policy_scope=row.get('resolved_policy_scope','topic')
        if not isinstance(policy,dict):
            policy, policy_error=decode_policy(row.get('category_policy_json'))
        if row['url'] in seen:continue
        seen.add(row['url'])
        brief,rule,title_identity,variant,measure,measure_error,comparable_title_key=prepared[row['id']]
        body=json.loads(row.get('detail_json') or '{}').get('conditions') or row.get('snippet') or ''
        title_key=comparable_title_key
        key=comparison_group_key(row,identity,brief,merchant_title_keys.get(identity['key'],()),
                                 title_platforms.get(title_key,()),title_key,rule['identity_mode'])
        by_id[row['id']]=key
        if brief['kind'] not in ('purchase','unknown'):continue
        total=brief.get('total_cents');quantity=brief.get('quantity')
        # Avoid substituting a title per-unit price for a cash order total.
        problems=[];current_problems=[]
        if identity['conflict']:problems.append(identity['label'])
        conditional=(row.get('auto_state')=='conditional' or bool(brief['promotions'])
                     or bool(brief['qualifications']) or bool(brief['audit']['risks']))
        merchant_id=identity['key'].startswith(('jd:','taobao:','pdd:','suning:','vip:')) and not identity['conflict']
        if conditional and not merchant_id:
            problems.append('条件优惠缺商家商品ID，无法证明优惠与该商品绑定')
        optimization_gaps=[]
        if conditional and brief['audit']['plan']['state']!='conditional_match':
            optimization_gaps.append('扣减方案缺字段、复杂规则或尚未复算吻合')
        if re.search(r'凑单|返现|返后|积分|淘金币|概率|随机领|部分账号|预售|定金',body):optimization_gaps.append('含额外支出、非现金权益或不确定条件')
        if json.loads(row.get('metadata_json') or '{}').get('content_truncated'):problems.append('原文正文被截断，可能缺少条件')
        if brief.get('error'):problems.append(brief['error'])
        selected_spec_key=specification_key(brief.get('selected_spec'))
        if not selected_spec_key:problems.append('缺明确选中规格，不能比较商品或数量方案')
        if row.get('resolved_comparison_rule_error'):
            problems.append('品类身份/计价规则配置无效')
        if rule['unit_mode']!='exact_spec' and measure_error:
            problems.append(measure_error)
        if total is None or not quantity:problems.append('缺整单金额或购买件数')
        if row.get('auto_state') not in ('observed','conditional'):
            problems.append('当前原文核验或时效未通过');current_problems.append('当前原文核验或时效未通过')
        if row.get('status') in ('ignored','expired'):
            problems.append('已忽略或失效');current_problems.append('已忽略或失效')
        if brief['audit']['plan']['state']=='conditional_mismatch':problems.append('优惠试算与报价不符')
        published=moment(row.get('published_at'));seen_at=moment(row.get('last_seen_at'));success=moment(row.get('last_success'))
        # Category rules can tighten, but cannot extend the source adapter's ceiling.
        ttl=timedelta(minutes=min(2*(row.get('interval_minutes') or 60)+15,120,
                                  policy['max_source_age_minutes']))
        if not publication_is_current(row,now):
            problems.append('原文过期或缺可靠时间');current_problems.append('原文过期或缺可靠时间')
        if not row.get('enabled') or row.get('source_status')!='healthy' or any(not t or not timedelta(0)<=now-t<=ttl for t in (seen_at,success)):
            problems.append('来源或采集快照已失效');current_problems.append('来源或采集快照已失效')
        # Unknown restrictions remain unknown; differing declared qualifications never compete.
        conditions=tuple(sorted(brief['qualifications']+[risk for risk in brief['audit']['risks'] if risk!='需要指定入口或操作']))
        restrictive=re.findall(r'[^，,。；;\n]{0,30}(?:限地区|限城市|限\w{1,4}地区|仅限|部分用户|部分账号|限时)[^，,。；;\n]{0,30}',body)
        conditions+=tuple(sorted(restrictive))
        identity_label=('原文明示规格的同标题候选（非商家SKU核验）'
                        if key.startswith('title-candidate:') else identity['label'])
        comparison_measure=measure*quantity if measure is not None and quantity else None
        normalized_unit=(Fraction(total,1)/Fraction(comparison_measure)
                         if rule['unit_mode']!='exact_spec' and total is not None and comparison_measure else None)
        normalized_variant=variant if rule['unit_mode']!='exact_spec' else selected_spec_key
        compare_quantity=quantity if rule['unit_mode']=='exact_spec' else None
        item=dict(identity_label=identity_label,merchant_key=identity['key'],optimization_gaps=optimization_gaps,id=row['id'],url=row['url'],platform=row.get('platform',''),title=row['title'],
                  total_cents=total,quantity=quantity,unit=Fraction(total,quantity) if total is not None and quantity else None,
                  normalized_unit_price=normalized_unit, normalized_base_unit=('g' if rule['unit_mode']=='mass' else 'ml' if rule['unit_mode']=='volume' else rule['count_unit'] if rule['unit_mode']=='count' else None),
                  unit_cents=round(Fraction(total,quantity)) if total is not None and quantity else None,
                  published_at=row.get('published_at'),conditions='；'.join(conditions) or '原文未注明资格限制（不代表人人适用）',
                  partition=(key,normalized_variant,conditions,compare_quantity,
                             row.get('resolved_category_name') or row.get('topic') or 'other',rule['unit_mode']),
                  problems=list(dict.fromkeys(problems)), source_current=not current_problems,
                  shipping_cents=brief['audit']['plan']['shipping_cents'], policy=policy,
                  policy_scope=policy_scope, resolved_category_name=row.get('resolved_category_name'),
                  unit_mode=rule['unit_mode'],
                  policy_error=policy_error)
        groups[key].append(item)
    result={}
    for oid,key in by_id.items():
        items=groups.get(key,[]);target=next((i for i in items if i['id']==oid),None)
        if target is None:
            result[oid]=dict(items=items,peers=0,best_id=None,saving_cents=0,message='旧快照或尚无完整商品方案',identity_label='',quantity_options=None)
            continue
        peers=[i for i in items if i['partition']==target['partition'] and not i['problems']
               and not i['optimization_gaps']]
        if key.startswith('title-candidate:'):
            target_platform=target['merchant_key'].split(':',1)[0]
            # Count this listing once per marketplace; copied source posts do not
            # create extra independent offers for the same product ID.
            by_listing={}
            for item in peers:
                if item['merchant_key'].startswith(('jd:','taobao:','pdd:','suning:','vip:')):
                    by_listing.setdefault(item['merchant_key'],item)
            peers=[item for item in by_listing.values()
                   if item['merchant_key']==target['merchant_key']
                   or item['merchant_key'].split(':',1)[0]!=target_platform]
        # Several deal-feed posts that resolve to the same merchant SKU are
        # repeated claims about one listing, not independent buying options.
        # Keep them visible for audit, but do not use them as a savings baseline.
        merchant_keys={i['merchant_key'] for i in items
                       if i['merchant_key'].startswith(('jd:','taobao:','pdd:','suning:','vip:'))}
        merchant_platforms={key.split(':',1)[0] for key in merchant_keys}
        has_unidentified=any(not i['merchant_key'].startswith(('jd:','taobao:','pdd:','suning:','vip:')) for i in items)
        same_merchant_listing=bool(merchant_keys) and (len(merchant_keys)==1 or len(merchant_platforms)==1 or has_unidentified)
        required_offers=target['policy']['minimum_comparable_offers']
        metric=lambda i:i['normalized_unit_price'] if i['normalized_unit_price'] is not None else i['total_cents']
        best=(min(peers,key=lambda i:(metric(i),-i['id']))
              if len(peers)>=required_offers and not same_merchant_listing else None)
        difference=metric(target)-metric(best) if best and not target['problems'] else Fraction(0)
        saving=round(difference)
        if best and not target['problems']:
            candidate_note=('按完全一致标题、原文明示报价规格、数量与资格形成候选，商家SKU未核验；'
                            if key.startswith('title-candidate:') else '')
            peer_prices=[metric(item) for item in peers]
            basis=(f"每{target['normalized_base_unit']}" if target['normalized_base_unit'] else '整单')
            if min(peer_prices)==max(peer_prices):
                message=(candidate_note+'采集原文声称价相同，尚未发现更低来源声称价；这不是商家核价或捡漏结论'
                         if target['normalized_unit_price'] is None else
                         candidate_note+f'按{basis}归一后采集原文声称价相同，尚未发现更低价；这不是商家核价或捡漏结论')
            elif difference <= 0:
                gap=max(peer_prices)-metric(target)
                pct=round(gap*10000/metric(target))/100 if metric(target) else 0
                gap_yuan=f'{float(gap)/100:.4f}' if target['normalized_unit_price'] is not None else f'{float(gap)/100:.2f}'
                message=(candidate_note+f'本来源声称价为候选中最低；较高来源声称价高{gap_yuan}元（约{pct:.2f}%），未核实商家实际价，不代表省钱或捡漏'
                         if target['normalized_unit_price'] is None else
                         candidate_note+f'本来源按{basis}归一后最低；高{gap_yuan}元（约{pct:.2f}%），是原文声称价，不代表实际省钱或捡漏')
            else:
                pct=round(difference*10000/metric(best))/100 if metric(best) else 0
                gap_yuan=f'{float(difference)/100:.4f}' if target['normalized_unit_price'] is not None else f'{float(difference)/100:.2f}'
                message=(candidate_note+f'采集原文声称价相差{gap_yuan}元（约{pct:.2f}%）；未核实商家实际价，不代表省钱或捡漏'
                         if target['normalized_unit_price'] is None else
                         candidate_note+f'按{basis}归一后来源声称价相差{gap_yuan}元（约{pct:.2f}%）；未核实商家价，不代表省钱或捡漏')
        elif same_merchant_listing:
            if len(merchant_keys)==1:
                message='多个采集线索指向同一商家商品ID，是同一商品页的价格声称，不是多个购买方案；缺独立基准，不能判断省钱或捡漏'
            else:
                message='同一购买平台内的不同商品ID尚不能证明是同一商品；不合并为独立跨渠道基准'
        elif target['problems']:message='暂不能参与当前比较：'+ '；'.join(target['problems'])
        else:message='当前没有第二个同商品候选、同数量及已提及资格的可比报价'
        ordered=sorted(items,key=lambda i:(bool(i['problems']),i['quantity'] or 10**9,i['total_cents'] if i['total_cents'] is not None else 10**12))
        ordered=[dict(i,comparison_note='与本方案数量、规格或资格不同，仅供对照' if i['partition']!=target['partition'] else '') for i in ordered]
        quantity_items=items
        if key.startswith('title-candidate:'):
            unique_listings={}
            for item in items:
                if item['merchant_key'].startswith(('jd:','taobao:','pdd:','suning:','vip:')):
                    unique_listings.setdefault(item['merchant_key'],item)
            quantity_items=list(unique_listings.values())
        result[oid]=dict(identity_label=target['identity_label'],quantity_options=quantity_options(quantity_items,target),items=ordered,
                        peers=len(peers) if not same_merchant_listing else min(len(peers),1),
                        required_comparable_offers=required_offers, policy=target['policy'],
                        unit_mode=target['unit_mode'], normalized_base_unit=target['normalized_base_unit'],
                        policy_scope=target['policy_scope'], resolved_category_name=target['resolved_category_name'],
                        policy_error=target['policy_error'], comparison_basis='source_claim',
                        saving_unit=target['normalized_base_unit'] or 'order_total',
                        best_id=best['id'] if best else None, saving_cents=saving,
                        message=message)
    return result
