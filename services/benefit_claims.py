"""Extract auditable discount claims from text without asserting usability."""
import re
import unicodedata
from decimal import Decimal

PARSER_VERSION = 'benefit-amount-span-v11'
_AMOUNT = r'\d{1,9}(?:\.\d{1,2})?'
_HSPACE = r'[^\S\n]*'
_THRESHOLD = re.compile(
    rf'(?:(?P<full>满){_HSPACE})?(?:(?P<qualifier>超补|超级补贴){_HSPACE})?'
    rf'(?P<threshold>{_AMOUNT}){_HSPACE}(?:元)?{_HSPACE}'
    rf'(?P<operator>减|[-－–]){_HSPACE}(?P<discount>{_AMOUNT}){_HSPACE}(?:元)?',
    re.I,
)
_QUANTITY_THRESHOLD = re.compile(
    rf'满{_HSPACE}(?P<quantity>\d+){_HSPACE}件{_HSPACE}'
    rf'(?:减|[-－–]){_HSPACE}(?P<discount>{_AMOUNT}){_HSPACE}(?:元)?'
)
_FIXED = re.compile(rf'(?P<qualifier>立减|直减|直降|支付减|下单减)\s*(?P<discount>{_AMOUNT})\s*元')
_SUBSIDY_AMOUNT = re.compile(
    rf'(?P<qualifier>官方补贴|国家补贴|政府补贴|地区补贴|平台补贴|店铺补贴|国补|补贴){_HSPACE}'
    rf'(?:(?:立减|直减|减|抵扣|抵){_HSPACE})?(?P<discount>{_AMOUNT}){_HSPACE}元'
    rf'(?!{_HSPACE}(?:优惠)?[券卷劵])'
)
_SUBSIDY_RATE = re.compile(
    rf'(?P<qualifier>官方补贴|国家补贴|政府补贴|地区补贴|国补)\s*'
    rf'(?P<rate>{_AMOUNT})\s*[%％]'
)
_COUPON_FIXED = re.compile(
    rf'(?P<qualifier>领券中心领取|领取|领券|领)\s*(?:优惠券)?\s*'
    rf'(?:立减|直减|减)\s*(?P<discount>{_AMOUNT})\s*元'
)
_PERCENT_REDUCTION = re.compile(
    rf'(?P<qualifier>立减|直减|直降|优惠|折扣)\s*(?P<rate>{_AMOUNT})\s*%'
)
_RATE = re.compile(r'(?P<qualifier>最高|至高|低至)?\s*(?P<rate>\d{1,2}(?:\.\d{1,2})?)\s*折')
_FACE_COUPON = re.compile(
    rf'(?:(?P<verb>领券中心领取|领取|领券|领)\s*)?'
    rf'(?P<amount>{_AMOUNT})\s*(?P<unit>元)?\s*(?:现金)?(?:优惠)?[券卷劵]'
)
_GIFT_END = r'(?=$|[\s，,。；;、)】]|到手|实付|红包|返现|优惠|活动|会员|PLUS|券|卷|劵|💰|¥|￥)'
_GIFT_EXPLICIT = re.compile(
    rf'(?P<qualifier>首(?:购|单)?|新人|新客)\s*(?:礼金|立减金|立减|红包)\s*'
    rf'(?:立减|减)?\s*(?P<discount>{_AMOUNT})\s*(?P<unit>元)?(?!起){_GIFT_END}'
)
_GIFT_SHORT = re.compile(
    rf'(?P<qualifier>首购|首单)\s*(?:立减|减|[-－−–])\s*(?P<discount>{_AMOUNT})\s*'
    rf'(?P<unit>元)?(?!起){_GIFT_END}'
)
_NEW_PRODUCT_GIFT = re.compile(
    rf'(?P<qualifier>新品(?:礼金|立减金|红包)?)\s*(?:立减|减)?\s*'
    rf'(?P<discount>{_AMOUNT})\s*(?P<unit>元)?(?!起){_GIFT_END}'
)
_NONCASH_CREDIT = re.compile(
    rf'(?P<qualifier>淘金币|京东豆|京豆|店铺积分|积分(?:兑换)?)\s*'
    rf'(?:可抵扣|可抵|抵扣|抵)\s*(?:约|最高|至多)?\s*'
    rf'(?P<amount>{_AMOUNT})\s*元(?P<range>起|以上|以内)?'
)
_NONCASH_CREDIT_EQUIVALENT = (
    re.compile(
        rf'(?P<qualifier>淘金币|京东豆|京豆|店铺积分|积分(?:兑换)?){_HSPACE}'
        rf'(?P<amount>{_AMOUNT}){_HSPACE}元(?P<range>起|以上|以内)?'
        rf'(?!{_HSPACE}(?:可)?(?:抵扣|抵)|{_HSPACE}(?:优惠)?[券卷劵])'
    ),
    re.compile(
        rf'(?P<amount>{_AMOUNT}){_HSPACE}元{_HSPACE}'
        rf'(?P<qualifier>淘金币|京东豆|京豆|店铺积分|积分(?:兑换)?)'
        rf'(?!{_HSPACE}(?:可)?(?:抵扣|抵)|{_HSPACE}(?:优惠)?[券卷劵])'
    ),
)
_RANDOM_REWARD = re.compile(
    rf'(?P<qualifier>抽到|抽中|中奖|中得|获得){_HSPACE}(?:了|红包|现金红包)?{_HSPACE}'
    rf'(?:(?:红包|现金红包){_HSPACE})?(?P<amount>{_AMOUNT}){_HSPACE}元'
    rf'(?!{_HSPACE}(?:优惠)?[券卷劵])'
)
_RANDOM_GUARANTEED_REWARD = (
    re.compile(rf'(?P<qualifier>预约|参与|参加)?(?P<event>抽奖|抽红包)[^\n。；;]{{0,12}}?'
               rf'(?P<amount>{_AMOUNT}){_HSPACE}元{_HSPACE}保底'),
    re.compile(rf'(?P<qualifier>预约|参与|参加)?(?P<event>抽奖|抽红包)[^\n。；;]{{0,12}}?'
               rf'保底{_HSPACE}(?P<amount>{_AMOUNT}){_HSPACE}元'),
)
_COUPON_MARK = re.compile(r'券|卷|劵')
_COUPON_SUFFIX = re.compile(r'^[^\S\n]*(?:优惠)?[券卷劵]')


def _normalize_with_offsets(text):
    normalized = []
    offsets = []
    for index, char in enumerate(text):
        value = unicodedata.normalize('NFKC', char)
        normalized.append(value)
        offsets.extend([(index, index + 1)] * len(value))
    return ''.join(normalized), offsets


def _cents(value):
    return int(Decimal(value) * 100)


def _claim(match, offsets, original, evidence_type, mechanism, values, span_end=None):
    start, match_end = match.span()
    end = span_end if span_end is not None else match_end
    if not offsets or end <= start:
        return None
    original_start = offsets[start][0]
    original_end = offsets[end - 1][1]
    excerpt_start = max(0, original_start - 32)
    excerpt_end = min(len(original), original_end + 32)
    occurrence = {
        'matched_text': original[original_start:original_end],
        'evidence_excerpt': original[excerpt_start:excerpt_end],
        'span_start': original_start,
        'span_end': original_end,
    }
    return {
        'mechanism': mechanism,
        **values,
        'qualifier': match.groupdict().get('qualifier') or '',
        'evidence_type': evidence_type,
        **occurrence,
        'occurrences': [occurrence],
        'parser_version': PARSER_VERSION,
        'verification': 'source_claim_only',
        'applicability': 'not_verified',
    }


def parse_discount_claims(text, evidence_type='source_post', limit=30):
    """Extract explicit coupon, cash-reduction, subsidy, and credit claims.

    Spans index the exact supplied input (before NFKC normalization). A parsed
    claim is only a statement found in a source; it does not establish product
    scope, eligibility, validity, stackability, or a payable/effective price.
    """
    limit = max(0, int(limit))
    if not limit:
        return []
    original = str(text or '')
    normalized, offsets = _normalize_with_offsets(original)
    candidates = []

    for match in _THRESHOLD.finditer(normalized):
        line_start = normalized.rfind('\n', 0, match.start()) + 1
        line_end = normalized.find('\n', match.end())
        if line_end < 0:
            line_end = len(normalized)
        if not match.group('full'):
            context = (normalized[max(line_start, match.start() - 6):match.start()]
                       + normalized[match.end():min(line_end, match.end() + 8)])
            if not match.group('qualifier') and not _COUPON_MARK.search(context):
                continue
        try:
            threshold = _cents(match.group('threshold'))
            discount = _cents(match.group('discount'))
        except ArithmeticError:
            continue
        if threshold <= 0 or discount <= 0:
            continue
        claim_end = match.end()
        trailing = _COUPON_MARK.search(normalized[claim_end:min(line_end, claim_end + 8)])
        if trailing:
            claim_end += trailing.end()
        values = {'threshold_cents': threshold, 'discount_cents': discount}
        if match.group('qualifier'):
            values['qualifier'] = match.group('qualifier')
        item = _claim(match, offsets, original, evidence_type, 'threshold_discount_claim',
                      values, claim_end)
        if item:
            candidates.append(item)

    for match in _QUANTITY_THRESHOLD.finditer(normalized):
        try:
            quantity = int(match.group('quantity'))
            discount = _cents(match.group('discount'))
        except (ArithmeticError, ValueError):
            continue
        if quantity <= 0 or discount <= 0:
            continue
        item = _claim(match, offsets, original, evidence_type,
                      'quantity_threshold_discount_claim',
                      {'threshold_quantity': quantity, 'discount_cents': discount})
        if item:
            candidates.append(item)

    for match in _SUBSIDY_AMOUNT.finditer(normalized):
        try:
            discount = _cents(match.group('discount'))
        except ArithmeticError:
            continue
        if discount <= 0:
            continue
        item = _claim(match, offsets, original, evidence_type, 'subsidy_reduction_claim',
                      {'discount_cents': discount})
        if item:
            candidates.append(item)

    for match in (*_FIXED.finditer(normalized), *_COUPON_FIXED.finditer(normalized)):
        coupon_suffix = _COUPON_SUFFIX.search(normalized[match.end():min(len(normalized), match.end() + 8)])
        # “领取减X元优惠券” states a coupon face amount; it does not prove
        # that X yuan is a fixed deduction from this product's checkout price.
        if match.re is _COUPON_FIXED and coupon_suffix:
            continue
        if any(gift.start() <= match.start() and gift.end() >= match.end()
               for gift in (*_GIFT_EXPLICIT.finditer(normalized), *_GIFT_SHORT.finditer(normalized))):
            continue
        try:
            discount = _cents(match.group('discount'))
        except ArithmeticError:
            continue
        if discount <= 0:
            continue
        claim_end = match.end()
        if match.re is _FIXED and coupon_suffix:
            claim_end += coupon_suffix.end()
        item = _claim(match, offsets, original, evidence_type, 'fixed_reduction_claim',
                      {'discount_cents': discount}, claim_end)
        if item:
            candidates.append(item)

    for match in _PERCENT_REDUCTION.finditer(normalized):
        try:
            reduction = Decimal(match.group('rate'))
        except ArithmeticError:
            continue
        if not Decimal('0') < reduction <= Decimal('100'):
            continue
        reduction_basis_points = int(reduction * 100)
        item = _claim(match, offsets, original, evidence_type, 'percentage_reduction_claim',
                      {'discount_rate_basis_points': reduction_basis_points,
                       'pay_rate_basis_points': 10000 - reduction_basis_points})
        if item:
            candidates.append(item)

    for match in _SUBSIDY_RATE.finditer(normalized):
        try:
            rate = Decimal(match.group('rate'))
        except ArithmeticError:
            continue
        if not Decimal('0') < rate <= Decimal('100'):
            continue
        item = _claim(match, offsets, original, evidence_type, 'subsidy_rate_claim',
                      {'subsidy_rate_basis_points': int(rate * 100)})
        if item:
            candidates.append(item)

    for match in _NEW_PRODUCT_GIFT.finditer(normalized):
        try:
            discount = _cents(match.group('discount'))
        except ArithmeticError:
            continue
        if discount <= 0:
            continue
        item = _claim(match, offsets, original, evidence_type, 'new_product_gift_claim',
                      {'discount_cents': discount,
                       'unit_evidence': 'explicit_cny' if match.group('unit') else 'unit_not_stated'})
        if item:
            candidates.append(item)

    for match in _NONCASH_CREDIT.finditer(normalized):
        try:
            claimed_value = _cents(match.group('amount'))
        except ArithmeticError:
            continue
        if claimed_value <= 0:
            continue
        range_term = match.group('range') or ''
        claim_mode = 'minimum_claim' if range_term in ('起', '以上') else ('maximum_claim' if range_term == '以内' else 'unspecified_range')
        item = _claim(match, offsets, original, evidence_type, 'noncash_credit_claim',
                      {'claimed_equivalent_cents': claimed_value, 'claim_mode': claim_mode,
                       'cash_deductible': False})
        if item:
            candidates.append(item)

    for pattern in _NONCASH_CREDIT_EQUIVALENT:
        for match in pattern.finditer(normalized):
            try:
                claimed_value = _cents(match.group('amount'))
            except ArithmeticError:
                continue
            if claimed_value <= 0:
                continue
            item = _claim(match, offsets, original, evidence_type,
                          'noncash_credit_claim',
                          {'claimed_equivalent_cents': claimed_value,
                           'claim_mode': 'source_claimed_equivalent',
                           'equivalence_basis': 'source_text_with_yuan_unit',
                           'cash_deductible': False})
            if item:
                candidates.append(item)

    for pattern in _RANDOM_GUARANTEED_REWARD:
        for match in pattern.finditer(normalized):
            try:
                reward = _cents(match.group('amount'))
            except ArithmeticError:
                continue
            if reward <= 0:
                continue
            item = _claim(match, offsets, original, evidence_type,
                          'random_reward_claim',
                          {'reward_cents': reward,
                           'claim_mode': 'source_claimed_minimum_reward',
                           'cash_deductible': False, 'probabilistic': True,
                           'guaranteed_minimum_claimed': True})
            if item:
                candidates.append(item)

    for match in _RANDOM_REWARD.finditer(normalized):
        try:
            reward = _cents(match.group('amount'))
        except ArithmeticError:
            continue
        if reward <= 0:
            continue
        item = _claim(match, offsets, original, evidence_type,
                      'random_reward_claim',
                      {'reward_cents': reward, 'claim_mode': 'source_reports_received',
                       'cash_deductible': False, 'probabilistic': True})
        if item:
            candidates.append(item)

    for pattern in (_GIFT_EXPLICIT, _GIFT_SHORT):
        for match in pattern.finditer(normalized):
            try:
                discount = _cents(match.group('discount'))
            except ArithmeticError:
                continue
            if discount <= 0:
                continue
            unit_evidence = 'explicit_cny' if match.group('unit') else 'inferred_from_first_order_shorthand'
            item = _claim(match, offsets, original, evidence_type, 'first_order_gift_claim',
                          {'discount_cents': discount, 'unit_evidence': unit_evidence})
            if item:
                candidates.append(item)

    for match in _RATE.finditer(normalized):
        try:
            source_rate = Decimal(match.group('rate'))
        except ArithmeticError:
            continue
        rate = source_rate
        # Chinese commerce sources often write 85折/95折 for 8.5折/9.5折.
        # Normalize only the conventional integer range; values like 11折 are
        # not silently reinterpreted as 1.1折.
        if source_rate == source_rate.to_integral_value() and Decimal('30') <= source_rate <= Decimal('100'):
            rate = source_rate / Decimal('10')
        if not Decimal('0') < rate <= Decimal('10'):
            continue
        pay_rate = int(rate * 1000)
        item = _claim(match, offsets, original, evidence_type, 'pay_rate_claim',
                      {'pay_rate_basis_points': pay_rate,
                       'discount_rate_basis_points': 10000 - pay_rate})
        if item:
            candidates.append(item)

    for match in _FACE_COUPON.finditer(normalized):
        if not match.group('unit') and not match.group('verb'):
            continue
        try:
            face_value = _cents(match.group('amount'))
        except ArithmeticError:
            continue
        if face_value <= 0:
            continue
        item = _claim(match, offsets, original, evidence_type, 'coupon_face_value_claim',
                      {'face_value_cents': face_value,
                       'unit_evidence': 'explicit_cny' if match.group('unit') else 'inferred_from_coupon_shorthand'})
        if item:
            candidates.append(item)

    # A threshold/fixed rule is more informative than its overlapping “X元券”
    # suffix. Never count the same text span once as a rule and again as a face value.
    rule_spans = [(item['span_start'], item['span_end']) for item in candidates
                  if item['mechanism'] in ('threshold_discount_claim', 'fixed_reduction_claim')]
    candidates = [item for item in candidates
                  if item['mechanism'] != 'coupon_face_value_claim'
                  or not any(start <= item['span_start'] and item['span_end'] <= end
                             for start, end in rule_spans)]

    candidates.sort(key=lambda item: (item['span_start'], item['span_end'], item['mechanism']))
    unique = []
    by_rule = {}
    for item in candidates:
        rule_values = tuple(sorted((key, value) for key, value in item.items()
                                   if key.endswith('_cents') or key.endswith('_basis_points')))
        key = (item['mechanism'], item['qualifier'], rule_values,
               item.get('claim_mode'), item.get('unit_evidence'))
        existing = by_rule.get(key)
        if existing:
            existing['occurrences'].extend(item['occurrences'])
            continue
        item['occurrence_count'] = 1
        by_rule[key] = item
        unique.append(item)
        if len(unique) >= limit:
            break
    for item in unique:
        item['occurrence_count'] = len(item['occurrences'])
    return unique
