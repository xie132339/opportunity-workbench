"""Evidence excerpts and explicit arithmetic only; never infer eligibility or stack coupons."""
import re
import unicodedata
from itertools import permutations
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from services.benefit_claims import parse_discount_claims

NUMBER = r'\d+(?:\.\d{1,2})?'


def normalize_rule_wording(text):
    # Only coupon-labelled shorthand; never rewrite weight/price ranges.
    # Keep the evidence label on the same source line; \s would cross into a
    # following copied/tag section and turn an unrelated numeric tail into a coupon.
    text = re.sub(r'(?<![\d.])('+NUMBER+r')[ \t]*[-－−–][ \t]*('+NUMBER+r')(?=[ \t]*(?:元)?(?:优惠券|优惠卷|券|卷|劵))',
                   lambda m: '满'+m[1]+'减'+m[2], text)
    text = re.sub(r'满[ \t]*('+NUMBER+r')[ \t]*[-－][ \t]*('+NUMBER+r')(?=[ \t]*(?:元?优惠券|元?券|券))',
                   lambda m: '满'+m[1]+'减'+m[2], text)
    text = re.sub(r'((?:领券|领卷)中心(?:领取|领)?)[ \t]*('+NUMBER+r')[ \t]*[-－][ \t]*('+NUMBER+r')',
                   lambda m: m[1]+'满'+m[2]+'减'+m[3], text)
    text = re.sub(r'(领取|领券[:：]?|优惠券[:：]?)[ \t]*('+NUMBER+r')[ \t]*[-－][ \t]*('+NUMBER+r')(?=[ \t]*(?:元?优惠券|元?券|[，,。；;]|$))',
                   lambda m: m[1]+'满'+m[2]+'减'+m[3], text)
    text = re.sub(r'(满\s*\d+\s*件)\s*[，,]\s*(?=打?\s*'+NUMBER+r'\s*折)',r'\1',text)
    return text


def promotion_mentions(text):
    source_text = text or ''
    text = normalize_rule_wording(source_text)
    pattern = (r'满\s*\d+(?:\.\d+)?\s*元?\s*减\s*\d+(?:\.\d+)?(?:元)?'
               r'|满\s*\d+\s*件\s*减\s*\d+(?:\.\d+)?元?'
               r'|(?:满)?\d+件\s*[，,]?\s*(?:打)?\s*\d+(?:\.\d+)?折'
               r'|首(?:购|单)?(?:礼金|立减金|红包)\s*(?:立减|减)?\s*\d+(?:\.\d+)?元'
               r'|首(?:购|单)\s*(?:立减|减|[-－−–])\s*\d+(?:\.\d+)?(?:元)?(?!起)(?=$|[\s，,。；;、)】])'
               r'|(?:补贴|砸落)?\d+(?:\.\d+)?元?券|立减\s*\d+(?:\.\d+)?元')
    mentions = re.findall(pattern, text)
    # Keep the legacy source excerpts and add mechanisms recognized by the
    # auditable claim parser. The parser only extracts source claims; it does
    # not confirm scope, eligibility, stacking, or a payable price.
    claim_mechanisms = {
        'threshold_discount_claim', 'quantity_threshold_discount_claim', 'fixed_reduction_claim',
        'subsidy_reduction_claim', 'subsidy_rate_claim',
        'new_product_gift_claim', 'first_order_gift_claim',
        'percentage_reduction_claim', 'pay_rate_claim',
    }
    def signature(claim):
        fields = ('mechanism', 'threshold_cents', 'threshold_quantity', 'discount_cents',
                  'subsidy_rate_basis_points', 'discount_rate_basis_points',
                  'pay_rate_basis_points')
        return tuple(claim.get(field) for field in fields)

    represented = {signature(claim) for mention in mentions
                   for claim in parse_discount_claims(mention)}
    for claim in parse_discount_claims(source_text):
        if claim['mechanism'] not in claim_mechanisms or signature(claim) in represented:
            continue
        mentions.append(claim['matched_text'])
        represented.add(signature(claim))
    return list(dict.fromkeys(mentions))


def amount(value):
    return int(Decimal(value) * 100)


def explicit_source_order_claim(body, title=''):
    """Bind a source-stated amount to an explicit purchase action and item count.

    This is only a claim from the source text. It does not assert that a merchant
    page, account, coupon, inventory, or checkout will honor the amount.
    """
    body = unicodedata.normalize('NFKC', (body or '').split('商品介绍')[0].split('品牌介绍')[0])
    title = unicodedata.normalize('NFKC', title or '')
    plan_text = title + '\n' + body
    claims = set()
    amount_re = r'('+NUMBER+r')'
    forward = re.compile(
        r'(?:拍|下单|购买|买)\s*(\d+)\s*件\s*'
        r'(?:(?:券后|到手|实付|合计|总计|共计|共|一共)\s*)?'
        r'[【〔\[（(]?\s*[¥￥]?\s*'+amount_re+r'\s*元(?!起)'
        r'(?!\s*[/／]\s*(?:件|个|提|包|卷|片|支|盒|瓶))')
    for match in forward.finditer(plan_text):
        claims.add((amount(match.group(2)), int(match.group(1))))

    title_prices = set()
    for match in re.finditer(r'[¥￥]\s*('+NUMBER+r')|(?<![\d.])('+NUMBER+r')\s*元', title):
        title_prices.add(amount(match.group(1) or match.group(2)))
    reverse = re.compile(
        r'(?P<price>(?:[¥￥]\s*)?'+NUMBER+r'(?:\s*元)?)\s*[/／]\s*'
        r'(?:拍|下单|购买|买)\s*(?P<quantity>\d+)\s*件')
    for match in reverse.finditer(plan_text):
        token = match.group('price')
        cents = amount(re.sub(r'[¥￥元\s]', '', token))
        if re.search(r'[¥￥]|元', token) or cents in title_prices:
            claims.add((cents, int(match.group('quantity'))))

    action_quantities = {
        int(value) for value in re.findall(
            r'(?:任拍|拍|下单|购买|需买|买)\s*(\d+)\s*件'
            r'(?!\s*(?:返|送|赠|享|折))', plan_text)
    }
    labeled_totals = {
        amount(value) for value in re.findall(
            r'(?<!返现后)(?<!返款后)(?<!返利后)'
            r'(?:最终)?(?:实付|到手(?:价)?|券后|合计|总计|共计)\s*'
            r'(?:低至)?[【〔\[（(]?\s*[¥￥]?('+NUMBER+r')\s*元(?!起)'
            r'(?!\s*[/／]\s*(?:件|个|提|包|卷|片|支|盒|瓶))', plan_text)
    }
    if len(action_quantities) == 1 and len(labeled_totals) == 1:
        claims.add((next(iter(labeled_totals)), next(iter(action_quantities))))

    if len(claims) > 1:
        return dict(total_cents=None, quantity=None,
                    error='原文包含多个“购买件数—金额”方案，不能合并成一条报价')
    if not claims:
        return dict(total_cents=None, quantity=None, error='')
    total_cents, quantity = next(iter(claims))
    return dict(total_cents=total_cents, quantity=quantity, error='')


def discount_audit(title, body):
    body = body or ''
    text = title + '\n' + body
    risk_text = re.sub(r'(?:颜色|款式|图案)随机|随机(?:颜色|款式|图案)', '', text)
    risk_patterns = [
        ('概率优惠，不能保证领到', r'概率|随机|部分有|部分用户|部分账号|不一定|不对则无|因人而异|砸券|砸蛋|砸落|抽奖'),
        ('限新客或首单', r'新客|新人|首单|首购|首礼金|首立减金'),
        ('需要会员或特定权益', r'88VIP|PLUS|会员|省钱卡'),
        ('需要指定入口或操作', r'会场|加购|直播|APP|app'),
        ('返款有后续条件', r'返现|返后|晒反|晒返'),
        ('优惠有地区或时间限制', r'限时|限地区|限城市|限川渝|限华东'),
    ]
    risks = [label for label, pattern in risk_patterns if re.search(pattern, risk_text)]
    parts = re.split(r'[\n；;。]+|(?<!\d)[，,]|[，,](?!\d)', body.split('商品介绍')[0].split('品牌介绍')[0])
    steps = []
    for part in parts:
        part = re.sub(r'https://[^\s<>]+', '', part).strip(' ，,:：')
        if part.startswith('可用券及活动'):
            continue
        if part and re.search(r'搜索|券|加购|会场|专享|砸|下单|实付|包邮|运费|返现|返后|积分|领取', part):
            if part not in steps:
                steps.append(part[:240])
    # Keep original shorthand: "3券" does not prove a 3-yuan deduction.
    coupons = promotion_mentions(text)
    terms = purchase_terms(body)
    bases, quantities = set(terms['base']), set(terms['quantity'])
    shipping = list(dict.fromkeys(re.findall(r'[^\n，,。；;]{0,20}(?:包邮|运费)[^\n，,。；;]{0,30}', body)))
    formula = dict(state='missing', expression='', calculated_cents=None, claimed_cents=None)
    # Only a labeled, complete +/- cash equation; no eval and no guessed stacking order.
    pattern = (r'(?:价格计算|整单计算|优惠计算|计算公式)\s*[:：]\s*'
               r'('+NUMBER+r'\s*元?(?:\s*[-−+＋]\s*'+NUMBER+r'\s*元?)+)'
               r'\s*[=＝]\s*('+NUMBER+r')\s*元?(?=$|[\s，,。；;])')
    matches = list(re.finditer(pattern, body))
    if len(matches) == 1:
        m = matches[0]
        values = re.findall(NUMBER, m[1])
        signs = re.findall(r'[-−+＋]', m[1])
        total = amount(values[0])
        for sign, value in zip(signs, values[1:]):
            total += amount(value) * (-1 if sign in '-−' else 1)
        claimed = amount(m[2])
        formula = dict(state='consistent' if total == claimed and total >= 0 else 'conflict',
                       expression=m[0], calculated_cents=total, claimed_cents=claimed)
    plan = calculate_plan(body, title=title)
    gaps = []
    if len(bases) != 1: gaps.append('缺唯一明确的起始单价')
    if len(quantities) != 1: gaps.append('购买件数或计价口径未明确')
    if coupons: gaps.append('券适用商品、领取资格及叠加顺序尚无商家规则依据')
    if plan['shipping_cents'] is None: gaps.append('运费金额或适用地区未明确')
    if formula['state'] == 'missing' and not plan['cases']: gaps.append(plan['reason'])
    if plan['state'] == 'conditional_mismatch': gaps.append(plan['reason'])
    if formula['state'] == 'conflict': gaps.append('原文计算式的算术结果不一致')
    if coupons or any(term in text for term in ('会员','新客','新人','首单','首购','首礼金','首立减金','PLUS','88VIP','直播','移动端','淘金币','补贴','概率','随机','砸蛋','抽奖')):
        gaps.append('现有公开证据无法确定优惠适用商品、资格或叠加规则')
    return dict(risks=risks, steps=steps[:12], steps_truncated=len(steps)>12,
                coupons=coupons, base_cents=next(iter(bases)) if len(bases)==1 else None,
                quantity=next(iter(quantities)) if len(quantities)==1 else None,
                shipping=shipping[:3], formula=formula, plan=plan, gaps=gaps,
                uncertain=bool(risks or coupons))


def public_market_profit(opp, quotes, source_total_cents, specification, mode='sold', source_shipping_cents=None):
    """One transparent spread formula; exact-SKU quotes only, never account checkout data."""
    labels = {'sold': '保守成交模式', 'listing': '挂牌参考模式'}
    if mode not in labels:
        mode = 'sold'
    if source_total_cents is None or source_total_cents <= 0:
        return dict(mode=mode, mode_label=labels[mode], amount_cents=None, reason='来源整单报价未解析，无法计算',
                    market_cents=None, buy_cents=source_total_cents, costs_cents=0, missing_costs=[])
    spec = ' '.join((specification or '').split()).casefold()
    if not spec:
        return dict(mode=mode, mode_label=labels[mode], amount_cents=None, reason='缺少可用于同款行情匹配的完整规格',
                    market_cents=None, buy_cents=source_total_cents, costs_cents=0, missing_costs=[])
    today = date.today()
    matches = []
    for raw in quotes:
        q = dict(raw)
        if (not q.get('same_spec') or not q.get('evidence_url') or not q.get('conditions')
                or ' '.join((q.get('specification') or '').split()).casefold() != spec):
            continue
        try:
            price_at = date.fromisoformat(q.get('price_at') or '')
            age = today - price_at
            if age < timedelta(0):
                continue
        except (TypeError, ValueError):
            continue
        if mode == 'sold' and q.get('kind') == 'sold' and age <= timedelta(days=30):
            matches.append(q)
        elif mode == 'listing' and q.get('kind') == 'listing' and age <= timedelta(days=14):
            if q.get('valid_until'):
                try:
                    if date.fromisoformat(q['valid_until']) < today:
                        continue
                except ValueError:
                    continue
            matches.append(q)
    independent = {}
    for q in matches:
        independent[q['evidence_url']] = min(independent.get(q['evidence_url'], q['amount_cents']), q['amount_cents'])
    if len(independent) < 3:
        return dict(mode=mode, mode_label=labels[mode], amount_cents=None,
                    reason=f'同规格{labels[mode]}仅有 {len(independent)} 条近期独立来源；至少需要 3 条',
                    market_cents=None, buy_cents=source_total_cents, costs_cents=0, missing_costs=[])
    market = min(independent.values())
    known_costs = source_shipping_cents or 0
    missing_costs = []
    if source_shipping_cents is None:
        missing_costs.append('买入运费')
    # Account-specific/manual execution fields are intentionally not treated as public market facts.
    missing_costs.extend(['卖出运费','平台费','检测/包装费','其他费用','风险预留'])
    amount_cents = market - source_total_cents - known_costs
    reason = (f'统一公式：最低同款{labels[mode]} - 来源整单报价 - 已知费用；'
              f'市场参考取 {len(independent)} 条独立记录中的最低值。')
    if missing_costs:
        reason += '以下费用未取得，当前仅为扣已知费用后的价差：' + '、'.join(missing_costs) + '。'
    else:
        reason += '费用字段齐全；仍是估算，不等于已成交利润。'
    return dict(mode=mode, mode_label=labels[mode], amount_cents=amount_cents,
                reason=reason, market_cents=market, buy_cents=source_total_cents,
                costs_cents=known_costs, missing_costs=missing_costs,
                sample_count=len(independent), formula='同款市场参考价 - 来源整单报价 - 已知费用')

def purchase_terms(body):
    """Shared source-text fields; values are claims, not checkout evidence."""
    body = (body or '').split('商品介绍')[0].split('品牌介绍')[0]
    patterns = {
        'base': r'(?:商品面价|商品原价|起始单价|活动售价)\s*[:：]?\s*('+NUMBER+r')元(?!起)',
        'unit': r'(?:最终到手价|实付单件)\s*[:：]?\s*('+NUMBER+r')元(?!起)',
        # Some upstream/OCR text corrupts the second character or inserts punctuation
        # (for example, “实.咐21元”). Keep the label anchored to “实付” so arbitrary
        # amounts elsewhere in the source are never promoted to a purchase total.
        'total': r'(?<!返现后)(?<!返款后)(?<!返利后)(?<!单件)实[\W_]{0,2}[付咐]\s*[:：]?\s*(?:低至)?\s*('+NUMBER+r')元(?!起)',
        'net_after_rebate': r'(?:返现|返款|返利)[ \t]*后[ \t]*(?:最终)?(?:实付|到手|净成本|成本)[ \t]*[:：]?[ \t]*(?:低至)?('+NUMBER+r')元(?!起)',
        'seckill': r'秒杀(?:活动)?(?:价|价格)?\s*[:：]?\s*('+NUMBER+r')元(?!起)',
    }
    values = {key: sorted({amount(v) for v in re.findall(pattern, body)}) for key,pattern in patterns.items()}
    gift_patterns = (
        re.compile(r'(?P<qualifier>首(?:购|单)?|新人|新客)\s*(?:礼金|立减金|立减|红包)\s*(?:立减|减)?\s*(?P<amount>'+NUMBER+r')\s*(?P<unit>元)?(?!起)(?=$|[\s，,。；;、)】])'),
        re.compile(r'(?P<qualifier>首购|首单)\s*(?:立减|减|[-－−–])\s*(?P<amount>'+NUMBER+r')\s*(?P<unit>元)?(?!起)(?=$|[\s，,。；;、)】])'),
    )
    gift_hits = [match for pattern in gift_patterns for match in pattern.finditer(body)]
    values['gift'] = sorted({amount(match.group('amount')) for match in gift_hits})
    values['gift_unit_assumption'] = [1] if any(not match.group('unit') for match in gift_hits) else []
    # A source may mention both the regular price and a seckill price. The
    # explicitly marked seckill amount is the candidate calculation baseline;
    # neither is promoted to a merchant checkout observation.
    if values['seckill']:
        values['base'] = values['seckill']
    del values['seckill']
    values['quantity'] = sorted({int(v) for v in re.findall(r'(?:购买|需买|下单|拍|买)\s*(\d+)\s*件(?!\s*(?:返|送|赠|享|折))',body)})
    return values


def shipping_claim(body):
    """Read source freight independently of whether its discount formula can be solved."""
    clean=(body or '').split('商品介绍')[0].split('品牌介绍')[0].split('可用券及活动')[0]
    fees={amount(v) for v in re.findall(r'运费\s*[:：]?\s*('+NUMBER+r')元',clean)}
    restricted=re.search(r'偏远|不包邮|不一定包邮|满.{0,12}包邮|部分地区|运费另|运费.{0,12}(?:起|至|到)|包邮.{0,5}(?:吗|么|不确定)',clean)
    free=bool(re.search(r'(?:^|[\s，,。；;：:【】]|\d+(?:\.\d+)?元?)包邮(?=$|[\s，,。；;！!】])',clean))
    if restricted or len(fees)>1:return None
    if len(fees)==1:return None if free and next(iter(fees))>0 else next(iter(fees))
    return 0 if free else None


def calculate_plan(body, title=''):
    """Conditional arithmetic across all sources; never chooses unknown stacking rules."""
    source_body = (body or '').split('商品介绍')[0].split('品牌介绍')[0].split('可用券及活动')[0]
    # xianbao detail summaries prepend machine-generated tag-only lines such as
    # “券后 / 首购、会员、用券、礼金”. They describe labels, not extra amounts.
    # Strip only leading lines made entirely from this closed tag vocabulary;
    # the same words later in the source remain evidence and can still block.
    summary_tags={'券后','首购','首单','新客','新人','会员','用券','礼金','补贴','淘金币','移动端','需买','凑单'}
    lines=source_body.splitlines()
    while lines:
        tokens=[part.strip() for part in re.split(r'[、，,\s]+',lines[0].strip()) if part.strip()]
        if tokens and all(token in summary_tags for token in tokens):
            lines.pop(0)
        else:
            break
    clean=(title+'\n' if title else '')+'\n'.join(lines)
    clean = normalize_rule_wording(clean)
    fields = purchase_terms(clean)
    result = dict(state='missing', reason='', cases=[], fields=fields, shipping_cents=shipping_claim(clean),
                  claimed_cents=None, rule_evidence=[])
    if any(len(values)>1 for values in fields.values()):
        result.update(state='blocked',reason='同一方案存在多个单价、整单金额或件数，需要先拆分报价方案')
        return result
    if not fields['base'] or not fields['quantity'] or fields['quantity'][0]<=0:
        result['reason']='缺明确起始单价或购买件数，不能倒推或默认买1件'
        return result
    base, quantity = fields['base'][0], fields['quantity'][0]
    claimed = fields['total'][0] if fields['total'] else None
    result['claimed_cents']=claimed
    if fields['unit'] and claimed is not None and abs(fields['unit'][0]*quantity-claimed)>quantity:
        result.update(state='blocked',reason='原文单件价×件数与整单金额不一致')
        return result
    if re.search(r'不可叠加|不叠加|二选一|任选|凑单|跨店|另购|第[二三四\d]+件|第二件|买[一二\d]+送|每满|上不封顶|买\d+件返|E卡|返现|返后|晒返|晒反|定金|尾款|预售|淘金币|积分|补贴|国补|立减|直降|折上折|限时优惠|随机|概率|砸券|砸落|部分账号',clean):
        result.update(state='blocked',reason='涉及混合凑单、额外扣减、非现金权益、概率或预售，当前简单方案不足以复算')
        return result
    coupon_matches = list(re.finditer(r'满\s*('+NUMBER+r')\s*元?\s*减\s*('+NUMBER+r')\s*元?',clean))
    quantity_reductions=list(re.finditer(r'满\s*(\d+)\s*件\s*减\s*('+NUMBER+r')\s*元?',clean))
    discounts = list(re.finditer(r'(满)?\s*(\d+)\s*件\s*(?:打)?\s*('+NUMBER+r')\s*折',clean))
    gift_patterns = (
        re.compile(r'(?P<qualifier>首(?:购|单)?|新人|新客)\s*(?:礼金|立减金|立减|红包)\s*(?:立减|减)?\s*(?P<amount>'+NUMBER+r')\s*(?P<unit>元)?(?!起)(?=$|[\s，,。；;、)】])'),
        re.compile(r'(?P<qualifier>首购|首单)\s*(?:立减|减|[-－−–])\s*(?P<amount>'+NUMBER+r')\s*(?P<unit>元)?(?!起)(?=$|[\s，,。；;、)】])'),
    )
    gift_matches = [match for pattern in gift_patterns for match in pattern.finditer(clean)]
    gift_amounts = sorted({amount(match.group('amount')) for match in gift_matches})
    if len(gift_amounts) > 1:
        result.update(state='blocked',reason='来源提到多个不同首购/新客礼金额，无法确定本单实际抵扣')
        return result
    gift = gift_amounts[0] if gift_amounts else None
    gift_unit_assumption = any(not match.group('unit') for match in gift_matches)
    if gift is not None and quantity != 1:
        result.update(state='blocked',reason='首购/新客礼金在多件订单中的按单/按件口径未明确')
        return result
    if len(coupon_matches)>1 or len(discounts)>1 or len(quantity_reductions)>1 or (quantity_reductions and discounts):
        result.update(state='blocked',reason='存在多个优惠条目，不能默认全部叠加或重复扣减')
        return result
    recognized = coupon_matches+discounts+quantity_reductions+gift_matches
    rest = clean
    for match in sorted(recognized,key=lambda m:m.start(),reverse=True):
        rest=rest[:match.start()]+rest[match.end():]
    # Product composition is not a percentage discount. Without this narrow
    # exception, common titles such as “100%纯棉洗脸巾” look like an unknown
    # percent-off promotion and block an otherwise auditable source formula.
    # Keep percentage claims outside these explicit material terms fail-closed.
    rest = re.sub(
        r'(?<![\d.])\d+(?:\.\d+)?\s*%\s*(?=(?:纯棉|棉|羊毛|羊绒|真丝|桑蚕丝|蚕丝|'
        r'聚酯纤维|锦纶|氨纶|涤纶|腈纶|粘胶纤维|莫代尔))',
        '', rest)
    if '礼金' in rest or '红包' in rest:
        result.update(state='blocked',reason='发现未能精确提取金额的礼金/红包，不忽略未知扣减')
        return result
    if re.search(r'\d+(?:\.\d+)?\s*(?:元?券|折|%|元?减)',rest):
        result.update(state='blocked',reason='有未解析的券或折扣条件，不能忽略后继续计算')
        return result
    if not coupon_matches and not discounts and not quantity_reductions and gift is None:
        result['reason']='未提取到支持的明确满减或件数折扣规则'
        return result
    threshold = reduction = None
    if coupon_matches:
        threshold,reduction=map(amount,coupon_matches[0].groups())
    rate = None;fixed_reduction=None
    if quantity_reductions:
        required,cut=quantity_reductions[0].groups();fixed_reduction=amount(cut)
        if int(required)<=0 or quantity<int(required) or fixed_reduction>base*quantity:
            result.update(state='blocked',reason='件数满减门槛不满足或扣减超过商品金额');return result
    if discounts:
        minimum,n,discount=discounts[0].groups();n=int(n);rate=Decimal(discount)/10
        if not Decimal(0)<rate<=1 or n<=0:
            result.update(state='blocked',reason='折扣或件数条件数值无效');return result
        if quantity<n or (not minimum and quantity!=n):
            result.update(state='blocked',reason='购买件数不满足或无法确认原文件数折扣的适用范围');return result
    result['rule_evidence']=[m[0] for m in recognized]
    gross=base*quantity
    promotion_label='件数满减' if fixed_reduction is not None else '折扣'
    if gift is not None:
        operations=[]
        if fixed_reduction is not None: operations.append('fixed')
        elif rate is not None: operations.append('discount')
        if threshold is not None: operations.append('coupon')
        operations.append('gift')
        orders=list(permutations(operations)) if len(operations)>1 else [operations]
    else:
        orders=[['coupon']] if rate is None and fixed_reduction is None else ([['discount']] if threshold is None else [['discount','coupon'],['coupon','discount']])
    fmt=lambda value: f'{Decimal(value)/100:.2f}'
    for order in orders:
        current=gross;expression=f'{fmt(base)} × {quantity}';valid=True;notes=[]
        for operation in order:
            if operation=='coupon':
                notes.append(f'该顺序按扣券前金额{fmt(current)}元判断满{fmt(threshold)}元门槛')
                if current<threshold or reduction>current:
                    valid=False;break
                current-=reduction;expression+=f' − {fmt(reduction)}'
            elif operation=='gift':
                if current<gift or gift<=0:
                    valid=False;break
                current-=gift
                expression+=f' − {fmt(gift)}'
                notes.append('首购/新客礼金按整单现金抵扣处理仅为算术假设；本人资格、礼金可抵范围及平台规则未核实')
                if gift_unit_assumption:
                    notes.append('来源简写，金额单位未在原文标注为人民币元；为解释来源金额声称，仅按人民币元值作条件试算')
            else:
                if fixed_reduction is not None:
                    if current<fixed_reduction:valid=False;break
                    current-=fixed_reduction;expression+=f' − {fmt(fixed_reduction)}'
                else:
                    current=int((Decimal(current)*rate).quantize(Decimal('1'),rounding=ROUND_HALF_UP))
                    expression=f'({expression}) × {rate}'
        if valid:
            fee=result['shipping_cents']
            result['cases'].append(dict(expression=expression+' = '+fmt(current)+'元',
                goods_cents=current,cash_cents=current+fee if fee is not None else None,
                matches_claim=(current==claimed) if claimed is not None else None,
                notes=notes,order='算术假设顺序：'+' → '.join(
                    {'coupon':'满减券','discount':'件数折扣','fixed':'件数立减','gift':'首购/新客礼金'}[item]
                    for item in order)))
    # Some coupons use the original eligible amount as their threshold basis.
    # Keep this as an explicit hypothesis, never silently pick it as the actual rule.
    if gift is None and (rate is not None or fixed_reduction is not None) and threshold is not None and gross>=threshold:
        discounted=gross-fixed_reduction if fixed_reduction is not None else int((Decimal(gross)*rate).quantize(Decimal('1'),rounding=ROUND_HALF_UP))
        if discounted<threshold and reduction<=discounted:
            value=discounted-reduction;fee=result['shipping_cents']
            result['cases'].append(dict(expression=f'({fmt(base)} × {quantity}) '+(f'− {fmt(fixed_reduction)}' if fixed_reduction is not None else f'× {rate}')+f' − {fmt(reduction)} = {fmt(value)}元',
                goods_cents=value,cash_cents=value+fee if fee is not None else None,
                matches_claim=(value==claimed) if claimed is not None else None,
                notes=[f'假设满{fmt(threshold)}元按折前商品金额{fmt(gross)}元判断；券类型、资格和适用商品尚无公开规则证据'],
                order=f'先{promotion_label}后满减，门槛按折前金额（来源没有说明，假设分支）'))
    if not result['cases']:
        result.update(state='blocked',reason='按已提取单价和件数未达到门槛，或减额超过商品总额')
    elif claimed is None:
        result.update(state='calculated',reason='已按明确字段试算，但缺原文整单报价可供核对')
    elif any(case['matches_claim'] for case in result['cases']):
        result.update(state='conditional_match',reason='至少一种试算与原文整单报价一致；算术吻合不证明资格、适用范围或实际扣减已成立')
    else:
        result.update(state='conditional_mismatch',reason='已识别条件的试算与原文整单报价不一致，可能缺优惠或计价依据')
    return result
