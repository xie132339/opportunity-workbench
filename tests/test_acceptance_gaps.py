import unittest,json
from pricing import calculate_plan,promotion_mentions,shipping_claim
from acceptance import freeze,evaluate
import test_comparison

class AcceptanceGapTests(unittest.TestCase):
    def test_quantity_reduction_and_coupon_both_count(self):
        b='活动售价109.9元，满1件减40元，领取满99减20元优惠券，下单1件，实付49.9元。'
        p=calculate_plan(b);self.assertEqual(p['state'],'conditional_match');self.assertNotIn('verified',p)
        self.assertTrue(any(c['goods_cents']==4990 for c in p['cases']));self.assertIn('满1件减40元',promotion_mentions(b))
    def test_quantity_reduction_has_own_gate_and_never_repeats(self):
        for q,state,value in [(1,'blocked',None),(2,'conditional_match',1500),(4,'conditional_mismatch',3500)]:
            p=calculate_plan(f'活动售价10元，下单{q}件，满2件减5元，实付15元')
            self.assertEqual(p['state'],state)
            if value is not None:self.assertEqual(p['cases'][0]['goods_cents'],value)
        p=calculate_plan('活动售价10元，下单2件，满2件减5元，每满2件减5元，实付10元');self.assertEqual(p['state'],'blocked')
    def test_shipping_extracted_even_without_discount_formula(self):
        for text,expected in [('拍下29.9元包邮；折5.98元/支',0),('【7.9包邮】',0),('下单1件，运费5元',500),('满99包邮',None),('部分地区包邮',None),('不包邮',None),('领包邮券',None),('不支持包邮',None),('运费5元，包邮',None),('运费5元起',None)]:
            self.assertEqual(calculate_plan(text)['shipping_cents'],expected,text)
            self.assertNotIn('verified',calculate_plan(text))
    def test_unknown_rules_cannot_pass_business_acceptance(self):
        r=test_comparison.ComparisonTests().row(1,title='某品牌 抽纸100抽 5元',tail='包邮');r['topic']='home';r['advertised_cents']=1000
        r['metadata_json']=json.dumps({'activity_links':['https://item.jd.com/123.html']})
        cohort=freeze([r]);result=evaluate([r],cohort,{})
        self.assertEqual(result['sample_size'],1);self.assertEqual(result['passed'],0);self.assertFalse(result['coverage_passed'])
        self.assertNotIn('merchant_rules',result['items'][0]['checks'])
    def test_disappearing_frozen_sample_counts_failure(self):
        r=test_comparison.ComparisonTests().row(1);r['topic']='home';r['advertised_cents']=1000
        result=evaluate([],freeze([r]),{})
        self.assertEqual(result['sample_size'],1);self.assertEqual(result['passed'],0);self.assertIn('样本已缺失',result['items'][0]['failures'])
    def test_duplicate_title_does_not_fill_sample_quota(self):
        a=test_comparison.ComparisonTests().row(1);b=test_comparison.ComparisonTests().row(2);a['topic']=b['topic']='home'
        self.assertEqual(len(freeze([b,a])['items']),1)

    def test_payment_coupon_is_not_product_cash_price(self):
        from offer import resource_kind
        self.assertEqual(resource_kind('京东工行省钱卡1买6元'),'coupon')
        self.assertEqual(resource_kind('支付券6元'),'coupon')
