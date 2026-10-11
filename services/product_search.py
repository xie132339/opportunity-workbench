"""Admission of explicit public prices to the visible product-offer list."""
import json
import re
import unicodedata
from decimal import Decimal
from services.taobao_alliance import (
    has_current_promotion_signal as taobao_has_current_promotion_signal,
    validated_offer as validated_taobao_alliance_offer,
)


UNUSABLE_REVIEW_STATES = frozenset({
    'conflict', 'excluded', 'retry', 'source_unavailable', 'stale',
})
UNBOUND_PRICE_STATES = frozenset({
    'amount_basis_unconfirmed', 'multiple_amounts', 'source_amount_unbound',
    'title_amount_unbound', 'starting_price_unbound',
    'merchant_page_amount_unbound',
})

_PRICE = r'(\d{1,9}(?:\.\d{1,2})?)'
_BEFORE_PRICE = re.compile(
    r'(?:商品原价|商品面价|原售价|原价|划线价|降价前(?:售价|价格)?|优惠前(?:售价|价格)?)'
    r'\s*(?:为|是|[:：])?\s*[¥￥]?\s*' + _PRICE + r'\s*元?'
)
_AFTER_PRICE = re.compile(
    r'(?P<label>券后(?:价)?|折后(?:价)?|优惠后(?:售价|价格)?|到手(?:价)?|实付(?:价)?|'
    r'合计|总计|共计|现售价|现价|活动售价|活动价|促销价|优惠价)'
    r'\s*(?:为|是|[:：])?\s*[¥￥]?\s*(?P<amount>' + _PRICE + r')\s*元?'
)


def quote_note(brief):
    """Use a non-price explanation when a source amount is not a usable quote."""
    state = brief.get('price_status', {}).get('state')
    if brief.get('net_after_rebate_cents') is not None and brief.get('total_cents') is None:
        return '来源只声称返现后的净成本；返现前付款金额与到账条件未核实，不作为购买报价或低价样本。'
    if state == 'merchant_page_amount_unbound':
        return brief.get('price_status', {}).get('note') or '商家页面金额是条件/预估线索，不作为报价。'
    if state in UNBOUND_PRICE_STATES:
        note = brief.get('price_status', {}).get('note')
        return ((note + ' ') if note else '原文含金额线索。') + '尚未形成可执行整单报价；不参与低价排序、预算筛选、同款比较或利润判断。'
    return brief.get('price_status', {}).get('note') or '当前证据不足，不能作为可用报价显示。'


def listing_quote(row, brief):
    """Return a price display only when its source and amount basis are explicit.

    Feed claims remain claims; catalogue observations remain public listing
    prices. Ambiguous or absent money stays in the clue inbox, never rendered as
    a purchasable offer or an invented zero.
    """
    if row.get('auto_state') in UNUSABLE_REVIEW_STATES:
        return None
    if row.get('auto_state') in {'campaign_upcoming', 'campaign_active'}:
        amount = row.get('advertised_cents')
        if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
            return None
        phase = '即将开始' if row.get('auto_state') == 'campaign_upcoming' else '活动进行中'
        return dict(amount_cents=amount, kind='campaign_public',
                    label=f'众筹公开档位价 · {phase}',
                    note=row.get('auto_reason') or '来源公开活动价；不是本人结算价或已核实捡漏。')
    if row.get('source_method') == 'taobao_alliance':
        evidence = validated_taobao_alliance_offer(row.get('metadata_json'))
        if not evidence or row.get('auto_state') not in {'observed', 'conditional'}:
            return None
        detail = row.get('detail_json') or {}
        if isinstance(detail, str):
            try:
                detail = json.loads(detail)
            except (TypeError, ValueError):
                detail = {}
        if (detail.get('evidence_kind') == 'taobao_alliance_sku_detail'
                and detail.get('field_state') == 'bound'
                and detail.get('promotional_candidate') is True
                and isinstance(detail.get('advertised_cents'), int)
                and detail.get('advertised_cents') > 0):
            return dict(amount_cents=detail['advertised_cents'], kind='source_claim',
                        label='淘宝联盟 SKU 预估优惠价',
                        note='官方详细接口已绑定具体SKU、规格、库存与优惠路径；该金额仍是预估到手价，账号资格、运费及结算结果需下单页确认。')
        amount = evidence.get('quote_price_cents')
        official_promotion = taobao_has_current_promotion_signal(evidence)
        if isinstance(amount, int) and not isinstance(amount, bool) and amount > 0 and official_promotion:
            return dict(
                amount_cents=amount,
                kind='official_promotion_observation',
                label='淘宝联盟官方推广价',
                note='官方物料接口已返回商品身份、推广入口、参考价与当前推广价。'
                     'SKU规格、库存、账号资格、运费和结算结果未核实；此价不能作为账号结算价或全网最低价。'
                     '只有标题规格命中本地维护基准时，才可产生“按维护基准”的比较结论。',
            )
        return None
    if row.get('catalog_listing'):
        amount = row.get('advertised_cents')
        if amount is None:
            return None
        return dict(amount_cents=amount, kind='catalog_public',
                    label='商城目录公开标价',
                    note='目录公开观察值；商品变体、库存、优惠和最终结算价未确认。')
    total = brief.get('total_cents')
    quantity = brief.get('quantity')
    if total is None or not quantity or brief.get('error'):
        return None
    return dict(amount_cents=total, kind='source_claim',
                label=f'来源原文整单声称价 · {quantity}件',
                note='金额与购买件数来自来源原文；不是商家结算价。')


def _has_inline_price_drop(row, amount, quantity):
    """Match a displayed after-price to a nearby before-price in source text."""
    if not isinstance(quantity, int) or quantity <= 0:
        return False
    source_text = unicodedata.normalize('NFKC', '\n'.join((
        row.get('title') or '', row.get('snippet') or '', row.get('auto_conditions') or '',
    )))
    before_prices = [
        (match.start(), int(Decimal(match.group(1)) * 100))
        for match in _BEFORE_PRICE.finditer(source_text)
    ]
    after_prices = [
        (match.start(), int(Decimal(match.group('amount')) * 100), match.group('label'))
        for match in _AFTER_PRICE.finditer(source_text)
    ]
    for before_position, before_cents in before_prices:
        for after_position, after_cents, after_label in after_prices:
            total_label = after_label in {'实付', '实付价', '合计', '总计', '共计'}
            quoted_total_matches = (
                after_cents == amount if total_label
                else after_cents * quantity == amount
            )
            if (abs(before_position - after_position) <= 120 and quoted_total_matches
                    and before_cents > after_cents):
                return True
    return False


def is_promotional_price_candidate(row, brief=None):
    """Require a bound purchase amount and explicit source evidence of a price drop.

    Promotion wording alone ("活动价", "优惠券", etc.) does not establish that
    the displayed amount is discounted. A source claim is admitted only when
    its amount is lower than an explicit before-price or matches a complete,
    source-stated discount calculation. This is still a source claim, not proof
    of merchant checkout eligibility or a bargain versus the market.
    """
    brief = brief or row.get('offer_brief') or {}
    quote = row.get('display_quote')
    if not quote:
        return False
    if quote.get('kind') == 'campaign_public':
        # A future public tier is useful as a dated lead, but it is not an
        # available offer and must not enter the default current-offer list.
        return row.get('auto_state') == 'campaign_active'
    catalog_price = quote.get('kind') == 'catalog_public' or row.get('catalog_listing')
    official_taobao_promotion = (row.get('source_method') == 'taobao_alliance'
                                 and quote.get('kind') == 'official_promotion_observation')
    if quote.get('kind') != 'source_claim' and not catalog_price and not official_taobao_promotion:
        return False
    amount = quote.get('amount_cents')
    if not isinstance(amount, int) or isinstance(amount, bool) or amount < 0:
        return False
    if amount == 0:
        return False

    if row.get('source_method') == 'taobao_alliance':
        evidence = validated_taobao_alliance_offer(row.get('metadata_json'))
        return bool(quote.get('kind') in {'official_promotion_observation', 'source_claim'}
                    and taobao_has_current_promotion_signal(evidence))

    previous = brief.get('previous_cents')
    quantity = brief.get('quantity')
    if catalog_price:
        # A catalogue price remains excluded unless the same product record
        # explicitly binds it to a higher prior price or to an inline after-
        # price. A coupon/activity word alone still does not qualify.
        if isinstance(previous, int) and previous > amount:
            return True
        return _has_inline_price_drop(row, amount, quantity or 1)
    if (isinstance(previous, int) and quantity == 1 and previous > amount):
        return True

    # Some publishers use ordinary labels such as “原价10元，券后8元” instead
    # of the older “降价前售价为10元” wording. Bind the lower amount to this
    # exact order quote and only compare nearby price labels in captured source
    # text; never infer a reduction from a coupon face value by itself.
    audit = brief.get('audit') or {}
    if _has_inline_price_drop(row, amount, quantity):
        return True

    plan = audit.get('plan') or {}
    base_cents = audit.get('base_cents')
    plan_quantity = audit.get('quantity')
    if (plan.get('state') == 'conditional_match'
            and isinstance(base_cents, int) and isinstance(plan_quantity, int)
            and isinstance(quantity, int) and not isinstance(quantity, bool)
            and base_cents > 0 and plan_quantity == quantity and plan_quantity > 0):
        if any(case.get('matches_claim') is True
               and isinstance(case.get('cash_cents'), int)
               and not isinstance(case.get('cash_cents'), bool)
               and case.get('cash_cents') == amount
               and isinstance(case.get('goods_cents'), int)
               and not isinstance(case.get('goods_cents'), bool)
               and 0 <= case.get('goods_cents') < base_cents * plan_quantity
               for case in plan.get('cases', [])):
            return True
    formula = audit.get('formula') or {}
    if (formula.get('state') == 'consistent'
            and formula.get('calculated_cents') == amount
            and formula.get('claimed_cents') == amount
            and isinstance(base_cents, int) and base_cents > amount
            and re.search(r'[-−]', formula.get('expression') or '')):
        return True
    return False


def promotion_admission(row, assessment):
    """Apply the product definition after a source promotion signal is found.

    A price-drop signal is only discovery evidence.  The default offer list
    additionally requires the shared quote-readiness checks: stable product
    identity, explicitly selected specification, bound order amount, current
    source evidence, and reproducible conditional arithmetic.  Market bargain
    status is deliberately not produced here because it requires a separate
    benchmark Decision.
    """
    if not row.get('promotional_candidate'):
        return dict(eligible=False, tier='clue', reason='未形成可核对的优惠证据')
    if row.get('source_method') == 'taobao_alliance':
        evidence = validated_taobao_alliance_offer(row.get('metadata_json'))
        quote = row.get('display_quote') or {}
        if (quote.get('kind') == 'official_promotion_observation'
                and isinstance(quote.get('amount_cents'), int)
                and quote['amount_cents'] > 0 and evidence):
            return dict(eligible=True, tier='official_public_promotion',
                        reason='淘宝联盟官方物料已提供商品身份、推广入口、当前推广价及优惠信号；'
                               'SKU规格、库存、账号资格、运费和结算价仍待增强核验，不称为账号结算价或全网最低价。')
        return dict(eligible=False, tier='promotion_clue',
                    reason='淘宝联盟物料缺少可显示的官方推广价或有效优惠信号')
    assessment = assessment or {}
    if not assessment.get('search_ready'):
        failures = assessment.get('search_failures') or []
        return dict(eligible=False, tier='promotion_clue',
                    reason='；'.join(failures) or '明确报价准入未通过')
    return dict(eligible=True, tier='public_promotion',
                reason='优惠证据与明确报价准入均通过；尚未形成市场捡漏结论')
