import unittest
from datetime import datetime,timedelta,timezone
from unittest.mock import patch,MagicMock
from comparison import comparison_index,product_key
from pricing import calculate_plan,promotion_mentions,discount_audit
from autoreview import public_offer,structured_spec
import scanner

class ComparisonTests(unittest.TestCase):
    def row(self,i,total='10',quantity=2,title='某品牌 抽纸 100抽3层6包 5元',tail='',**extra):
        now=datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        spec=extra.pop('selected_spec',structured_spec(title))
        selected=f'该价格商品规格：{spec} 京东商城。' if spec else ''
        r=dict(id=i,title=title,url=f'https://guangdiu.com/detail.php?id={i}',snippet=selected+f'活动售价10元，下单{quantity}件，实付{total}元。'+tail,
            detail_json='{}',metadata_json='{}',auto_state='observed',status='pending',platform='逛丢',
            enabled=1,source_status='healthy',interval_minutes=10,published_at=now,last_seen_at=now,last_success=now)
        r.update(extra);return r

    def test_compares_full_order_not_title_price(self):
        r=comparison_index([self.row(1,'10'),self.row(2,'8')])
        self.assertEqual(r[1]['saving_cents'],200);self.assertEqual(r[1]['best_id'],2)
        self.assertEqual(r[2]['best_id'],2)
        self.assertEqual(r[2]['saving_cents'],0)

    def test_different_counts_and_qualifications_do_not_compete(self):
        for changed in [self.row(2,'8',quantity=3),self.row(2,'8',tail='限新客首单'),self.row(2,'8',tail='仅限北京地区')]:
            r=comparison_index([self.row(1),changed]);self.assertIsNone(r[1]['best_id'])

    def test_variant_and_spec_not_fuzzy_matched(self):
        r=comparison_index([self.row(1),self.row(2,title='某品牌 抽纸 80抽3层6包 4元')])
        self.assertEqual(len(r[1]['items']),1)
        self.assertNotEqual(product_key('型号A1 6GB 100元'),product_key('型号A1 8GB 90元'))

    def test_old_failed_future_quotes_do_not_win(self):
        for extra in [dict(published_at='2020-01-01 00:00:00'),dict(source_status='failed'),dict(auto_state='conflict'),dict(published_at='2099-01-01 00:00:00')]:
            r=comparison_index([self.row(1),self.row(2,'1',**extra)])
            self.assertIsNone(r[1]['best_id']);self.assertTrue(next(i for i in r[1]['items'] if i['id']==2)['problems'])

    def test_same_url_snapshots_not_two_independent_offers(self):
        r=comparison_index([self.row(1),self.row(2,'8',url='https://guangdiu.com/detail.php?id=1')])
        self.assertEqual(len(r[2]['items']),1);self.assertIsNone(r[2]['best_id'])

    def test_unknown_shipping_stays_unknown(self):
        r=comparison_index([self.row(1),self.row(2,'8')]);self.assertIsNone(r[1]['items'][0]['shipping_cents'])

    def test_generic_explicit_plan_across_sources(self):
        for u in ['https://www.smzdm.com/p/123/','https://another.example/item/1']:
            r=public_offer(u,'下单2件，实付10元，实付单件5元')
            self.assertEqual((r['total_cents'],r['quantity']),(1000,2))
        self.assertEqual(public_offer('https://another.example/item/1','实付单件5元'),{})

    def test_coupon_shorthand_and_original_threshold(self):
        p=calculate_plan('活动售价5.97元，下单领取29-10优惠券，满1件，打7.9折，下单5件，实付13.58元')
        self.assertEqual(p['state'],'conditional_match');self.assertNotIn('verified',p)
        self.assertIn('满1件打7.9折',promotion_mentions('满1件，打7.9折'))
        self.assertIn('满29减10',promotion_mentions('下单领取29-10优惠券'))
        c=next(c for c in p['cases'] if c['matches_claim']);self.assertEqual(c['goods_cents'],1358)
        self.assertIn('折前金额',c['order']);self.assertIn('尚无公开规则证据',c['notes'][0])
        self.assertEqual(calculate_plan('活动售价5元，下单2件，重量4-5斤，实付10元')['state'],'missing')

    def test_rss_retains_links_and_long_rule_text(self):
        body='<p>'+('规则文字'*400)+'</p><a href="https://guangdiu.com/to.php?u=https%3A%2F%2Fcoupon.m.jd.com%2Fcoupons%2Fshow.action%3Fkey%3Dpublic">券</a><a href="javascript:alert(1)">坏链接</a><a href="https://guangdiu.com/go.php?id=1">购买</a>'
        response=MagicMock(status_code=200,content=b'feed')
        session=MagicMock();session.__enter__.return_value=session;session.get.return_value=response
        feed=MagicMock(bozo=False,entries=[dict(title='商品测试',link='https://guangdiu.com/detail.php?id=1',summary=body)])
        with patch('scanner.requests.Session',return_value=session),patch('scanner.feedparser.parse',return_value=feed):
            r=scanner._rss_rows('http://127.0.0.1:1200/test','http://127.0.0.1:1200')[0]
        self.assertGreater(len(r[3]),1500)
        self.assertEqual(r[5]['activity_links'],['https://coupon.m.jd.com/coupons/show.action?key=public','https://guangdiu.com/go.php?id=1'])
        self.assertFalse(r[5]['content_truncated'])

    def test_identical_quotes_not_claimed_as_discount(self):
        r=comparison_index([self.row(1),self.row(2)])
        self.assertEqual(r[1]['saving_cents'],0);self.assertIn('相同',r[1]['message'])

    def test_lowest_source_claim_is_not_misreported_as_equal(self):
        r=comparison_index([self.row(1,'10'),self.row(2,'8')])
        self.assertIn('价为候选中最低',r[2]['message'])
        self.assertIn('价高2.00元',r[2]['message'])
        self.assertNotIn('声称价相同',r[2]['message'])
        self.assertIn('声称价相差2.00元',r[1]['message'])

    def test_checkout_step_does_not_become_account_qualification(self):
        sku='{"activity_links":["https://item.jd.com/123.html"]}'
        r=comparison_index([self.row(1,'10',tail='需要加购',metadata_json=sku),self.row(2,'8',metadata_json=sku)])
        self.assertEqual(r[1]['saving_cents'],0)
        self.assertIsNone(r[1]['best_id'])
        self.assertIn('同一商家商品ID',r[1]['message'])

    def test_exact_title_and_explicit_variant_can_compare_across_marketplace_ids_as_candidate(self):
        title='某品牌抽纸100抽3层6包 5元'
        jd='{"activity_links":["https://item.jd.com/123.html"]}'
        tm='{"activity_links":["https://detail.tmall.com/item.htm?id=456"]}'
        rows=[self.row(1,'10',title=title,metadata_json=jd),
              self.row(2,'8',title=title,metadata_json=tm)]
        result=comparison_index(rows)
        self.assertEqual(result[1]['best_id'],2)
        self.assertEqual(result[1]['saving_cents'],200)
        self.assertIn('商家SKU未核验',result[1]['message'])
        self.assertIn('非商家SKU核验',result[1]['identity_label'])

    def test_truncated_rules_do_not_compete(self):
        r=comparison_index([self.row(1),self.row(2,'8',metadata_json='{"content_truncated":true}')])
        self.assertIsNone(r[1]['best_id'])

    def test_random_product_color_is_not_random_coupon(self):
        self.assertNotIn('概率优惠，不能保证领到',discount_audit('垃圾袋颜色随机','下单2件')['risks'])
        self.assertIn('概率优惠，不能保证领到',discount_audit('垃圾袋','随机领券')['risks'])
