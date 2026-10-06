import unittest
from datetime import datetime,timezone
from pricing import calculate_plan,discount_audit,purchase_terms,promotion_mentions
from autoreview import offer_summary,classify

from scenario_groups import grouped_scenarios

@grouped_scenarios({
    'test_quantity_threshold_and_rounding_cases': (
        'quantity_threshold_and_threshold_not_met',
        'discount_rounding_and_count_scope',
    ),
    'test_ambiguous_and_unsupported_plan_cases_fail_closed': (
        'ambiguous_order_never_chooses_lowest',
        'unsupported_conditions_not_silently_ignored',
        'multiple_prices_and_count_conflicts_not_resolved_by_minimum',
    ),
    'test_coupon_shorthand_stays_bound_to_local_product_text': (
        'coupon_center_shorthand_remains_bound_to_product_text',
        'coupon_context_does_not_leak_across_source_summary_lines',
    ),
    'test_shared_calculation_and_display_fields_stay_aligned': (
        'shared_fields_used_for_display',
        'summary_and_calculation_share_rule_wording',
    ),
})
class SharedPricingTests(unittest.TestCase):
    def body(self,base='7.98',quantity=1,rule='满5.01减5',total='2.98',tail=''):
        return f'商品面价{base}元；领券：{rule}；购买{quantity}件；实付：{total}元。'+tail

    def test_shared_behavior_across_sources_and_products(self):
        samples=[('玻璃罐','7.98','满5.01减5','2.98'),('鸡蛋40枚','51.9','满39元减10元','41.9'),
                 ('洗衣液','20','满20减4','16')]
        urls=['https://new.ixbk.net/haodan/123.html','https://guangdiu.com/detail.php?id=123',
              'https://www.smzdm.com/p/123/','https://another-source.example/item/123']
        for title,base,rule,total in samples:
            for url in urls:
                with self.subTest(title=title,url=url):
                    p=offer_summary(title,url,self.body(base=base,rule=rule,total=total))['audit']['plan']
                    self.assertEqual(p['state'],'conditional_match')
                    self.assertIsNone(p['cases'][0]['cash_cents'])
                    self.assertNotIn('verified',p)

    def _case_quantity_threshold_and_threshold_not_met(self):
        p=calculate_plan(self.body(base='10',quantity=2,rule='满20减5',total='15'))
        self.assertEqual(p['cases'][0]['goods_cents'],1500)
        self.assertEqual(calculate_plan(self.body(base='10',rule='满20减5'))['state'],'blocked')
        self.assertEqual(calculate_plan(self.body(base='1',rule='满0减5'))['state'],'blocked')
        self.assertEqual(calculate_plan(self.body(quantity=0))['state'],'missing')

    def _case_ambiguous_order_never_chooses_lowest(self):
        p=calculate_plan(self.body(base='100',rule='1件8折、满50减10',total='70'))
        self.assertEqual({c['goods_cents'] for c in p['cases']},{7000,7200})
        self.assertEqual(sum(c['matches_claim'] for c in p['cases']),1)
        self.assertNotIn('best_price',p)
        self.assertNotIn('verified',p)

    def _case_discount_rounding_and_count_scope(self):
        p=calculate_plan(self.body(base='5.12',quantity=2,rule='满1件打9.2折',total='9.42'))
        self.assertEqual(p['cases'][0]['goods_cents'],942)
        self.assertEqual(calculate_plan(self.body(quantity=2,rule='1件9折'))['state'],'blocked')
        self.assertEqual(calculate_plan(self.body(rule='1件95折'))['state'],'blocked')

    def test_shipping_unknown_conditional_and_explicit(self):
        for tail,expected in [('',None),('运费6元',898),('包邮',298),('满20包邮',None),
                              ('运费6元，满20包邮',None),('部分地区包邮',None),('不包邮',None),('运费6元至12元',None),('包邮，运费6元',None)]:
            with self.subTest(tail=tail):
                self.assertEqual(calculate_plan(self.body(tail=tail))['cases'][0]['cash_cents'],expected)

    def _case_unsupported_conditions_not_silently_ignored(self):
        for tail in ['还有2元券','可抵1元淘金币','返后0元','需凑单','概率领券','先付定金','补贴3元',
                     '另有满2减1','两张券不可叠加','积分支付','第二件半价']:
            with self.subTest(tail=tail):
                self.assertEqual(calculate_plan(self.body(tail=tail))['state'],'blocked')
        self.assertEqual(calculate_plan(self.body(rule='满5减2、满5减2'))['state'],'blocked')

    def _case_multiple_prices_and_count_conflicts_not_resolved_by_minimum(self):
        self.assertEqual(calculate_plan(self.body(tail='新人实付1.98元'))['state'],'blocked')
        self.assertEqual(calculate_plan(self.body(tail='购买2件'))['state'],'blocked')
        self.assertEqual(calculate_plan(self.body(tail='实付单件1元'))['state'],'blocked')

    def test_mismatch_remains_missing_evidence_not_fake_confirmed_discount(self):
        body=self.body(total='1.98')
        p=calculate_plan(body)
        self.assertEqual(p['state'],'conditional_mismatch')
        self.assertEqual(p['cases'][0]['goods_cents'],298)
        now=datetime.now(timezone.utc).replace(tzinfo=None);stamp=now.strftime('%Y-%m-%d %H:%M:%S')
        row=dict(title='玻璃罐1.98元',url='https://example.com/a',snippet=body,status='pending',
                 published_at=stamp,last_seen_at=stamp,last_success=stamp,enabled=1,
                 source_status='healthy',interval_minutes=10)
        r=classify(row,now)
        self.assertEqual(r['state'],'conditional')
        self.assertIn('不一致',r['reason'])

    def _case_shared_fields_used_for_display(self):
        a=discount_audit('纸巾','目前活动售价10元，下单2件，满20减5，实付低至15元')
        self.assertEqual(a['base_cents'],1000)
        self.assertEqual(a['quantity'],2)
        self.assertEqual(a['plan']['state'],'conditional_match')
        self.assertFalse(any('没有完整扣减计算式' in g for g in a['gaps']))

    def test_reward_trigger_quantity_is_not_cart_quantity(self):
        body='商品面价98.9元，买1件返6元京东E卡，下单2件，实付150.8元'
        self.assertEqual(purchase_terms(body)['quantity'],[2])
        self.assertEqual(calculate_plan(body)['state'],'blocked')

    def _case_summary_and_calculation_share_rule_wording(self):
        body='活动售价5.12元，满9元减3元，满1件打9.2折，下单2件，实付低至6.42元'
        brief=offer_summary('商品3.21元','https://example.com/a',body)
        self.assertIn('满9元减3元',brief['promotions'])
        self.assertIn('满1件打9.2折',brief['promotions'])
        self.assertEqual(brief['promotions'],brief['audit']['coupons'])
        self.assertEqual({c['goods_cents'] for c in brief['audit']['plan']['cases']},{642,666})

    def _case_coupon_center_shorthand_remains_bound_to_product_text(self):
        self.assertEqual(promotion_mentions('领卷中心领5.1-5下单1件'),['满5.1减5'])
        self.assertIn('满5.1减5',promotion_mentions('用5.1-5优惠券，首购-1，下单1件'))
        self.assertIn('首购-1',promotion_mentions('用5.1-5优惠券，首购-1，下单1件'))
        self.assertNotIn('首购-1',promotion_mentions('首购-1kg装湿厕纸'))
        self.assertEqual(promotion_mentions('普通价格区间5.1-5'),[])

    def _case_coupon_context_does_not_leak_across_source_summary_lines(self):
        source='满件折1-0.85'
        duplicated='优惠券满5.1-5券后\n用券\n'+source
        self.assertEqual(promotion_mentions(duplicated),['满5.1减5'])

    def test_source_summary_includes_subsidy_and_new_product_gift_without_duplicate_coupon(self):
        source=('活动售价14.36元，下单领取6-3优惠券，参与立减6.46元，'
                '官方补贴减1.29元，新品礼金减1元优惠活动，淘金币可抵3.99元起。')
        mentions=promotion_mentions(source)
        self.assertEqual(mentions, ['满6减3', '立减6.46元', '官方补贴减1.29元', '新品礼金减1元'])
        self.assertNotIn('淘金币可抵3.99元起', mentions)
