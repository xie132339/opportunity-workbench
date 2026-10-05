import json
import unittest
from comparison import merchant_identity,comparison_index,quantity_options,assess_readiness
import test_comparison as fixtures

class MarketMethodsTests(unittest.TestCase):
    def row(self,i,sku=None,**kw):
        row=fixtures.ComparisonTests().row(i,**kw)
        if sku:row['metadata_json']=json.dumps({'activity_links':['https://item.jd.com/'+sku+'.html']})
        return row

    def test_identity_rejects_shop_coupon_fake_hosts(self):
        row=self.row(1);row['metadata_json']=json.dumps({'activity_links':['https://item.jd.com.evil.example/123.html','https://coupon.m.jd.com/?skuId=123','https://shop.m.jd.com/?venderid=123','https://evil.example/?u=https://item.jd.com/123.html']})
        self.assertTrue(merchant_identity(row)['key'].startswith('catalog:'))
        row['metadata_json']=json.dumps({'activity_links':['https://item.m.jd.com/ware/view.action?wareId=12345']})
        self.assertEqual(merchant_identity(row)['key'],'jd:12345')

    def test_same_sku_different_title_can_be_grouped(self):
        rows=[self.row(1,'12345'),self.row(2,'12345',title='另一写法同一商品 4元',total='8')]
        r=comparison_index(rows);self.assertIsNone(r[1]['best_id'])
        self.assertEqual(r[1]['peers'],1)
        self.assertIn('同一商家商品ID',r[1]['message'])
        self.assertIn('12345',r[1]['identity_label'])

    def test_same_title_different_sku_never_merged(self):
        r=comparison_index([self.row(1,'12345'),self.row(2,'67890',total='8')])
        self.assertIsNone(r[1]['best_id']);self.assertEqual(len(r[1]['items']),1)

    def test_multiple_skus_are_ambiguous(self):
        row=self.row(1);row['metadata_json']=json.dumps({'activity_links':['https://item.jd.com/12345.html','https://item.jd.com/67890.html']})
        r=comparison_index([row]);self.assertTrue(r[1]['items'][0]['problems']);self.assertIsNone(r[1]['best_id'])

    def test_explicit_product_ids_from_domestic_markets(self):
        cases={
            'https://item.taobao.com/item.htm?id=123':'taobao:123',
            'https://detail.tmall.com/item.htm?id=456':'taobao:456',
            'https://mobile.yangkeduo.com/goods.html?goods_id=789':'pdd:789',
            'https://product.suning.com/0000000000/11128387984.html':'suning:0000000000:11128387984',
            'https://detail.vip.com/detail-1710618487-6920028890971952983.html':'vip:1710618487:6920028890971952983',
        }
        for url,key in cases.items():
            row=self.row(1);row['metadata_json']=json.dumps({'activity_links':[url]})
            self.assertEqual(merchant_identity(row)['key'],key,url)

    def item(self,i,total,q,shipping=None,**kw):
        d=dict(id=i,total_cents=total,quantity=q,shipping_cents=shipping,problems=[],optimization_gaps=[],partition=('',(),q));d.update(kw);return d

    def test_pareto_does_not_confuse_low_total_with_low_unit(self):
        a=self.item(1,1000,1);b=self.item(2,1500,2);c=self.item(3,1800,2)
        r=quantity_options([a,b,c],a)
        self.assertEqual(r['lowest_total'],1);self.assertEqual(r['lowest_unit'],2)
        self.assertEqual(set(r['frontier']),{1,2});self.assertIn('运费不全',r['reason'])

    def test_known_shipping_changes_winner(self):
        a=self.item(1,1000,1,600);b=self.item(2,1200,1,0)
        self.assertEqual(quantity_options([a,b],a)['lowest_total'],2)

    def test_ineligible_stale_or_missing_rules_cannot_win(self):
        a=self.item(1,1000,1);b=self.item(2,100,5,problems=['过期']);c=self.item(3,100,5,optimization_gaps=['凑单总额未知'])
        d=self.item(4,100,5,partition=('',('新客',),5))
        r=quantity_options([a,b,c,d],a);self.assertEqual(r['count'],1);self.assertEqual(r['lowest_unit'],1)

    def test_real_addon_example_is_not_quantity_recommendation(self):
        row=self.row(1,title='AXE洗洁精1.01kg*3瓶 22.44元',total='112.2',quantity=5,tail='黑五补贴200-20，需凑单儿童尤克里里')
        r=comparison_index([row]);self.assertEqual(r[1]['quantity_options']['count'],0)
        self.assertTrue(r[1]['items'][0]['optimization_gaps'])

    def test_search_admission_requires_complete_product_and_reproducible_discount(self):
        direct=self.row(1,title='某品牌抽纸100抽3层6包 5元',total='5',quantity=1,auto_state='observed')
        comparisons=comparison_index([direct])
        assessment=assess_readiness(direct,comparisons[1])
        self.assertTrue(assessment['search_ready']);self.assertFalse(assessment['comparable'])
        missing=self.row(2,title='某品牌纸巾 5元',total='5',quantity=1,auto_state='observed')
        self.assertFalse(assess_readiness(missing,comparison_index([missing])[2])['search_ready'])
        conditional=self.row(3,title='某品牌抽纸100抽3层6包 5元',total='5',quantity=1,
                             auto_state='conditional',tail='随机领2元券')
        failed=assess_readiness(conditional,comparison_index([conditional])[3])
        self.assertFalse(failed['search_ready']);self.assertIn('优惠条件无法复算', '；'.join(failed['failures']))
        matched=self.row(4,title='某品牌抽纸100抽3层6包 7.5元',total='15',quantity=2,
                         auto_state='conditional',tail='满2件减5元')
        matched_comparison=comparison_index([matched])[4]
        unbound=assess_readiness(matched,matched_comparison)
        self.assertFalse(unbound['search_ready'])
        self.assertIn('条件优惠缺商家商品ID', '；'.join(matched_comparison['items'][0]['problems']))
        self.assertIn('条件优惠缺商家商品ID', '；'.join(unbound['search_failures']))
        self.assertNotIn('时效、来源状态', '；'.join(unbound['search_failures']))
        bound=self.row(5,'12345',title='某品牌抽纸100抽3层6包 7.5元',total='15',quantity=2,
                       auto_state='conditional',tail='满2件减5元')
        bound_assessment=assess_readiness(bound,comparison_index([bound])[5])
        self.assertTrue(bound_assessment['search_ready']);self.assertFalse(bound_assessment['comparable'])

    def test_aggregator_posts_for_one_merchant_sku_are_not_independent_savings_options(self):
        rows=[self.row(1,'12345',total='4.37'),self.row(2,'12345',total='4.45')]
        result=comparison_index(rows)
        self.assertEqual(result[1]['peers'],1)
        self.assertIsNone(result[1]['best_id'])
        self.assertEqual(result[1]['saving_cents'],0)
        self.assertIn('不能判断省钱或捡漏',result[1]['message'])

    def test_unrecalculated_optimization_gap_cannot_be_a_comparison_peer(self):
        a=self.row(1,total='4.37')
        b=self.row(2,total='4.00',tail='需凑单其他商品')
        result=comparison_index([a,b])
        self.assertIsNone(result[1]['best_id'])
        self.assertEqual(result[1]['peers'],1)
        self.assertTrue(next(i for i in result[1]['items'] if i['id']==2)['optimization_gaps'])
