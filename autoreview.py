"""Automatic public-evidence triage. Never writes checkout or transaction evidence."""
import json
import html
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from db import connect
from pricing import discount_audit, purchase_terms, promotion_mentions
from xianbao import extract_links
from offer import resource_kind, resource_topic, RESOURCE_LABELS

LABELS = {
    'excluded': '自动排除', 'stale': '时效不满足',
    'source_unavailable': '等待来源恢复', 'queued': '自动复查排队中',
    'retry': '访问失败／自动退避', 'missing_time': '缺原文发布时间',
    'missing_price': '缺结构化价格来源', 'conflict': '原文报价／规格冲突', 'observed': '公开报价已提取',
    'activity': '活动线索／条件待核',
    'conditional': '条件报价（含优惠／资格限制）',
}


def moment(value):
    try:
        d = datetime.fromisoformat(value or '')
        return d.astimezone(timezone.utc).replace(tzinfo=None) if d.tzinfo else d
    except (ValueError, TypeError):
        return None


def stamp(value):
    return value.strftime('%Y-%m-%d %H:%M:%S')


def supported(url):
    u = urlparse(url or '')
    if u.scheme == 'https' and u.netloc == 'new.ixbk.net' and not u.query:
        return re.fullmatch(r'/[a-zA-Z0-9_-]+/\d+\.html',u.path) is not None
    return (u.scheme == 'https' and u.netloc == 'www.smzdm.com'
            and re.fullmatch(r'/p/\d+/', u.path) is not None and not u.query)


def extract(text):
    """A single explicit yuan price only; quantities/coupons cannot become prices."""
    amounts = re.findall(r'(?:[¥￥]\s*(\d+(?:\.\d{1,2})?)(?![\d.])|(?<![\d.])(\d+(?:\.\d{1,2})?)\s*元(?=$|[\s，。,；;()（）/]|起|包邮|到手))', text)
    values = {int(Decimal(a or b) * 100) for a, b in amounts}
    ambiguous = re.search(r'\d+(?:\.\d+)?\s*元?\s*[-~～至]\s*\d+(?:\.\d+)?\s*元|\d\s*[万亿]元', text)
    price = next(iter(values)) if len(values) == 1 and not ambiguous else None
    specs = list(dict.fromkeys(re.findall(
        r'\d+(?:\.\d+)?\s*(?:毫升|ml|mL|kg|克|g|升|层|抽|小包|小袋|包|卷|提|箱|盒|片|GB|TB)(?![a-zA-Z邮])', text)))
    terms = [term for term in ('新客','新人','首单','首购','88VIP','会员','省钱卡','直播','用券','券后','返现','返后','礼金','限购','起','每月','/月','/M','多人团','淘金币','补贴','移动端','折合','需买') if term.lower() in text.lower()]
    return price, ' · '.join(specs), '、'.join(terms)


def structured_spec(text):
    """Return only an explicit, single product specification from source text."""
    text=unicodedata.normalize('NFKC',html.unescape(text or ''))
    if re.search(r'任选|多规格|多款|随机|NB\s*/\s*S\s*/\s*M\s*/\s*L|S\s*/\s*M\s*/\s*L',text,re.I):
        return ''
    option_unit=r'(毫升|ml|千克|kg|克|g|升|L|斤|层|抽|小包|小袋|包|卷|提|箱|盒|片|支|袋|瓶|罐|套|枚|只|个|条|块|双|cm|mm|英寸|寸|GB|TB|G)'
    alternatives=re.search(r'(?<![\d.])(\d+(?:\.\d+)?)\s*'+option_unit+
                            r'\s*/\s*(\d+(?:\.\d+)?)\s*\2(?![A-Za-z邮])',text,re.I)
    if alternatives and Decimal(alternatives[1]) != Decimal(alternatives[3]):
        return ''
    unit=r'(?:毫升|ml|kg|千克|克|g|升|L|斤|层|抽|小包|小袋|包|卷|提|箱|盒|片|支|袋|瓶|罐|套|枚|只|个|条|块|双|cm|mm|英寸|寸|GB|TB|G)'
    tokens=re.findall(r'(?<![\d.])\d+(?:\.\d+)?\s*'+unit,text,re.I)
    tokens+=re.findall(r'(?<![A-Za-z])(?:NB|S|M|L|XL|XXL|XXXL)\s*\d+(?!\d)',text,re.I)
    normalized=[]
    for token in tokens:
        token=re.sub(r'\s+','',token)
        if token.lower().endswith('ml'):token=token[:-2]+'ml'
        if token not in normalized:normalized.append(token)
    return ' × '.join(normalized[:8])


def price_conflicts(price, conditions):
    paid = {int(Decimal(v)*100) for v in re.findall(r'实付(?:低至)?\s*(\d+(?:\.\d{1,2})?)元', conditions)}
    quantity = re.search(r'需买(\d+)件', conditions)
    multiplier = int(quantity[1]) if quantity else 1
    return bool(price is not None and paid and all(abs(v-price*multiplier)>multiplier for v in paid))


def selected_spec_conflict(title, body):
    """Compare only an explicitly selected-price specification, not marketing quantities."""
    selected = re.search(r'该价格商品规格\s*[:：]\s*(.+?)(?=天猫|京东|拼多多|淘宝|苏宁|$)', body)
    if not selected:
        return None
    def quantities(text):
        result = {}
        for number, unit in re.findall(r'(?<![\d.])(\d+)\s*(包|提|卷|抽|层|片|盒|支|袋)', text):
            result.setdefault(unit, set()).add(int(number))
        return result
    headline_units, selected_units = quantities(title), quantities(selected[1])
    for unit in sorted(headline_units.keys() & selected_units.keys()):
        a, b = headline_units[unit], selected_units[unit]
        if len(b) > 1:
            return f"该价格选中规格包含多个{unit}数量选项，无法确定对应包装，不能比较"
        if len(a) > 1 and b.isdisjoint(a):
            return f"标题列出多个{unit}规格，该价格选中 {next(iter(b))}{unit} 不在标题选项中；数量口径冲突"
        if len(a) == len(b) == 1 and a != b:
            return f"标题标注 {next(iter(a))}{unit}，该报价选中规格标注 {next(iter(b))}{unit}；数量口径冲突，不能按标题折算单价或利润"
    return None


def public_offer(url, body, title=''):
    """Normalize explicit Guangdiu feed purchase plans; never infer checkout or sale evidence."""
    u = urlparse(url or '')
    supported_plan = ((u.hostname in ('guangdiu.com', 'www.guangdiu.com') and u.path == '/detail.php')
                      or (u.hostname == 'new.ixbk.net' and re.fullmatch(r'/[a-zA-Z0-9_-]+/\d+\.html',u.path)))
    body = (body or '').split('商品介绍')[0].split('品牌介绍')[0]
    fields = purchase_terms(body)
    # Other sources may provide the same explicit full purchase plan.
    if not supported_plan and not (fields['total'] and fields['quantity']):
        return {}
    unit, total, counts = (set(fields[key]) for key in ('unit','total','quantity'))
    if title and resource_kind(title,body)=='purchase':
        title_price,_,_=extract(title)
        if title_price is not None and not total:total.add(title_price)
        if not counts:
            title_counts={int(v) for v in re.findall(r'(?:购买|需买|下单|拍|买)\s*(\d+)\s*件(?!\s*(?:返|送|赠|享|折))',title)}
            counts=title_counts or {1}
    selected = re.search(r'该价格商品规格\s*[:：]\s*(.+?)(?=天猫|京东|拼多多|淘宝|苏宁|$)', body)
    store = re.search(r'店铺\s*[:：]?\s*(.+?)\s*,商品面价', body)
    result = dict(unit_cents=next(iter(unit)) if len(unit)==1 else None,
                  total_cents=next(iter(total)) if len(total)==1 else None,
                  quantity=next(iter(counts)) if len(counts)==1 and next(iter(counts))>0 else None,
                  selected_spec=selected[1].strip() if selected else (structured_spec(title) or structured_spec(body)),
                  store=store[1].strip() if store else '', error='')
    if any(len(values)>1 for values in (unit,total,counts)):
        result['error'] = '原文购买方案包含多个单价、总价或件数，无法确定同一报价口径'
    elif (result['unit_cents'] is not None and result['total_cents'] is not None and result['quantity']
          and abs(result['unit_cents']*result['quantity']-result['total_cents'])>result['quantity']):
        result['error'] = '原文单件价乘购买件数与整单报价不一致'
    return result


def _price_status(title, body, metadata, total_cents):
    """Explain why no single purchase total is available; never guess among source prices."""
    if total_cents is not None:
        return dict(label='', note='', signals=[])
    text = html.unescape((title or '') + '\n' + (body or ''))
    pattern = r'(?:[¥￥]\s*(\d+(?:\.\d{1,2})?)(?![\d.])|(?<![\d.])(\d+(?:\.\d{1,2})?)\s*元(?!起))'
    signals = list(dict.fromkeys(int(Decimal(a or b) * 100) for a,b in re.findall(pattern,text)))
    origin = json.loads(metadata or '{}')
    source_hint = None
    raw_hint = origin.get('source_price')
    if raw_hint is not None and not isinstance(raw_hint, bool):
        try:
            candidate = int(Decimal(str(raw_hint)) * 100)
            if candidate > 0:
                source_hint = candidate
        except Exception:
            pass
    if len(signals) > 1:
        label = '多组金额待拆分'
        note = '原文金额线索：' + '、'.join(f'¥{Decimal(value)/100:.2f}' for value in signals[:4]) + '；尚未对应到唯一购买方案。'
    elif signals:
        label = '金额口径待确认'
        note = f'原文提及 ¥{Decimal(signals[0])/100:.2f}，但无法确认是单件价还是整单价。'
    elif source_hint is not None:
        label = '来源价格线索待确认'
        note = f'来源接口记录 ¥{Decimal(source_hint)/100:.2f}；尚未确认对应商品规格与购买口径。'
    else:
        label = '金额未提取'
        note = '标题和已采集正文中没有提取到明确人民币报价。'
    return dict(label=label, note=note, signals=signals, source_hint_cents=source_hint)


def offer_summary(title, url, body, conditions='', metadata='{}', detail_json=None, sync_meta=None):
    """Readable excerpts, not a new price calculation or eligibility decision."""
    detail = json.loads(detail_json or '{}')
    body = detail.get('conditions') or body or ''
    text = title + '\n' + body + '\n' + (conditions or '')
    offer = public_offer(url, body, title)
    display_unit_cents = offer.get('unit_cents')
    if display_unit_cents is None and offer.get('total_cents') is not None and offer.get('quantity'):
        display_unit_cents = round(offer['total_cents'] / offer['quantity'])
    promotion = promotion_mentions(text)
    qualifications = [x for x in ('88VIP','PLUS','新客','首单','会员','省钱卡','直播','移动端','多人团','淘金币','返现','补贴') if x in text]
    origin = json.loads(metadata or '{}')
    price_status = _price_status(title, body, metadata, offer.get('total_cents'))
    links = list(dict.fromkeys(detail.get('activity_links',[]) + origin.get('activity_links',[])))
    platforms = list(dict.fromkeys(re.findall(r'抖音商城|抖音小时达|美团闪购|淘宝闪购|京东秒送|京东到家|美团外卖|饿了么|京东|天猫|拼多多|苏宁|唯品会|淘宝|美团|抖音|高德|支付宝', offer.get('store','') + ' ' + title + ' ' + body + ' ' + origin.get('source_category',''))))
    previous = re.search(r'降价前售价为\s*(\d+(?:\.\d{1,2})?)元', body)
    name = re.sub(r'^(?:限移动端|88VIP会员)[：:]\s*', '', title)
    # Only remove an explicit trailing quote, never a model number such as “荣耀600 元气版”.
    name = re.sub(r'\s+(?:券后)?\d+(?:\.\d{1,2})?元(?:[/／]件)?(?:[（(].*[）)])?$', '', name)
    sync_meta = sync_meta or {}
    return dict(offer, title=name, display_unit_cents=display_unit_cents, platforms=' / '.join(platforms),
                price_status=price_status, kind=resource_kind(title,body), source_category=origin.get('source_category',''),
                resolved_links=origin.get('resolved_link_evidence',[]), activity_links=links, origin_url=detail.get('origin_url',''), external_checks=detail.get('external_checks',[]), audit=discount_audit(title,body),
                promotions=promotion, qualifications=qualifications,
                previous_cents=int(Decimal(previous[1])*100) if previous else None,
                promotion_sync=promotion_sync_status(title, body, detail_json,
                    sync_meta.get('detail_checked_at'), sync_meta.get('detail_error'), sync_meta.get('checked_at')))


def promotion_sync_status(title, body, detail_json=None, detail_checked_at=None, detail_error=None, checked_at=None):
    """Explain source-sync and parser outcome separately; no condition is not a sync state."""
    detail = json.loads(detail_json or '{}')
    detail_body = detail.get('conditions') or ''
    source_text = '\n'.join(part for part in (title, body, detail_body) if part)
    found = promotion_mentions(source_text)
    qualifications = [x for x in ('88VIP','PLUS','新客','首单','会员','省钱卡','直播','移动端','多人团','淘金币','返现','补贴') if x in source_text]
    detail_expired = False
    if detail_checked_at and not detail_error:
        try:
            checked = datetime.fromisoformat(detail_checked_at)
            checked = checked.replace(tzinfo=timezone.utc) if checked.tzinfo is None else checked.astimezone(timezone.utc)
            detail_expired = not timedelta(0) <= datetime.now(timezone.utc) - checked <= timedelta(minutes=30)
        except (TypeError, ValueError):
            detail_expired = True
    if detail_error:
        state, label = 'detail_sync_failed', '详情同步失败'
        explanation = f'来源摘要已检查；详情读取失败：{detail_error}'
    elif detail_expired:
        state, label = 'detail_sync_stale', '详情同步结果已过期'
        local_checked = checked.astimezone(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')
        explanation = (f'上次详情同步时间：{local_checked}；'
                      + ('曾识别到优惠提及，但当前没有新详情结果。' if found or qualifications
                         else '上次解析未发现明确优惠条件，但当前没有新详情结果。'))
    elif found or qualifications:
        state, label = 'conditions_found', '已同步并识别优惠条件'
        explanation = f'识别到 {len(found)} 条优惠提及、{len(qualifications)} 条资格/入口提示；不代表已确认可用或可叠加。'
    elif detail_checked_at:
        state, label = 'detail_synced_empty', '详情已同步，未识别到明确优惠条件'
        explanation = '详情正文已读取；当前解析器未找到明确券额、折扣或资格条件。'
    elif body:
        state, label = 'summary_synced_empty', '来源摘要已同步，未识别到明确优惠条件'
        explanation = '目前只有来源摘要；详情页尚未成功同步，摘要中未解析出明确优惠条件。'
    else:
        state, label = 'not_synced', '尚未同步到来源正文'
        explanation = '当前只有标题或链接，没有可供优惠解析的来源正文。'
    return dict(state=state, label=label, explanation=explanation,
                source_checked_at=checked_at or '', detail_checked_at=detail_checked_at or '',
                detail_error=detail_error or '', promotions=found, qualifications=qualifications)


def parse_xianbao_detail(html):
    soup = BeautifulSoup(html,'html.parser')
    headline = soup.select_one('article.art-main h1.art-title')
    content = soup.select_one('article.art-main .article-content')
    if not headline or not content:
        raise ValueError('线报详情结构不匹配；未把登录页或评论当作活动规则')
    title = headline.get_text(' ',strip=True)
    body = content.get_text('\n',strip=True)[:12000]
    price, _, _ = extract(title)
    node = soup.select_one('article.art-main time.time')
    published = None
    if node:
        try:
            date = datetime.strptime(node.get('title',''),'%Y-%m-%d %H:%M:%S')
            published = stamp(date.replace(tzinfo=timezone(timedelta(hours=8))).astimezone(timezone.utc))
        except ValueError: pass
    origin = soup.select_one('article.art-main .art-copyright .addr + a[href]')
    origin_links = extract_links(str(origin)) if origin else []
    return dict(title=title,advertised_cents=price,conditions=body,published_at=published,
                evidence=title+'\n'+body,activity_links=extract_links(str(content)),
                origin_url=origin_links[0] if origin_links else '')


def parse_detail(html):
    """Only SMZDM's article headline/price/byline description; no comments or cards."""
    soup = BeautifulSoup(html, 'html.parser')
    headline = soup.select_one('h1.J_title')
    if not headline:
        raise ValueError('详情结构不匹配或遇到验证页；未提取价格')
    title = headline.get_text(' ', strip=True)
    price_node = soup.select_one('.info .price .price-large .num')
    raw = price_node.get_text(strip=True) if price_node else ''
    price = int(Decimal(raw) * 100) if re.fullmatch(r'\d+(?:\.\d{1,2})?', raw) else None
    descriptions = soup.select('.baoliao-block [itemprop="description"]')
    body = '\n'.join(el.get_text(' ', strip=True) for el in descriptions)[:3000]
    price_note = soup.select_one('.info .price .price-desc')
    conditions = '\n'.join(filter(None, [price_note.get_text(' ', strip=True) if price_note else '', body]))
    published = None
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.get_text())
            if not isinstance(data, dict):
                continue
            raw_date = data.get('pubDate') or data.get('datePublished')
            if raw_date:
                d = datetime.fromisoformat(raw_date)
                if not d.tzinfo:
                    d = d.replace(tzinfo=timezone(timedelta(hours=8)))
                published = stamp(d.astimezone(timezone.utc))
                break
        except (ValueError, TypeError):
            continue
    # Publication time is deliberately never replaced with upDate or fetch time.
    _, spec, _ = extract(title)
    _, _, terms = extract(title + ' ' + conditions)
    conflict = price_conflicts(price, conditions)
    return dict(title=title, advertised_cents=price, specification=spec,
                conditions=(terms + '\n' + conditions).strip(), published_at=published,
                evidence=(title + '\n' + conditions)[:3500], conflict=conflict)


def classify(row, now, duplicate=False):
    price, spec, terms = extract(row['title'])
    snippet = (row.get('snippet') or '')[:12000]
    kind = resource_kind(row['title'],snippet)
    offer = public_offer(row['url'], snippet, row['title'])
    detail = json.loads(row['detail_json']) if row.get('detail_json') else {}
    if detail:
        title_price, spec, _ = extract(detail['title'])
        price = detail['advertised_cents'] if detail['advertised_cents'] is not None else title_price
        terms = detail['conditions']
        offer = public_offer(row['url'], terms, detail.get('title') or row['title'])
        if offer.get('unit_cents') is not None:
            price = offer['unit_cents']
        elif offer.get('total_cents') is not None:
            price = offer['total_cents']
    else:
        _, _, body_terms = extract(snippet)
        terms = '\n'.join(filter(None, [terms, body_terms, snippet]))
        if offer.get('unit_cents') is not None:
            price = offer['unit_cents']
        elif offer.get('total_cents') is not None:
            price = offer['total_cents']
    kind = resource_kind(detail.get('title') or row['title'], detail.get('conditions') or snippet)
    evidence = '\n'.join(filter(None, [row['title'], snippet, detail.get('evidence')]))[:6000]
    result = dict(advertised_cents=price, specification=spec, conditions=terms, evidence=evidence)
    if kind not in ('purchase','unknown'):
        # A coupon face value, rebate price or lottery prize is never a cash buy price.
        result['advertised_cents'] = None

    def state(key, reason):
        return dict(result, state=key, reason=reason)

    if row['status'] in ('ignored', 'expired'):
        return state('excluded', '已被标记忽略或失效')
    if duplicate:
        return state('excluded', '同一原文已有更新或其他栏目快照；保留历史，不重复展示')
    if re.search(r'已出|已售|售罄|已过期|已结束', row['title'] + ' ' + detail.get('title','')):
        return state('excluded', '原文标题标明已出、已售或活动结束')
    if re.search(r'^[\s【\[（(「]*(?:求购|求助|收购|收一个|求一个)', row['title']):
        return state('excluded', '求购或求助内容，不能作为可购买货源')
    conflict = selected_spec_conflict(row['title'], snippet + '\n' + detail.get('conditions',''))
    if conflict:
        return state('conflict', conflict)
    if offer.get('error'):
        return state('conflict', offer['error'])
    title_amounts = {int(Decimal(v)*100) for v in re.findall(r'(\d+(?:\.\d{1,2})?)元', row['title'])}
    if offer.get('unit_cents') is not None and len(title_amounts)==1:
        if next(iter(title_amounts)) != offer['unit_cents']:
            return state('conflict', '标题报价与正文明确单件价不一致，不能用于价格比较')
    published = moment(row['published_at'] or detail.get('published_at'))
    if published and not timedelta(0) <= now - published <= timedelta(hours=2):
        return state('stale', '原文超过 2 小时或时间在未来；自动留档，不列作当前机会')
    ttl = timedelta(minutes=min(2 * row['interval_minutes'] + 15, 120))
    seen, success = moment(row['last_seen_at']), moment(row['last_success'])
    if not row['enabled'] or row['source_status'] != 'healthy' or not seen or not success or not (timedelta(0) <= now-seen <= ttl and timedelta(0) <= now-success <= ttl):
        return state('source_unavailable', '来源暂停、抓取失败或快照超时；等待采集恢复后自动重算')
    if supported(row['url']):
        if row.get('detail_error'):
            return state('retry', '详情访问失败；按退避时间自动重试，不认定价格有效')
        checked = moment(row.get('detail_checked_at'))
        if not checked or now - checked > timedelta(minutes=30):
            return state('queued', '自动读取原文发布时间、公开报价和适用条件')
    if not published:
        return state('missing_time', '上游未提供可靠原文时间；隔离留档，采集更新后自动重算')
    audit = discount_audit(row['title'], detail.get('conditions') or snippet)
    if audit['formula']['state'] == 'conflict':
        return state('conflict', '原文扣减计算式算术不一致；不能认定报价成立')
    if detail and supported(row['url']) and urlparse(row['url']).hostname == 'www.smzdm.com' and price_conflicts(price, terms):
        return state('conflict', '标题区报价与正文实付描述不一致；自动隔离，等待原文更新后复查')
    if kind not in ('purchase','unknown'):
        return state('activity', RESOURCE_LABELS[kind] + '；需核对支出、资格、名额与截止时间，未认定免费或确定收益')
    if price is None:
        return state('missing_price', '没有唯一明确的人民币报价；需接入该来源的结构化价格能力')
    if audit['plan']['state'] == 'conditional_mismatch':
        return state('conditional', audit['plan']['reason'])
    if audit['uncertain']:
        return state('conditional', '；'.join(audit['risks'] or ['来源优惠扣减条件需拆解']) + '；仅按公开原文记录条件，不把未知扣减当成已生效价格')
    return state('observed', '已提取来源公开报价；还需核对完整规格、数量口径、可比行情与费用依据，再判断低价及预计利润')


def review_all():
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with connect() as db:
        rows = db.execute('''SELECT o.*, e.snippet,e.metadata_json,e.published_at,e.last_seen_at,s.enabled,
            s.status AS source_status,s.interval_minutes,s.last_success,
            a.detail_json,a.detail_checked_at,a.detail_error,
            EXISTS(SELECT 1 FROM opportunities newer JOIN sources ns ON ns.id=newer.source_id
                   JOIN events ne ON ne.id=newer.event_id
                   WHERE newer.url=o.url AND
                   ((newer.source_id=o.source_id AND newer.id>o.id) OR (s.method='xianbao' AND ns.method='xianbao'
                    AND ns.enabled=1 AND ns.status='healthy' AND
                    (ne.last_seen_at>e.last_seen_at OR (ne.last_seen_at=e.last_seen_at AND newer.id>o.id))))) AS duplicate
            FROM opportunities o JOIN events e ON e.id=o.event_id
            JOIN sources s ON s.id=o.source_id LEFT JOIN auto_reviews a ON a.opportunity_id=o.id''').fetchall()
        counts = {}
        for item in rows:
            row = dict(item)
            metadata = json.loads(row.get('metadata_json') or '{}')
            detail = json.loads(row.get('detail_json') or '{}')
            db.execute('UPDATE opportunities SET resource_kind=?,topic=? WHERE id=?',
                       (resource_kind(detail.get('title') or row['title'],detail.get('conditions') or row['snippet']),
                        resource_topic(row['title'],metadata.get('source_category','')),row['id']))
            result = classify(row, now, bool(row['duplicate']))
            db.execute('''INSERT INTO auto_reviews
                (opportunity_id,state,reason,advertised_cents,specification,conditions,evidence)
                VALUES(:id,:state,:reason,:advertised_cents,:specification,:conditions,:evidence)
                ON CONFLICT(opportunity_id) DO UPDATE SET state=excluded.state,reason=excluded.reason,
                advertised_cents=excluded.advertised_cents,specification=excluded.specification,
                conditions=excluded.conditions,evidence=excluded.evidence,checked_at=CURRENT_TIMESTAMP''', dict(result,id=row['id']))
            counts[result['state']] = counts.get(result['state'], 0) + 1
        return counts


def run_cycle(limit=6, opportunity_id=None):
    """Bounded network batch; SQLite lease prevents overlapping workers from probing twice."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with connect() as db:
        db.execute('INSERT OR IGNORE INTO review_runs(id) VALUES(1)')
        leased = db.execute('''UPDATE review_runs SET started_at=?,finished_at=NULL
            WHERE id=1 AND (finished_at IS NOT NULL OR started_at IS NULL OR started_at<?)''',
            (stamp(now), stamp(now-timedelta(minutes=10)))).rowcount
    if not leased:
        return {'busy': True}
    counts = review_all()
    with connect() as db:
        queue = db.execute('''SELECT a.*,o.url,o.event_id FROM auto_reviews a
            JOIN opportunities o ON o.id=a.opportunity_id
            WHERE a.state IN ('queued','retry') AND (a.next_check_at IS NULL OR a.next_check_at<=CURRENT_TIMESTAMP)
            AND (? IS NULL OR o.id=?)
            ORDER BY a.detail_checked_at IS NOT NULL,a.next_check_at,o.id DESC LIMIT ?''', (opportunity_id,opportunity_id,limit)).fetchall()
    from scanner import _fetch
    for row in queue:
        if not supported(row['url']):
            continue
        try:
            html = _fetch(row['url'])
            detail = parse_xianbao_detail(html) if urlparse(row['url']).hostname == 'new.ixbk.net' else parse_detail(html)
            previous = json.loads(row['detail_json'] or '{}')
            if previous.get('external_checks'):
                detail['external_checks'] = previous['external_checks']
            with connect() as db:
                db.execute('''UPDATE auto_reviews SET detail_json=?,detail_checked_at=CURRENT_TIMESTAMP,
                    detail_error=NULL,attempts=0,next_check_at=datetime('now','+30 minutes') WHERE opportunity_id=?''',
                    (json.dumps(detail, ensure_ascii=False), row['opportunity_id']))
                if detail['published_at']:
                    db.execute('UPDATE events SET published_at=COALESCE(published_at,?) WHERE id=?',
                               (detail['published_at'],row['event_id']))
        except Exception as exc:
            delay = min(360, 5 * 2 ** min(row['attempts'], 7))
            with connect() as db:
                db.execute('''UPDATE auto_reviews SET detail_error=?,attempts=attempts+1,
                    detail_checked_at=CURRENT_TIMESTAMP,next_check_at=datetime('now',?) WHERE opportunity_id=?''',
                    (str(exc)[:250], f'+{delay} minutes',row['opportunity_id']))
    counts = review_all()
    with connect() as db:
        db.execute('''UPDATE review_runs SET finished_at=CURRENT_TIMESTAMP,reviewed=?,probed=? WHERE id=1''',
                   (sum(counts.values()),len(queue)))
    return dict(counts=counts, probed=len(queue))
