import json
import unittest
from datetime import date, datetime, timezone

from autoreview import promotion_sync_status, qualification_mentions
from offer import product_subcategory, resource_topic
from pricing import public_market_profit
from services.opportunity_analysis import public_profit_estimate

from scenario_groups import grouped_scenarios

@grouped_scenarios({
    'test_category_subcategories_cover_representative_products': (
        'category_subcategory_recognizes_household_paper',
        'packaging_bag_does_not_misclassify_face_wipes_as_apparel',
        'common_personal_care_and_fruit_examples_get_price_categories',
        'home_electrical_hardware_has_a_specific_subcategory',
    ),
})
class PublicProfitTests(unittest.TestCase):
    def setUp(self):
        self.opp = dict(specification='TEST SKU 6包', sell_shipping_cents=0,
                        platform_fee_cents=0, processing_cents=0,
                        other_cents=0, reserve_cents=0)
        self.quotes = [dict(kind='sold', amount_cents=3200-i*100, same_spec=1,
                            evidence_url=f'https://example.test/sold/{i}',
                            conditions='隔离合成样本', specification='TEST SKU 6包',
                            price_at=date.today().isoformat(), valid_until=None)
                       for i in range(3)]

    def test_sold_mode_uses_three_independent_same_spec_sources_and_one_formula(self):
        result = public_market_profit(self.opp, self.quotes, 2000, 'TEST SKU 6包', 'sold', 0)
        self.assertEqual(result['amount_cents'], 1000)
        self.assertEqual(result['sample_count'], 3)
        self.assertEqual(result['market_cents'], 3000)
        self.assertIn('平台费', result['missing_costs'])

    def test_missing_market_samples_does_not_invent_a_profit(self):
        result = public_market_profit(self.opp, self.quotes[:2], 2000, 'TEST SKU 6包', 'sold', None)
        self.assertIsNone(result['amount_cents'])
        self.assertIn('至少需要 3 条', result['reason'])

    def test_listing_mode_uses_only_listing_quotes(self):
        listings = [dict(q, kind='listing') for q in self.quotes]
        result = public_market_profit(self.opp, listings, 2000, 'TEST SKU 6包', 'listing', 0)
        self.assertEqual(result['mode'], 'listing')
        self.assertEqual(result['amount_cents'], 1000)

    def test_source_headline_amount_is_not_promoted_to_total_profit_input(self):
        now=datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        opp=dict(source_method='rss',source_enabled=1,source_status='healthy',source_interval=10,
                 source_last_success=now,last_seen_at=now,published_at=now,
                 category='零售优惠',topic='home',title='抽纸100抽3层6包 20元')
        brief=dict(title=opp['title'],total_cents=None,quantity=None,selected_spec='')
        review=dict(advertised_cents=2000,specification='100抽3层6包',detail_json=None)
        result=public_profit_estimate(opp,self.quotes,brief,review)
        self.assertIsNone(result['amount_cents'])
        self.assertIsNone(result['buy_cents'])
        self.assertIn('整单报价未解析',result['reason'])

    def _case_category_subcategory_recognizes_household_paper(self):
        self.assertEqual(product_subcategory('抽纸100抽3层6包', 'home'), '纸品')
        self.assertEqual(product_subcategory('中粮梅林午餐肉198g*3罐', 'food'), '罐头熟食')

    def _case_packaging_bag_does_not_misclassify_face_wipes_as_apparel(self):
        title='全棉时代 洗脸巾20抽*1包加厚 20*20CM 旅行装'
        topic=resource_topic(title)
        self.assertEqual(topic,'home')
        self.assertEqual(product_subcategory(title,topic),'清洁巾与棉柔巾')
        self.assertEqual(product_subcategory('旅行双肩包 防水背包','other'),'服饰鞋包')

    def _case_common_personal_care_and_fruit_examples_get_price_categories(self):
        bath = '京东京造 艾叶爽肤沁润沐浴露 800ml*2件'
        topic = resource_topic(bath, '国内折扣 / 海外折扣')
        self.assertEqual(topic, 'beauty')
        self.assertEqual(product_subcategory(bath, topic), '洗护')
        toothpaste = '参半3天白高纯度葡萄籽牙膏120g 任拍3件'
        topic = resource_topic(toothpaste, '食品优惠')
        self.assertEqual(topic, 'beauty')
        self.assertEqual(product_subcategory(toothpaste, topic), '洗护')
        oranges = '巨无霸 四川眉山 特大果 爱媛38号果冻橙 4.5斤装'
        topic = resource_topic(oranges, '公开优惠线索')
        self.assertEqual(topic, 'food')
        self.assertEqual(product_subcategory(oranges, topic), '生鲜食品')

    def _case_home_electrical_hardware_has_a_specific_subcategory(self):
        title='西门子开关插座面板 致典系列雅白色 一开单控'
        topic=resource_topic(title)
        self.assertEqual(topic,'home')
        self.assertEqual(product_subcategory(title,topic),'家居五金与电工')

@grouped_scenarios({
    'test_offer_conditions_and_eligibility_do_not_claim_usability': (
        'conditions_found_is_not_claimed_as_usable',
        'coupon_requirement_and_required_item_count_are_visible_but_not_amounts',
        'explicitly_not_required_coupon_is_not_reported_as_a_condition',
        'entry_and_trial_routes_are_visible_without_becoming_cash_discounts',
        'new_user_gift_is_counted_as_benefit_and_eligibility_evidence',
    ),
    'test_lottery_claims_remain_noncash_and_unconfirmed': (
        'source_claimed_lottery_minimum_is_not_a_coupon_or_product_discount',
        'lottery_reward_is_counted_but_not_as_cash_discount',
    ),
    'test_stale_detail_sync_time_cases': (
        'malformed_detail_sync_time_is_stale_instead_of_crashing',
        'old_detail_sync_is_not_labeled_as_current',
    ),
})
class PromotionSyncStatusTests(unittest.TestCase):
    def test_distinguishes_not_synced_summary_synced_and_details_synced(self):
        self.assertEqual(promotion_sync_status('纸巾', '')['state'], 'not_synced')
        summary = promotion_sync_status('纸巾', '原文没有优惠')
        self.assertEqual(summary['state'], 'summary_synced_empty')
        self.assertIn('详情页尚未成功同步', summary['explanation'])
        detail = promotion_sync_status('纸巾', '原文没有优惠', json.dumps({'conditions':'原文没有优惠'}),
                                       datetime.now(timezone.utc).isoformat())
        self.assertEqual(detail['state'], 'detail_synced_empty')
        self.assertEqual(promotion_sync_status('纸巾', '原文没有优惠', detail_error='HTTP 502')['state'], 'detail_sync_failed')

    def _case_conditions_found_is_not_claimed_as_usable(self):
        result = promotion_sync_status('纸巾', '满100减20券')
        self.assertEqual(result['state'], 'conditions_found')
        self.assertIn('不代表已确认可用', result['explanation'])

    def _case_coupon_requirement_and_required_item_count_are_visible_but_not_amounts(self):
        result = promotion_sync_status('多芬 深层营润美肤沐浴露',
            '16.83元（88VIP需用券，需买2件，券后价14.99元）')
        self.assertEqual(result['state'], 'conditions_found')
        self.assertEqual(result['promotions'], [])
        self.assertEqual(result['qualifications'], ['88VIP', '需用券', '券后价', '需买2件'])
        self.assertIn('0 条优惠提及、4 条资格/入口提示', result['explanation'])

    def _case_explicitly_not_required_coupon_is_not_reported_as_a_condition(self):
        self.assertEqual(qualification_mentions('无需用券，买1件即可'), [])

    def _case_entry_and_trial_routes_are_visible_without_becoming_cash_discounts(self):
        result = promotion_sync_status('威王洁厕块',
            '秒杀频道专享，需从频道加购；来源实付1元。')
        self.assertEqual(result['state'], 'conditions_found')
        self.assertIn('秒杀', result['qualifications'])
        self.assertIn('加购', result['qualifications'])
        self.assertEqual(result['promotions'], [])
        trial = promotion_sync_status('理肤泉试用装', '试用价1元，仅本场活动')
        self.assertIn('试用', trial['qualifications'])

    def _case_source_claimed_lottery_minimum_is_not_a_coupon_or_product_discount(self):
        result = promotion_sync_status('预约抽奖活动', '预约抽奖2元保底，有包')
        self.assertEqual(result['state'], 'conditions_found')
        self.assertEqual(result['promotions'], [])
        self.assertEqual(result['random_reward_claims'][0]['reward_cents'], 200)
        self.assertEqual(result['random_reward_claims'][0]['claim_mode'], 'source_claimed_minimum_reward')
        self.assertFalse(result['random_reward_claims'][0]['cash_deductible'])

    def _case_lottery_reward_is_counted_but_not_as_cash_discount(self):
        result = promotion_sync_status('每日红包抽奖', '4个号都抽到了1元')
        self.assertEqual(result['state'], 'conditions_found')
        self.assertEqual(result['random_reward_claims'][0]['reward_cents'], 100)
        self.assertFalse(result['random_reward_claims'][0]['cash_deductible'])
        self.assertIn('随机奖励声称', result['explanation'])

    def _case_new_user_gift_is_counted_as_benefit_and_eligibility_evidence(self):
        result = promotion_sync_status('全棉时代洗脸巾',
            '秒杀价4.9元，满3.1减3优惠券，首礼金1元，下单1件，实付0.9元')
        self.assertEqual(result['promotions'], ['满3.1减3','首礼金1元'])
        self.assertIn('首礼金', result['qualifications'])
        self.assertIn('2 条优惠提及、2 条资格/入口提示', result['explanation'])

    def test_mixed_source_claims_are_counted_once_and_non_cash_items_stay_separate(self):
        result = promotion_sync_status('抽纸垃圾袋',
            '活动售价14.36元，下单领取6-3优惠券，参与立减6.46元，官方补贴减1.29元，'
            '新品礼金减1元优惠活动，领取20元优惠券，淘金币可抵3.99元起。')
        self.assertEqual(result['state'], 'conditions_found')
        self.assertEqual(result['promotions'], [
            '满6减3', '立减6.46元', '官方补贴减1.29元', '新品礼金减1元'])
        self.assertEqual([claim['matched_text'] for claim in result['coupon_face_claims']],
                         ['领取20元优惠券'])
        self.assertEqual([claim['matched_text'] for claim in result['noncash_credit_claims']],
                         ['淘金币可抵3.99元起'])
        self.assertIn('4 条优惠提及', result['explanation'])
        self.assertIn('券面额线索（面额不等于实际减免）', result['explanation'])
        self.assertIn('非现金', result['explanation'])

    def _case_malformed_detail_sync_time_is_stale_instead_of_crashing(self):
        result = promotion_sync_status('纸巾', '满100减20券',
                                       json.dumps({'conditions':'满100减20券'}),
                                       'not-a-timestamp')
        self.assertEqual(result['state'], 'detail_sync_stale')
        self.assertIn('时间无法解析', result['explanation'])

    def _case_old_detail_sync_is_not_labeled_as_current(self):
        result = promotion_sync_status('纸巾', '原文没有优惠', json.dumps({'conditions':'原文没有优惠'}),
                                       '2020-01-01 00:00:00')
        self.assertEqual(result['state'], 'detail_sync_stale')
        self.assertIn('当前没有新详情结果', result['explanation'])

if __name__ == '__main__':
    unittest.main()
