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
from services.source_freshness import is_catalog_listing, source_snapshot_is_current
from services.apple_catalog import parse_product_detail as parse_apple_catalog_detail
from services.apple_catalog import supports_product_url as supports_apple_catalog_product
from services.benefit_claims import parse_discount_claims
from services.guangdiu_detail import parse_detail as parse_guangdiu_detail
from services.guangdiu_detail import supports_url as supports_guangdiu_detail
from services.merchant_product_page import parse_product_page as parse_merchant_product_page
from services.merchant_product_page import supports_url as supports_merchant_product_page

LABELS = {
    'excluded': '自动排除', 'stale': '时效不满足',
    'source_unavailable': '等待来源恢复', 'queued': '自动复查排队中',
    'retry': '访问失败／自动退避', 'missing_time': '缺原文发布时间',
    'missing_price': '未形成可用报价', 'conflict': '原文报价／规格冲突', 'observed': '公开报价已提取',
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
    if supports_apple_catalog_product(url):
        return True
    if supports_guangdiu_detail(url) or supports_merchant_product_page(url):
        return True
    if u.scheme == 'https' and u.netloc == 'new.ixbk.net' and not u.query:
        return re.fullmatch(r'/[a-zA-Z0-9_-]+/\d+\.html',u.path) is not None
    return (u.scheme == 'https' and u.netloc == 'www.smzdm.com'
            and re.fullmatch(r'/p/\d+/', u.path) is not None and not u.query)


def detail_probe_needed(url, title='', body='', detail=None):
    """Fetch supported pages only when current evidence cannot form a complete plan."""
    if supports_apple_catalog_product(url):
        return True
    if supports_merchant_product_page(url):
        # Keep an exact fresh catalog-card price as a public listing observation;
        # probe direct pages where the listing did not provide one or only gave
        # a starting/account-dependent estimate.
        return (extract(title)[0] is None
                or bool(re.search(r'\d+(?:\.\d+)?\s*元\s*起|预估到手价|预计到手价', title)))
    if supports_guangdiu_detail(url):
        detail = detail or {}
        plan_title = detail.get('title') or title
        plan_body = detail.get('conditions') or body
        offer = public_offer(url, plan_body, plan_title)
        return bool(offer.get('error') or offer.get('total_cents') is None
                    or not offer.get('quantity'))
    return supported(url)


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
    quantities = {int(v) for v in re.findall(r'(?:购买|需买|下单|拍|买)\s*(\d+)\s*件(?!\s*(?:返|送|赠|享|折))', conditions)}
    if len(quantities) != 1:
        return False
    multiplier = next(iter(quantities))
    return bool(price is not None and paid and all(abs(v-price*multiplier)>multiplier for v in paid))


def selected_spec_conflict(title, body):
    """Compare only an explicitly selected-price specification, not marketing quantities."""
    selected = re.search(r'该价格商品规格\s*[:：]\s*(.+?)(?=[\r\n，,。；;]|天猫|京东|拼多多|淘宝|苏宁|$)', body)
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
    # Other sources may provide the same explicit full purchase plan. A net-after-rebate claim
    # is preserved for display, but never becomes the cash order total.
    if not supported_plan and not ((fields['total'] and fields['quantity']) or fields['net_after_rebate']):
        return {}
    unit, total, counts, net_after = (set(fields[key]) for key in ('unit','total','quantity','net_after_rebate'))
    if title and resource_kind(title,body)=='purchase':
        if not counts:
            title_counts={int(v) for v in re.findall(r'(?:购买|需买|下单|拍|买)\s*(\d+)\s*件(?!\s*(?:返|送|赠|享|折))',title)}
            counts=title_counts
    selected = re.search(r'该价格商品规格\s*[:：]\s*(.+?)(?=[\r\n，,。；;]|天猫|京东|拼多多|淘宝|苏宁|$)', body)
    store = re.search(r'店铺\s*[:：]?\s*(.+?)\s*,商品面价', body)
    result = dict(unit_cents=next(iter(unit)) if len(unit)==1 else None,
                  total_cents=next(iter(total)) if len(total)==1 else None,
                  quantity=next(iter(counts)) if len(counts)==1 and next(iter(counts))>0 else None,
                  net_after_rebate_cents=next(iter(net_after)) if len(net_after)==1 else None,
                  selected_spec=selected[1].strip() if selected else '',
                  store=store[1].strip() if store else '', error='')
    if any(len(values)>1 for values in (unit,total,counts,net_after)):
        result['error'] = '原文购买方案包含多个单价、总价或件数，无法确定同一报价口径'
    elif (result['unit_cents'] is not None and result['total_cents'] is not None and result['quantity']
          and abs(result['unit_cents']*result['quantity']-result['total_cents'])>result['quantity']):
        result['error'] = '原文单件价乘购买件数与整单报价不一致'
    return result


def _price_status(title, body, metadata, total_cents, detail=None):
    """Explain why no single purchase total is available; never guess among source prices."""
    detail = detail or {}
    raw_page_amount = detail.get('page_amount_cents')
    page_amount_cents = (raw_page_amount if isinstance(raw_page_amount, int)
                         and not isinstance(raw_page_amount, bool) and raw_page_amount > 0 else None)
    page_amount_label = str(detail.get('page_amount_label') or '')
    page_amount_evidence = str(detail.get('page_amount_evidence') or '')
    raw_detail_headline = detail.get('headline_amount_cents')
    detail_headline_cents = (raw_detail_headline if isinstance(raw_detail_headline, int)
                             and not isinstance(raw_detail_headline, bool) and raw_detail_headline > 0
                             else None)
    page_claim = dict(page_amount_cents=page_amount_cents,
                      page_amount_label=page_amount_label,
                      page_amount_evidence=page_amount_evidence)
    if total_cents is not None:
        return dict(label='', note='', signals=[], state='explicit_order_amount', **page_claim)
    text = html.unescape((title or '') + '\n' + (body or ''))
    pattern = r'(?:[¥￥]\s*(\d+(?:\.\d{1,2})?)(?![\d.])|(?<![\d.])(\d+(?:\.\d{1,2})?)\s*元(?!起))'
    signals = list(dict.fromkeys(int(Decimal(a or b) * 100) for a,b in re.findall(pattern,text)))
    title_amount, _, _ = extract(title or '')
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
    starting_amounts = {int(Decimal(value) * 100) for value in
                        re.findall(r'(?<![\d.])(\d+(?:\.\d{1,2})?)\s*元\s*起', text)}
    if page_amount_cents is not None:
        headline_amount_cents = title_amount
        label = page_amount_label or '商家页面金额线索'
        note = (f'商家页面显示{label} ¥{Decimal(page_amount_cents)/100:.2f}'
                '；这是页面声称值，不是公开标价或已确认的账号结算价。')
        if page_amount_evidence:
            note += f'读取依据：{page_amount_evidence}。'
        note += '不参与低价排序、预算筛选、同款比较或利润判断。'
        state = 'merchant_page_amount_unbound'
    elif len(starting_amounts) == 1:
        headline_amount_cents = next(iter(starting_amounts))
        label = '来源起售价线索 · 具体规格待定'
        note = (f'来源标注“{Decimal(headline_amount_cents)/100:.2f}元起”，只说明某个未确定规格的起点；'
                '不作为本商品报价，不参与低价排序、预算筛选、同款比较或利润判断。')
        state = 'starting_price_unbound'
    elif title_amount is None and detail_headline_cents is not None:
        headline_amount_cents = detail_headline_cents
        label = '来源详情标题金额线索 · 非整单报价'
        note = (f'来源详情主文章标题标注 ¥{Decimal(detail_headline_cents)/100:.2f}；'
                '尚未形成明确整单金额与购买件数，不参与低价排序、预算筛选、同款比较或利润判断。')
        state = 'title_amount_unbound'
    elif title_amount is not None:
        headline_amount_cents = title_amount
        other_signals = [value for value in signals if value != title_amount]
        label = '标题金额已提取，购买口径待补全'
        note = f'标题金额线索 ¥{Decimal(title_amount)/100:.2f}。'
        if other_signals:
            note += ('原文正文还出现 ' + '、'.join(
                f'¥{Decimal(value)/100:.2f}' for value in other_signals[:4])
                + '；这些金额尚未按购买步骤绑定，不能当作整单应付价。')
        else:
            note += '尚未确认它是单件价还是整单价，也未提取明确购买件数。'
        state = 'title_amount_unbound'
    elif len(signals) > 1:
        headline_amount_cents = None
        label = '原文含多个金额，未形成唯一报价'
        note = '原文金额线索：' + '、'.join(f'¥{Decimal(value)/100:.2f}' for value in signals[:4]) + '；尚未对应到唯一购买方案。'
        state = 'multiple_amounts'
    elif signals:
        headline_amount_cents = None
        label = '原文金额未能对应购买方案'
        note = f'原文提及 ¥{Decimal(signals[0])/100:.2f}，但无法确认是单件价还是整单价。'
        state = 'amount_basis_unconfirmed'
    elif source_hint is not None:
        headline_amount_cents = None
        label = '来源金额缺少商品方案对应关系'
        note = f'来源接口记录 ¥{Decimal(source_hint)/100:.2f}；尚未确认对应商品规格与购买口径。'
        state = 'source_amount_unbound'
    else:
        headline_amount_cents = None
        label = '原文暂无明确商品报价'
        note = '标题和已采集正文中没有提取到明确人民币报价。'
        state = 'no_explicit_amount'
    return dict(label=label, note=note, signals=signals, headline_amount_cents=headline_amount_cents,
                source_hint_cents=source_hint, state=state, **page_claim)


def offer_summary(title, url, body, conditions='', metadata='{}', detail_json=None, sync_meta=None):
    """Readable excerpts, not a new price calculation or eligibility decision."""
    detail = json.loads(detail_json or '{}')
    body = detail.get('conditions') or body or ''
    text = title + '\n' + body + '\n' + (conditions or '')
    offer = public_offer(url, body, title)
    offer.setdefault('selected_spec', '')
    offer.setdefault('net_after_rebate_cents', None)
    offer['source_spec'] = offer.get('selected_spec') or structured_spec(title) or structured_spec(body)
    display_unit_cents = offer.get('unit_cents')
    if display_unit_cents is None and offer.get('total_cents') is not None and offer.get('quantity'):
        display_unit_cents = round(offer['total_cents'] / offer['quantity'])
    promotion = promotion_mentions(text)
    qualifications = qualification_mentions(text)
    origin = json.loads(metadata or '{}')
    price_status = _price_status(title, body, metadata, offer.get('total_cents'), detail)
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
    qualifications = qualification_mentions(source_text)
    parsed_claims = parse_discount_claims(source_text)
    coupon_face_claims = [claim for claim in parsed_claims
                          if claim['mechanism'] == 'coupon_face_value_claim']
    noncash_credit_claims = [claim for claim in parsed_claims
                             if claim['mechanism'] == 'noncash_credit_claim']
    random_reward_claims = [claim for claim in parsed_claims
                            if claim['mechanism'] == 'random_reward_claim']
    claim_counts = (f'{len(found)} 条优惠提及、{len(qualifications)} 条资格/入口提示'
                    + (f'、{len(coupon_face_claims)} 条券面额线索（面额不等于实际减免）'
                       if coupon_face_claims else '')
                    + (f'、{len(noncash_credit_claims)} 条积分/金币折抵声称（非现金）'
                       if noncash_credit_claims else '')
                    + (f'、{len(random_reward_claims)} 条随机奖励声称（不计入商品现金减价）'
                       if random_reward_claims else ''))
    has_claims = bool(found or qualifications or coupon_face_claims
                      or noncash_credit_claims or random_reward_claims)
    detail_expired = False
    if detail_checked_at and not detail_error:
        checked = None
        try:
            checked = datetime.fromisoformat(detail_checked_at)
            checked = checked.replace(tzinfo=timezone.utc) if checked.tzinfo is None else checked.astimezone(timezone.utc)
            detail_expired = not timedelta(0) <= datetime.now(timezone.utc) - checked <= timedelta(minutes=30)
        except (TypeError, ValueError):
            detail_expired = True
    if detail_error:
        state, label = 'detail_sync_failed', '详情同步失败'
        explanation = f'来源摘要已检查；{("原文识别到" + claim_counts + "；") if has_claims else "未识别到明确优惠线索；"}详情读取失败：{detail_error}'
    elif detail_expired:
        state, label = 'detail_sync_stale', '详情同步结果已过期'
        if checked:
            local_checked = checked.astimezone(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')
            freshness_note = f'上次详情同步时间：{local_checked}；'
        else:
            freshness_note = '上次详情同步时间无法解析；'
        explanation = (freshness_note
                      + ('曾识别到优惠提及，但当前没有新详情结果。' if has_claims
                         else '上次解析未发现明确优惠条件，但当前没有新详情结果。'))
    elif has_claims:
        state, label = 'conditions_found', '原文含优惠线索（未核实可用）'
        explanation = f'识别到{claim_counts}；均为来源声称，不代表已确认可用、可扣减或可叠加。'
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
                detail_error=detail_error or '', promotions=found, qualifications=qualifications,
                coupon_face_claims=coupon_face_claims,
                noncash_credit_claims=noncash_credit_claims,
                random_reward_claims=random_reward_claims)


def qualification_mentions(text):
    """Extract visible eligibility and entry cues without treating them as proof."""
    normalized=unicodedata.normalize('NFKC',text or '').casefold()
    terms=('88VIP','PLUS','新客','新用户','新人','首单','首购','首礼金','首立减金',
           '会员','省钱卡','直播','移动端','多人团','淘金币','返现','补贴',
           '详情页弹','页面弹券','加购砸蛋','砸蛋','抽奖','加购','详情页',
           '秒杀','试用','换购','凑单')
    found=[term for term in terms if term.casefold() in normalized]

    # Coupon required/entry and coupon-after prices are eligibility clues,
    # not coupon amounts. Exclude explicit negations such as "无需用券".
    coupon_pattern=re.compile(
        r'(?<!无)(?<!不)(?:需(?:要)?|须|必须)\s*(?:使用|用|领取|领)?\s*(?:优惠券|券)'
        r'|(?<!无需)(?<!不需)(?<!不需要)(?:领取优惠券|领券|用券)'
        r'|券后(?:价)?')
    found.extend(match.group(0) for match in coupon_pattern.finditer(normalized))

    # Preserve explicit minimum purchase quantities as conditions. A bare
    # "买2件" is not enough: only stated requirements are extracted.
    quantity_pattern=re.compile(
        r'(?<!无)(?<!不)(?:需(?:要)?|须|必须|至少|最低)\s*(?:购买|买|购|拍)\s*'
        r'\d+\s*(?:件|个|份|盒|包|提|箱|组|套|罐|瓶|张|次)(?:起)?')
    found.extend(match.group(0) for match in quantity_pattern.finditer(normalized))

    # Prefer the longest exact condition over component phrases.
    unique=list(dict.fromkeys(found))
    return [term for term in unique
            if not any(term != longer and term.casefold() in longer.casefold() for longer in unique)]


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
    catalog_listing = is_catalog_listing(row)
    starting_price = bool(re.search(r'\d+(?:\.\d+)?\s*元\s*起', row['title']))
    estimated_price = bool(re.search(r'预估到手价|预计到手价', row['title'] + ' ' + snippet))
    if catalog_listing and (starting_price or estimated_price):
        # A starting price or an account-dependent estimate is not a single
        # comparable public item price.
        price = None
    kind = resource_kind(row['title'],snippet)
    offer = public_offer(row['url'], snippet, row['title'])
    detail = json.loads(row['detail_json']) if row.get('detail_json') else {}
    if (catalog_listing and isinstance(detail.get('page_amount_cents'), int)
            and not isinstance(detail.get('page_amount_cents'), bool)
            and detail['page_amount_cents'] > 0):
        estimated_price = True
    if detail:
        title_price, inferred_spec, _ = extract(detail['title'])
        title_price = title_price or detail.get('headline_amount_cents')
        spec = detail.get('specification') or inferred_spec
        if detail.get('evidence_kind') == 'merchant_product_page':
            # Merchant pages can upgrade a catalog observation only with an
            # exact Product/Offer price, never from a product title or range.
            price = detail.get('advertised_cents')
        else:
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
    # Detail parsing can populate ``price`` after the initial title check above.
    # Keep a starting/estimated catalogue label from being accidentally
    # upgraded to an exact quote by an unrelated or generic detail amount.
    if catalog_listing and (starting_price or estimated_price):
        price = None
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
    catalog_current = catalog_listing and source_snapshot_is_current(row, now)
    if catalog_listing and not catalog_current:
        return state('source_unavailable', '商城目录快照超时或来源未成功采集；不能把旧目录标价当作当前')
    if not catalog_listing and published and not timedelta(0) <= now - published <= timedelta(hours=2):
        return state('stale', '原文超过 2 小时或时间在未来；自动留档，不列作当前机会')
    ttl = timedelta(minutes=min(2 * row['interval_minutes'] + 15, 120))
    seen, success = moment(row['last_seen_at']), moment(row['last_success'])
    if not row['enabled'] or row['source_status'] != 'healthy' or not seen or not success or not (timedelta(0) <= now-seen <= ttl and timedelta(0) <= now-success <= ttl):
        return state('source_unavailable', '来源暂停、抓取失败或快照超时；等待采集恢复后自动重算')
    page_checked = moment(row.get('detail_checked_at'))
    if (detail.get('availability_status') == 'out_of_stock' and page_checked
            and timedelta(0) <= now - page_checked <= timedelta(minutes=30)):
        return state('missing_price', '商家商品详情明确标记已下架；主价区没有当前报价，页面其他区域金额不采纳')
    listing_card_observation = detail.get('evidence_kind') == 'apple_catalog_card'
    if detail_probe_needed(row['url'], row['title'], snippet, detail) and not listing_card_observation:
        if row.get('detail_error'):
            return state('retry', '详情访问失败；按退避时间自动重试，不认定价格有效')
        checked = moment(row.get('detail_checked_at'))
        if not checked or now - checked > timedelta(minutes=30):
            return state('queued', '自动读取原文发布时间、公开报价和适用条件')
    if not published and not catalog_listing:
        return state('missing_time', '上游未提供可靠原文时间；隔离留档，采集更新后自动重算')
    audit = discount_audit(row['title'], detail.get('conditions') or snippet)
    if audit['formula']['state'] == 'conflict':
        return state('conflict', '原文扣减计算式算术不一致；不能认定报价成立')
    if detail and supported(row['url']) and urlparse(row['url']).hostname == 'www.smzdm.com' and price_conflicts(price, terms):
        return state('conflict', '标题区报价与正文实付描述不一致；自动隔离，等待原文更新后复查')
    if kind not in ('purchase','unknown'):
        return state('activity', RESOURCE_LABELS[kind] + '；需核对支出、资格、名额与截止时间，未认定免费或确定收益')
    if price is None:
        if catalog_listing and starting_price:
            return state('missing_price', '商城目录仅提供“起”价，未确定具体规格对应价格，不能用于商品比价')
        if catalog_listing and estimated_price:
            return state('conditional', '商城标注预估到手价，依赖商品选项或账号条件；未作为可执行报价')
        return state('missing_price', '没有唯一明确的人民币报价；需接入该来源的结构化价格能力')
    if audit['plan']['state'] == 'conditional_mismatch':
        return state('conditional', audit['plan']['reason'])
    if audit['uncertain']:
        return state('conditional', '；'.join(audit['risks'] or ['来源优惠扣减条件需拆解']) + '；仅按公开原文记录条件，不把未知扣减当成已生效价格')
    if not catalog_listing and (offer.get('total_cents') is None or not offer.get('quantity')):
        return state('missing_price', '原文可解析到金额线索，但没有明确整单金额与购买件数对应关系；仅保留线索，不作为商品报价')
    if catalog_listing:
        return state('observed', '商城商品目录单一公开标价；本次采集可见时间作为观察时间，不代表详情页库存、运费、优惠或账号最终结算价')
    return state('observed', '已提取来源公开报价；还需核对完整规格、数量口径、可比行情与费用依据，再判断低价及预计利润')


def classify_catalog_observation(row, now, duplicate=False):
    """Re-evaluate a direct catalog row with shared fields for read-only views."""
    source_row = dict(row)
    source_row.setdefault('enabled', source_row.get('source_enabled'))
    source_row.setdefault('interval_minutes', source_row.get('source_interval'))
    source_row.setdefault('last_success', source_row.get('source_last_success'))
    return classify(source_row, now, duplicate)


def review_all():
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with connect() as db:
        rows = db.execute('''SELECT o.*, e.snippet,e.metadata_json,e.published_at,e.last_seen_at,s.enabled,
            s.parser AS source_parser,s.method AS source_method,
            s.status AS source_status,s.interval_minutes,s.last_success,s.url AS source_url,
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
            if row['source_parser'] == 'apple' and not detail:
                observation = metadata.get('catalog_observation') or {}
                if observation.get('evidence_kind') == 'apple_catalog_card':
                    try:
                        from services.apple_catalog import parse_listing_card
                        detail = parse_listing_card(
                            row['url'], row['title'], observation['card_text'], row['source_url'])
                        row['detail_json'] = json.dumps(detail, ensure_ascii=False)
                    except (KeyError, TypeError, ValueError):
                        # Malformed or stale imported metadata never becomes price evidence.
                        detail = {}
                        row['detail_json'] = None
            db.execute('UPDATE opportunities SET resource_kind=?,topic=? WHERE id=?',
                       (resource_kind(detail.get('title') or row['title'],detail.get('conditions') or row['snippet']),
                        resource_topic(row['title'],metadata.get('source_category','')),row['id']))
            result = classify(row, now, bool(row['duplicate']))
            db.execute('''INSERT INTO auto_reviews
                (opportunity_id,state,reason,advertised_cents,specification,conditions,evidence,detail_json)
                VALUES(:id,:state,:reason,:advertised_cents,:specification,:conditions,:evidence,:detail_json)
                ON CONFLICT(opportunity_id) DO UPDATE SET state=excluded.state,reason=excluded.reason,
                advertised_cents=excluded.advertised_cents,specification=excluded.specification,
                conditions=excluded.conditions,evidence=excluded.evidence,
                detail_json=COALESCE(excluded.detail_json,auto_reviews.detail_json),
                checked_at=CURRENT_TIMESTAMP''', dict(result,id=row['id'],detail_json=row.get('detail_json')))
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
    counts = {}
    queue = []
    try:
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
                if supports_apple_catalog_product(row['url']):
                    detail = parse_apple_catalog_detail(row['url'], html)
                elif supports_merchant_product_page(row['url']):
                    detail = parse_merchant_product_page(row['url'], html)
                elif supports_guangdiu_detail(row['url']):
                    detail = parse_guangdiu_detail(html)
                    detail['advertised_cents'], inferred_spec, _ = extract(detail['title'])
                    detail['specification'] = structured_spec(detail['title']) or inferred_spec
                elif urlparse(row['url']).hostname == 'new.ixbk.net':
                    detail = parse_xianbao_detail(html)
                else:
                    detail = parse_detail(html)
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
        return dict(counts=counts, probed=len(queue))
    finally:
        # A killed or interrupted process must not strand the singleton lease.
        # Matching started_at prevents a slow, superseded run from releasing a newer lease.
        with connect() as db:
            db.execute('''UPDATE review_runs SET finished_at=CURRENT_TIMESTAMP,reviewed=?,probed=?
                WHERE id=1 AND started_at=? AND finished_at IS NULL''',
                       (sum(counts.values()),len(queue),stamp(now)))
