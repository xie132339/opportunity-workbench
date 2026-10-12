"""Admission of explicit public prices to the visible product-offer list."""
import json
import re
import unicodedata
from decimal import Decimal
from urllib.parse import urlparse
from services.taobao_living_topics import living_topic_ceiling_cents
from services import discount_calibration
from services.taobao_alliance import (
    has_current_promotion_signal as taobao_has_current_promotion_signal,
    reference_drop_plausibility,
    validated_offer_for_row as validated_taobao_alliance_offer,
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
        evidence = validated_taobao_alliance_offer(row)
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
        evidence = validated_taobao_alliance_offer(row)
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


def official_promotion_discount(row):
    """Return one current, explainable Taobao Alliance discount amount.

    The material feed can contain a reference-price reduction, a single coupon
    illustration, and a promotion path at once. Their stackability is not
    guaranteed, so select the largest individually explainable path instead of
    adding values together. Revalidation keeps expired coupons and paths out.
    """
    if row.get('source_method') != 'taobao_alliance':
        return None
    evidence = validated_taobao_alliance_offer(row)
    if not evidence:
        return None

    candidates = []
    reference_drop = evidence.get('promotion_discount_cents')
    topic = discount_calibration.topic_key(row.get('source_name') or '')
    reference_drop_plausible, _reason = discount_calibration.judge_drop(
        topic, evidence.get('reserve_price_cents'), evidence.get('promotion_price_cents'))
    if (evidence.get('price_drop_candidate') is True
            and reference_drop_plausible
            and isinstance(reference_drop, int)
            and not isinstance(reference_drop, bool)
            and reference_drop > 0):
        reference_cents = evidence.get('reserve_price_cents')
        candidates.append(dict(
            amount_cents=reference_drop,
            rate_basis_points=(reference_drop * 10000 // reference_cents
                               if isinstance(reference_cents, int) and reference_cents > 0 else None),
            basis='推广价相对官方参考价差',
            note='联盟物料中的当前推广价低于官方返回的参考价；参考价不是账号结算价。',
        ))

    coupon_drop = evidence.get('coupon_applied_cents')
    if (evidence.get('quote_basis') == 'single_coupon_conditional_estimate'
            and evidence.get('coupon_candidate') is True
            and isinstance(coupon_drop, int)
            and not isinstance(coupon_drop, bool)
            and coupon_drop > 0):
        promotion_cents = evidence.get('promotion_price_cents')
        candidates.append(dict(
            amount_cents=coupon_drop,
            rate_basis_points=(coupon_drop * 10000 // promotion_cents
                               if isinstance(promotion_cents, int) and promotion_cents > 0 else None),
            basis='单券条件试算减免',
            note='当前券时间、库存与单件门槛可计算；账号资格和结算扣减仍未核实。',
        ))

    promotion_path = evidence.get('promotion_path') or {}
    path_drop = promotion_path.get('known_discount_cents')
    if (evidence.get('promotion_path_complete') is True
            and isinstance(path_drop, int)
            and not isinstance(path_drop, bool)
            and path_drop > 0):
        promotion_cents = evidence.get('promotion_price_cents')
        candidates.append(dict(
            amount_cents=path_drop,
            rate_basis_points=(path_drop * 10000 // promotion_cents
                               if isinstance(promotion_cents, int) and promotion_cents > 0 else None),
            basis='完整官方促销路径减免',
            note='当前官方促销组件可完整复算；不与参考价差或券面额叠加。',
        ))

    return max(candidates, key=lambda candidate: candidate['amount_cents'], default=None)


def meets_minimum_official_discount(row, minimum_cents):
    """Whether an official offer meets the caller's minimum discount filter."""
    if minimum_cents <= 0:
        return True
    discount = row.get('official_discount') or official_promotion_discount(row)
    return bool(discount and discount['amount_cents'] >= minimum_cents)


def affiliate_promotion(row):
    """Return a current Alliance promotion link and its returned commission.

    A material price is not itself a personal promotion link.  The UI may call
    a link "我的联盟推广链接" only when the official material response returned
    the signed Alliance redirect for the configured ad zone.  Commission is a
    returned estimate, so a missing or malformed amount is displayed as zero
    rather than reconstructed from a commission rate.
    """
    if row.get('source_method') != 'taobao_alliance':
        return None
    offer = validated_taobao_alliance_offer(row)
    url = str(row.get('url') or '').strip()
    parsed = urlparse(url)
    if not (offer and offer.get('link_kind') == 'official_promotion_link'
            and parsed.scheme == 'https' and parsed.hostname == 's.click.taobao.com'
            and parsed.path):
        return None
    commission = offer.get('commission_amount_cents')
    if not isinstance(commission, int) or isinstance(commission, bool) or commission < 0:
        commission = 0
    return {
        'url': url,
        'commission_amount_cents': commission,
    }


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
        evidence = validated_taobao_alliance_offer(row)
        quote = row.get('display_quote') or {}
        promotion_cents = (evidence or {}).get('promotion_price_cents')
        topic = discount_calibration.topic_key(row.get('source_name') or '')
        ceiling = (discount_calibration.price_ceiling_cents(topic)
                   if topic else living_topic_ceiling_cents(row.get('source_name') or ''))
        if (ceiling and isinstance(promotion_cents, int)
                and not isinstance(promotion_cents, bool)
                and promotion_cents > ceiling):
            return dict(eligible=False, tier='promotion_clue',
                        reason='推广价超出当前生活品主题合理上界，仅作来源审计，不作为默认优惠展示')
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
