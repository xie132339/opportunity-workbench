import unittest
import json
from datetime import datetime,timedelta,timezone
from unittest.mock import patch,MagicMock
from comparison import comparison_index,product_key
from services.category_policy import default_policy, encode_policy, resolve_category_policy
from services.category_comparison_rules import default_rule, encode_rule, normalized_spec
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

    def test_category_policy_controls_minimum_comparable_offers(self):
        leaf_policy=default_policy();leaf_policy['minimum_comparable_offers']=3
        categories=[dict(id=1,name='纸品',topic_key='home',enabled=1,
                         match_terms_json='["抽纸","卷纸"]',policy_json=encode_policy(leaf_policy))]
        policy,category_name,error,scope=resolve_category_policy('某品牌抽纸100抽3层6包','home',categories,
                                                                  encode_policy(default_policy()))
        self.assertEqual((category_name,error,scope),('纸品',None,'category'))
        first=self.row(1,'10',title='某品牌抽纸100抽3层6包 5元',
                       metadata_json='{"activity_links":["https://item.jd.com/123.html"]}',
                       resolved_category_policy=policy,resolved_category_name=category_name,
                       resolved_policy_scope=scope)
        second=self.row(2,'8',title='某品牌抽纸100抽3层6包 5元',
                        metadata_json='{"activity_links":["https://detail.tmall.com/item.htm?id=456"]}',
                        resolved_category_policy=policy,resolved_category_name=category_name,
                        resolved_policy_scope=scope)
        result=comparison_index([first,second])
        self.assertEqual(result[1]['required_comparable_offers'],3)
        self.assertIsNone(result[1]['best_id'])
        self.assertEqual(result[1]['comparison_basis'],'source_claim')
        self.assertEqual((result[1]['resolved_category_name'],result[1]['policy_scope']),('纸品','category'))

    def test_configured_measure_normalizes_packages_and_keeps_variant_identity(self):
        rule=dict(default_rule(),unit_mode='count',count_unit='抽')
        first=self.row(1,'10',title='某品牌抽纸100抽3层6包 10元',selected_spec='100抽 × 3层 × 6包',quantity=1,
                        metadata_json='{"activity_links":["https://item.jd.com/123.html"]}')
        second=self.row(2,'8',title='某品牌抽纸200抽3层3包 8元',selected_spec='200抽 × 3层 × 3包',quantity=1,
                         metadata_json='{"activity_links":["https://detail.tmall.com/item.htm?id=456"]}')
        for row in (first,second):
            row['resolved_comparison_rule']=rule
            row['resolved_category_name']='纸品'
        result=comparison_index([first,second])
        self.assertEqual(result[1]['best_id'],2)
        self.assertEqual(result[1]['items'][0]['normalized_base_unit'],'抽')
        self.assertIn('按每抽归一',result[1]['message'])
        self.assertEqual(result[1]['quantity_options']['count'],0)

    def test_ambiguous_or_incompatible_measure_is_blocked(self):
        rule=dict(default_rule(),unit_mode='mass')
        first=self.row(1,'10',title='某品牌米200克 10元',selected_spec='200克',
                        metadata_json='{"activity_links":["https://item.jd.com/123.html"]}')
        second=self.row(2,'8',title='某品牌米300克 8元',selected_spec='300克',
                         metadata_json='{"activity_links":["https://detail.tmall.com/item.htm?id=456"]}')
        for row in (first,second):
            row['resolved_comparison_rule']=rule
            row['resolved_category_name']='食品'
        invalid=self.row(3,'1',title='某品牌米200克 1元',selected_spec='200克 / 300克')
        invalid['resolved_comparison_rule']=rule
        invalid['resolved_category_name']='食品'
        result=comparison_index([first,second,invalid])
        self.assertEqual(result[1]['best_id'],2)
        bad=next(item for item in result[3]['items'] if item['id']==3)
        self.assertTrue(any('无歧义换算' in issue for issue in bad['problems']))
        self.assertIsNone(result[3]['best_id'])
        from services.category_comparison_rules import normalized_spec
        rule=dict(default_rule(),unit_mode='mass')
        kg=normalized_spec('某牌奶粉0.4kg 2罐','0.4kg × 2罐',rule)
        grams=normalized_spec('某牌奶粉800g','800克',rule)
        self.assertEqual((kg[0],kg[1],kg[2]),(grams[0],grams[1],grams[2]))
        self.assertEqual(kg[2],grams[2])
        conflict=normalized_spec('某牌奶粉400g','800克',rule)
        self.assertIn('标题计价规格与选中报价规格冲突',conflict[3])

    def test_identity_policy_can_forbid_title_only_cross_platform_match(self):
        rule=dict(default_rule(),identity_mode='merchant_id_only')
        title='某品牌抽纸100抽3层6包 5元'
        rows=[self.row(1,'10',title=title),self.row(2,'8',title=title)]
        for row in rows:
            row['resolved_comparison_rule']=rule
        result=comparison_index(rows)
        self.assertIsNone(result[1]['best_id'])
        self.assertEqual(result[1]['peers'],1)  # includes the current offer; no second candidate

    def test_unit_normalization_does_not_fuzz_unconfigured_attributes(self):
        rule=dict(default_rule(),unit_mode='mass')
        a=normalized_spec('某牌奶粉 400g 2罐','奶粉段数3段 400g 2罐',rule)
        b=normalized_spec('某牌奶粉 800g','奶粉段数2段 800g 1罐',rule)
        self.assertEqual(a[0],b[0])
        self.assertNotEqual(a[1],b[1])
        self.assertEqual((str(a[2]),a[3]),('800',None))

    def test_ambiguous_category_keyword_uses_broad_policy(self):
        categories=[
            dict(id=1,name='抽纸',topic_key='home',enabled=1,match_terms_json='["抽纸"]',policy_json='{}'),
            dict(id=2,name='纸巾套装',topic_key='home',enabled=1,match_terms_json='["抽纸"]',policy_json='{}'),
        ]
        policy,name,error,scope=resolve_category_policy('抽纸套装','home',categories,encode_policy(default_policy()))
        self.assertIsNone(name)
        self.assertEqual(scope,'topic')
        self.assertIsNone(error)
        self.assertEqual(policy,default_policy())

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

    def test_catalog_currentness_uses_recent_successful_observation(self):
        now=datetime.now(timezone.utc).replace(tzinfo=None)
        title='米家冰箱 对开636L 1899元'
        row=self.row(1,title=title,url='https://www.mi.com/shop/buy?product_id=22504',
                     source_parser='mi',published_at=None,
                     last_seen_at=now.strftime('%Y-%m-%d %H:%M:%S'),
                     last_success=now.strftime('%Y-%m-%d %H:%M:%S'),platform='小米商城')
        result=comparison_index([row],now=now)
        item=result[1]['items'][0]
        self.assertTrue(item['source_current'])
        self.assertIsNone(result[1]['best_id'])

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
