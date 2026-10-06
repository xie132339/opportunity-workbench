import unittest

from services.benefit_claims import PARSER_VERSION, parse_discount_claims
from pricing import promotion_mentions


class BenefitClaimParserTests(unittest.TestCase):
    def test_extracts_common_threshold_shorthands_without_calling_them_verified(self):
        for text, threshold, discount in (
            ('10点 跳转 APP抢 5.9-5卷', 590, 500),
            ('199-100宠物劵', 19900, 10000),
            ('京东特价版8-4全品券', 800, 400),
            ('满1000元减40元', 100000, 4000),
        ):
            with self.subTest(text=text):
                claim = parse_discount_claims(text)[0]
                self.assertEqual(claim['mechanism'], 'threshold_discount_claim')
                self.assertEqual(claim['threshold_cents'], threshold)
                self.assertEqual(claim['discount_cents'], discount)
                self.assertEqual(claim['verification'], 'source_claim_only')
                self.assertEqual(claim['applicability'], 'not_verified')
                self.assertEqual(claim['parser_version'], PARSER_VERSION)
                self.assertEqual(text[claim['span_start']:claim['span_end']], claim['matched_text'])

    def test_extracts_each_explicit_mechanism_in_one_source(self):
        text = '家用券：满199减100，支付立减5元，会员最高8.5折。'
        claims = parse_discount_claims(text)
        self.assertEqual([claim['mechanism'] for claim in claims], [
            'threshold_discount_claim', 'fixed_reduction_claim', 'pay_rate_claim'])
        self.assertEqual(claims[0]['threshold_cents'], 19900)
        self.assertEqual(claims[1]['discount_cents'], 500)
        self.assertEqual(claims[2]['pay_rate_basis_points'], 8500)
        self.assertEqual(claims[2]['discount_rate_basis_points'], 1500)
        for claim in claims:
            self.assertEqual(text[claim['span_start']:claim['span_end']], claim['matched_text'])
            self.assertIn(claim['evidence_excerpt'], text)

    def test_first_order_gift_and_implicit_yuan_shorthand_remain_claims_with_exact_spans(self):
        text='用5.1-5优惠券，首购-1，下单1件；另一个入口写首礼金1元'
        claims=parse_discount_claims(text)
        gifts=[claim for claim in claims if claim['mechanism']=='first_order_gift_claim']
        self.assertEqual(len(gifts),2)
        shorthand=next(claim for claim in gifts if claim['matched_text']=='首购-1')
        explicit=next(claim for claim in gifts if claim['matched_text']=='首礼金1元')
        self.assertEqual(shorthand['discount_cents'],100)
        self.assertEqual(shorthand['unit_evidence'],'inferred_from_first_order_shorthand')
        self.assertEqual(explicit['discount_cents'],100)
        self.assertEqual(explicit['unit_evidence'],'explicit_cny')
        for claim in claims:
            self.assertEqual(text[claim['span_start']:claim['span_end']],claim['matched_text'])
            self.assertEqual(claim['verification'],'source_claim_only')
            self.assertEqual(claim['applicability'],'not_verified')
        payable = parse_discount_claims('首单减5到手💰41.7')[0]
        self.assertEqual(payable['mechanism'], 'first_order_gift_claim')
        self.assertEqual(payable['matched_text'], '首单减5')

    def test_repeated_same_rule_is_one_candidate_but_all_source_spans_survive(self):
        text = '满199减100宠物券\n标题：满199减100宠物券'
        claims = parse_discount_claims(text)
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0]['occurrence_count'], 2)
        self.assertEqual(len(claims[0]['occurrences']), 2)
        for occurrence in claims[0]['occurrences']:
            self.assertEqual(text[occurrence['span_start']:occurrence['span_end']],
                             occurrence['matched_text'])
        self.assertEqual(claims[0]['matched_text'], claims[0]['occurrences'][0]['matched_text'])

    def test_full_width_amounts_keep_exact_original_offsets(self):
        text = '新人券满１９９减１００劵，限地区'
        claim = parse_discount_claims(text)[0]
        self.assertEqual((claim['threshold_cents'], claim['discount_cents']), (19900, 10000))
        self.assertEqual(text[claim['span_start']:claim['span_end']], '满１９９减１００劵')
        self.assertEqual(claim['matched_text'], '满１９９减１００劵')

    def test_ambiguous_prices_units_and_number_pairs_require_local_promotion_context(self):
        for text in (
            '商品标价100元，活动价80元', '纸巾100抽×10包', '型号199-100，普通商品',
            '型号199-100组合装', '首购-1kg装湿厕纸',
        ):
            with self.subTest(rejected=text):
                self.assertEqual(parse_discount_claims(text), [])
        accepted = parse_discount_claims('型号199-100优惠券')
        self.assertEqual([item['mechanism'] for item in accepted], ['threshold_discount_claim'])

    def test_super_subsidy_shorthand_is_parsed_only_with_its_explicit_prefix(self):
        text = '活动售价55.9元，领取超补39-6，下单1件，来源称低至16.83元'
        claim = next(item for item in parse_discount_claims(text) if item.get('qualifier') == '超补')
        self.assertEqual((claim['matched_text'], claim['threshold_cents'], claim['discount_cents']), ('超补39-6', 3900, 600))
        self.assertEqual(claim['verification'], 'source_claim_only')
        self.assertIn('超补39-6', promotion_mentions(text))
        self.assertEqual(parse_discount_claims('型号39-6组合装'), [])

    def test_coupon_scope_stays_local_and_rate_claims_normalize_with_bounds(self):
        for text, expected in (
            ('优惠券满5.1-5券后\n用券\n满件折1-0.85', '满5.1-5券'),
            ('满1000减40元优惠券，特价1104.15，满件折1-0.85\n券后', '满1000减40元优惠券'),
        ):
            with self.subTest(scope=text):
                claims = parse_discount_claims(text)
                self.assertEqual(len(claims), 1)
                self.assertEqual(claims[0]['mechanism'], 'threshold_discount_claim')
                self.assertEqual(claims[0]['matched_text'], expected)
        for text in ('全场11折', '最高0折', '加价11折'):
            with self.subTest(rejected_rate=text):
                self.assertEqual(parse_discount_claims(text), [])
        self.assertEqual(parse_discount_claims('会员低至8折')[0]['qualifier'], '低至')
        for source, normalized in (('85折', 8500), ('95折', 9500), ('75折', 7500), ('8.5折', 8500)):
            with self.subTest(source=source):
                claim = parse_discount_claims(f'会员{source}')[0]
                self.assertEqual(claim['mechanism'], 'pay_rate_claim')
                self.assertEqual(claim['pay_rate_basis_points'], normalized)
                self.assertEqual(claim['matched_text'], source)
        claims = parse_discount_claims('立减5%优惠活动；直降15％')
        self.assertEqual([claim['mechanism'] for claim in claims], [
            'percentage_reduction_claim', 'percentage_reduction_claim'])
        self.assertEqual([claim['discount_rate_basis_points'] for claim in claims], [500, 1500])
        self.assertEqual([claim['pay_rate_basis_points'] for claim in claims], [9500, 8500])
        self.assertEqual([claim['matched_text'] for claim in claims], ['立减5%', '直降15％'])

    def test_coupon_face_value_is_not_a_threshold_or_confirmed_deduction(self):
        text = '领取20元优惠券，另有满99减10元券；领取5优惠券'
        claims = parse_discount_claims(text)
        face = [claim for claim in claims if claim['mechanism'] == 'coupon_face_value_claim']
        threshold = [claim for claim in claims if claim['mechanism'] == 'threshold_discount_claim']
        self.assertEqual([(claim['face_value_cents'], claim['unit_evidence']) for claim in face], [
            (2000, 'explicit_cny'), (500, 'inferred_from_coupon_shorthand')])
        self.assertEqual(len(threshold), 1)
        self.assertEqual(threshold[0]['discount_cents'], 1000)
        self.assertFalse(any(claim['mechanism'] == 'coupon_face_value_claim'
                             and claim['matched_text'] == '10元券' for claim in claims))
        amount_text = '补贴价3699元，领取减1922元优惠券，使用国家补贴9折'
        amount_claims = parse_discount_claims(amount_text)
        coupon = next(item for item in amount_claims if item['mechanism'] == 'coupon_face_value_claim')
        self.assertEqual((coupon['face_value_cents'], coupon['matched_text']), (192200, '1922元优惠券'))
        self.assertFalse(any(item['mechanism'] == 'fixed_reduction_claim' for item in amount_claims))
        self.assertNotIn('领取减1922元', promotion_mentions(amount_text))
        fixed_text = '参加支付立减5元优惠券活动'
        fixed_claims = parse_discount_claims(fixed_text)
        self.assertEqual([item['mechanism'] for item in fixed_claims], ['fixed_reduction_claim'])
        self.assertEqual(fixed_claims[0]['matched_text'], '立减5元优惠券')
        self.assertEqual(fixed_text[fixed_claims[0]['span_start']:fixed_claims[0]['span_end']],
                         fixed_claims[0]['matched_text'])
        self.assertEqual(parse_discount_claims('20优惠券'), [])

    def test_mixed_real_world_promotion_keeps_subsidy_gift_and_points_separate(self):
        text = ('天猫活动售价14.36元，下单领取6-3优惠券，参与立减6.46元，'
                '官方补贴减1.29元，新品礼金减1元优惠活动，淘金币可抵3.99元起，'
                '下单1件，实付低至2.61元。')
        claims = parse_discount_claims(text)
        by_kind = {item['mechanism']: item for item in claims}
        self.assertEqual(set(by_kind), {
            'threshold_discount_claim', 'fixed_reduction_claim',
            'subsidy_reduction_claim', 'new_product_gift_claim', 'noncash_credit_claim'})
        self.assertEqual((by_kind['threshold_discount_claim']['threshold_cents'],
                          by_kind['threshold_discount_claim']['discount_cents']), (600, 300))
        self.assertEqual(by_kind['fixed_reduction_claim']['discount_cents'], 646)
        self.assertEqual(by_kind['subsidy_reduction_claim']['discount_cents'], 129)
        self.assertEqual(by_kind['new_product_gift_claim']['discount_cents'], 100)
        self.assertEqual(by_kind['noncash_credit_claim']['claimed_equivalent_cents'], 399)
        self.assertEqual(by_kind['noncash_credit_claim']['claim_mode'], 'minimum_claim')
        self.assertFalse(by_kind['noncash_credit_claim']['cash_deductible'])
        for item in claims:
            self.assertEqual(text[item['span_start']:item['span_end']], item['matched_text'])
            self.assertEqual(item['verification'], 'source_claim_only')
            self.assertEqual(item['applicability'], 'not_verified')

    def test_subsidy_rate_cash_cut_and_coin_credit_keep_distinct_semantics(self):
        subsidy_rate = parse_discount_claims('国补10%至15%')[0]
        self.assertEqual(subsidy_rate['mechanism'], 'subsidy_rate_claim')
        self.assertEqual(subsidy_rate['subsidy_rate_basis_points'], 1000)
        self.assertNotIn('discount_cents', subsidy_rate)
        coupon_suffix = parse_discount_claims('补贴10元优惠券')
        self.assertFalse(any(item['mechanism'] == 'subsidy_reduction_claim'
                             for item in coupon_suffix))
        text = '商品面价23.8元，补贴5元，下单自动领取淘金币1.92元'
        claims = parse_discount_claims(text)
        by_kind = {item['mechanism']: item for item in claims}
        self.assertEqual(by_kind['subsidy_reduction_claim']['matched_text'], '补贴5元')
        coin = by_kind['noncash_credit_claim']
        self.assertEqual((coin['matched_text'], coin['claimed_equivalent_cents'], coin['claim_mode']),
                         ('淘金币1.92元', 192, 'source_claimed_equivalent'))
        self.assertFalse(coin['cash_deductible'])
        self.assertEqual(promotion_mentions(text), ['补贴5元'])
        coin_claims = [item for item in parse_discount_claims('14.88元+1.9元淘金币；淘金币1.92元')
                       if item['mechanism'] == 'noncash_credit_claim']
        self.assertEqual([item['matched_text'] for item in coin_claims],
                         ['1.9元淘金币', '淘金币1.92元'])
        self.assertEqual([item['claimed_equivalent_cents'] for item in coin_claims], [190, 192])
        self.assertTrue(all(not item['cash_deductible'] for item in coin_claims))

    def test_item_count_threshold_is_a_distinct_discount_claim(self):
        text = '满1件减1200元；商品面价3999元，购买1件，实付2799元'
        claim = parse_discount_claims(text)[0]
        self.assertEqual(claim['mechanism'], 'quantity_threshold_discount_claim')
        self.assertEqual(claim['threshold_quantity'], 1)
        self.assertEqual(claim['discount_cents'], 120000)
        self.assertEqual(text[claim['span_start']:claim['span_end']], '满1件减1200元')
        self.assertEqual(promotion_mentions(text), ['满1件减1200元'])

    def test_lottery_rewards_remain_probabilistic_and_outside_product_cash_discount(self):
        cases = (
            ('VX搜索未眠野，活动里有一个每日红包抽奖，我4个号都抽到了1元',
             100, 'source_reports_received', False),
            ('预约抽奖2元保底。', 200, 'source_claimed_minimum_reward', True),
            ('活动预约抽奖保底1元有包。', 100, 'source_claimed_minimum_reward', True),
        )
        for text, amount, mode, guaranteed in cases:
            with self.subTest(text=text):
                claim = next(item for item in parse_discount_claims(text)
                             if item['mechanism'] == 'random_reward_claim')
                self.assertEqual(claim['reward_cents'], amount)
                self.assertEqual(claim['claim_mode'], mode)
                self.assertEqual(claim.get('guaranteed_minimum_claimed', False), guaranteed)
                self.assertTrue(claim['probabilistic'])
                self.assertFalse(claim['cash_deductible'])
                self.assertNotIn(claim['matched_text'], promotion_mentions(text))

    def test_limit_is_respected(self):
        claims = parse_discount_claims('满99减10，满199减20，满299减30', limit=2)
        self.assertEqual(len(claims), 2)
        self.assertEqual(parse_discount_claims('满99减10', limit=0), [])


if __name__ == '__main__':
    unittest.main()
