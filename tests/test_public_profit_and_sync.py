import json
import unittest
from datetime import date, datetime, timezone

from autoreview import promotion_sync_status
from offer import product_subcategory
from pricing import public_market_profit


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

    def test_category_subcategory_recognizes_household_paper(self):
        self.assertEqual(product_subcategory('抽纸100抽3层6包', 'home'), '纸品')
        self.assertEqual(product_subcategory('中粮梅林午餐肉198g*3罐', 'food'), '罐头熟食')


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

    def test_conditions_found_is_not_claimed_as_usable(self):
        result = promotion_sync_status('纸巾', '满100减20券')
        self.assertEqual(result['state'], 'conditions_found')
        self.assertIn('不代表已确认可用', result['explanation'])

    def test_old_detail_sync_is_not_labeled_as_current(self):
        result = promotion_sync_status('纸巾', '原文没有优惠', json.dumps({'conditions':'原文没有优惠'}),
                                       '2020-01-01 00:00:00')
        self.assertEqual(result['state'], 'detail_sync_stale')
        self.assertIn('当前没有新详情结果', result['explanation'])


if __name__ == '__main__':
    unittest.main()
