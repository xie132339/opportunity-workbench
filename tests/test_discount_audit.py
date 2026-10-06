import json
import unittest
from datetime import datetime
from unittest.mock import patch

import autoreview as ar
from pricing import discount_audit
from xianbao import extract_links
import test_xianbao as xb
sample = xb.sample
from xianbao import parse_items
import scanner
import db
from app import app

BODY = '京东app搜索 商品标题或关键字，部分有限时补贴3券(概率)，要加购领券\n秒杀会场找到此商品专享价加购，(直达)：\nhttps://u.jd.com/FGLrYPy\n百家好世 扫把簸箕套装组合，再砸落5券，2.4\nhttps://u.jd.com/FGLytTW'
HTML = '<article class="art-main"><h1 class="art-title">扫把套装 2.4元</h1><time class="time" title="2026-10-04 10:02:02"></time><div class="article-content">'+BODY+'</div><div class="art-copyright"><strong class="addr">原文地址：</strong><a href="https://m.weibo.cn/detail/5350236814843988">原帖</a></div></article><div class="comment">实付0元</div>'

from scenario_groups import grouped_scenarios

@grouped_scenarios({
    'test_non_cash_rewards_and_title_percentages_are_not_cash_discounts': (
        'probability_never_reverse_calculates_base',
        'real_face_towel_title_composition_percent_is_not_a_discount',
    ),
    'test_equation_replay_checks_arithmetic_not_truth': (
        'explicit_equation_only_checks_arithmetic',
        'conflicting_equation_isolated',
    ),
    'test_conditional_promotion_replay_preserves_eligibility_and_header_boundaries': (
        'seckill_coupon_and_first_order_gift_reproduce_claim_without_verifying_eligibility',
        'live_first_order_shorthand_can_be_reconciled_but_lottery_stays_a_risk',
        'generated_offer_tag_header_does_not_create_a_fake_unparsed_gift',
    ),
    'test_incompatible_discount_combinations_remain_blocked': (
        'seckill_price_wins_over_regular_price_but_subsidy_mix_stays_blocked',
        'multiple_qualifications_no_single_formula',
    ),
})
class DiscountAuditTests(unittest.TestCase):

    def _case_probability_never_reverse_calculates_base(self):
        a=discount_audit('扫把2.4元',BODY)
        self.assertIsNone(a['base_cents']);self.assertNotIn('verified',a)
        self.assertIn('概率优惠，不能保证领到',a['risks'])
        self.assertIn('补贴3券',a['coupons']);self.assertIn('砸落5券',a['coupons'])
        self.assertEqual(a['formula']['state'],'missing')
        self.assertTrue(any('会场' in s for s in a['steps']))
        self.assertTrue(any('运费' in s for s in a['gaps']))

    def _case_explicit_equation_only_checks_arithmetic(self):
        a=discount_audit('扫把2.4元','商品面价10.4元 购买1件。价格计算：10.4元-3元-5元=2.4元。运费另计')
        self.assertEqual(a['formula']['calculated_cents'],240)
        self.assertEqual(a['formula']['state'],'consistent');self.assertNotIn('verified',a)
        self.assertEqual(a['base_cents'],1040);self.assertEqual(a['quantity'],1)
        self.assertEqual(discount_audit('','价格计算：18.9-3-5=2.4元')['formula']['state'],'conflict')
        self.assertEqual(discount_audit('','价格计算：2-3=-1元')['formula']['state'],'missing')
        self.assertEqual(discount_audit('','满100减20券，8.5折，到手65元')['formula']['state'],'missing')

    def _case_seckill_coupon_and_first_order_gift_reproduce_claim_without_verifying_eligibility(self):
        body=('京东此款目前活动售价秒杀价4.9元，下单领取满3.1-3优惠券，首礼金1元，'
              '下单1件，实付低至0.9元。直达为原价，需要从首页秒杀频道加购下单。')
        audit=discount_audit('全棉时代洗脸巾20抽*1包',body)
        plan=audit['plan']
        self.assertEqual(plan['state'],'conditional_match')
        self.assertEqual(plan['fields']['base'],[490])
        self.assertEqual(plan['fields']['gift'],[100])
        self.assertEqual(plan['claimed_cents'],90)
        self.assertTrue(any(case['matches_claim'] is True and case['goods_cents']==90 for case in plan['cases']))
        self.assertTrue(all(case['cash_cents'] is None for case in plan['cases']))
        self.assertIsNone(plan['shipping_cents'])
        self.assertTrue(audit['uncertain'])
        self.assertIn('限新客或首单', audit['risks'])
        self.assertIn('券适用商品、领取资格及叠加顺序尚无商家规则依据', audit['gaps'])
        self.assertTrue(any('资格' in note for case in plan['cases'] for note in case['notes']))

    def _case_generated_offer_tag_header_does_not_create_a_fake_unparsed_gift(self):
        conditions=('券后'+chr(10)+'首购、会员、用券、礼金'+chr(10)+'需秒杀频道购买：京东此款日常售价10.9元，目前秒杀活动6.93元，'
                    '下单领取首购礼金1元，领取满5.1减5元家清优惠券，下单1件，Plus会员包邮，实付低至0.93元。')
        plan=discount_audit('立白洗衣粉680g',conditions)['plan']
        self.assertEqual(plan['state'],'conditional_match')
        self.assertEqual(plan['fields']['base'],[693])
        self.assertEqual(plan['claimed_cents'],93)
        self.assertIsNone(plan['shipping_cents'])
        self.assertTrue(any(case['matches_claim'] for case in plan['cases']))

    def _case_real_face_towel_title_composition_percent_is_not_a_discount(self):
        title=('全棉时代 洗脸巾20抽*1包加厚100%纯棉柔巾擦脸面巾旅行装便携小包20*20CM 券后0.9元')
        conditions=('券后'+chr(10)+'用券、礼金'+chr(10)+'京东此款目前活动售价秒杀价4.9元，下单领取满3.1-3优惠券，'
                    '首礼金1元，下单1件，实付低至0.9元。直达为原价，需要从首页 秒杀频道 加购下单 '
                    '可用券及活动：满3.1-3、秒杀价4.9、首礼金1 前往购买')
        plan=discount_audit(title,conditions)['plan']
        self.assertEqual(plan['state'],'conditional_match')
        self.assertEqual(plan['fields']['base'],[490])
        self.assertEqual(plan['claimed_cents'],90)
        self.assertTrue(any(case['matches_claim'] for case in plan['cases']))
        self.assertIsNone(plan['shipping_cents'])
        self.assertTrue(any('首购/新客礼金' in note for case in plan['cases'] for note in case['notes']))

    def _case_live_first_order_shorthand_can_be_reconciled_but_lottery_stays_a_risk(self):
        body=('京东秒杀价7.1元，用5.1-5优惠券，首购-1，下单1件，实付低至1.1元。'
              '优惠券获取方式：详情页弹或加购砸蛋。')
        audit=discount_audit('维达湿厕纸7片*8包',body)
        plan=audit['plan']
        self.assertEqual(plan['state'],'conditional_match')
        self.assertEqual(plan['fields']['base'],[710])
        self.assertEqual(plan['fields']['gift'],[100])
        self.assertTrue(plan['fields']['gift_unit_assumption'])
        self.assertTrue(any(c['matches_claim'] and c['goods_cents']==110 for c in plan['cases']))
        self.assertTrue(any('单位未在原文标注' in note for c in plan['cases'] for note in c['notes']))
        self.assertIn('概率优惠，不能保证领到',audit['risks'])
        self.assertTrue(audit['uncertain'])
        self.assertEqual(ar.qualification_mentions(body), ['首购', '详情页弹', '加购砸蛋', '秒杀'])

    def _case_seckill_price_wins_over_regular_price_but_subsidy_mix_stays_blocked(self):
        body=('日常售价10.9元，目前秒杀活动6.93元，下单领取满5.1减5元优惠券，'
              '首购礼金1元，下单1件，实付低至0.93元。')
        audit=discount_audit('立白洗衣粉680g',body)
        self.assertEqual(audit['plan']['fields']['base'],[693])
        self.assertEqual(audit['plan']['state'],'conditional_match')
        complex_body=('活动售价8.46元，领取8-4元券，满1件打8.8折，首购礼金2元，'
                      '国家补贴15%，下单1件，实付低至1.23元。')
        self.assertEqual(discount_audit('开关插座',complex_body)['plan']['state'],'blocked')

    def _case_multiple_qualifications_no_single_formula(self):
        a=discount_audit('新人会员价','价格计算：10-3=7元。价格计算：10-5=5元')
        self.assertEqual(a['formula']['state'],'missing')
        self.assertIn('限新客或首单',a['risks'])
        self.assertIn('需要会员或特定权益',a['risks'])

    def test_plain_links_and_article_only(self):
        links=extract_links('<p>https://u.jd.com/FGLrYPy</p><a href="https://u.jd.com/FGLrYPy">重复</a><a href="javascript:alert(1)">x</a>')
        self.assertEqual(links,['https://u.jd.com/FGLrYPy'])
        detail=ar.parse_xianbao_detail(HTML)
        self.assertEqual(len(detail['activity_links']),2)
        self.assertNotIn('实付0元',detail['conditions'])
        self.assertEqual(detail['published_at'],'2026-10-04 02:02:02')
        self.assertIn('weibo.cn',detail['origin_url'])
        with self.assertRaises(ValueError):ar.parse_xianbao_detail('<h1>请登录</h1>')

    def _case_conflicting_equation_isolated(self):
        now=datetime(2026,10,4,2,10)
        r=dict(title='商品2.4元',url='https://example.com/a',snippet='价格计算：18.9-3-5=2.4元',
               status='pending',published_at=ar.stamp(now),last_seen_at=ar.stamp(now),last_success=ar.stamp(now),
               enabled=1,source_status='healthy',interval_minutes=10)
        self.assertEqual(ar.classify(r,now)['state'],'conflict')

class DetailPipelineTests(unittest.TestCase):
    setUp = xb.XianbaoTests.setUp
    tearDown = xb.XianbaoTests.tearDown
    # Reuse only the isolated database fixture, not inherited test methods.
    def test_targeted_auto_probe_and_visible_conditions(self):
        item=sample();item.update(title='扫把套装2.4元',content=BODY)
        with patch('scanner.xianbao_rows',return_value=parse_items([item])):scanner.scan_source(1)
        current_html = HTML.replace('2026-10-04 10:02:02',datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        with patch('scanner._fetch',return_value=current_html):
            result=ar.run_cycle(opportunity_id=1)
        self.assertEqual(result['probed'],1)
        with db.connect() as c:
            review=c.execute('SELECT * FROM auto_reviews WHERE opportunity_id=1').fetchone()
            self.assertIsNotNone(review['detail_checked_at'])
            self.assertEqual(review['state'],'conditional')
            self.assertEqual(len(json.loads(review['detail_json'])['activity_links']),2)
            self.assertEqual(c.execute('SELECT count(*) FROM buy_checks').fetchone()[0],0)
        with app.test_client() as client:
            text=client.get('/opportunities/1').get_data(as_text=True)
            self.assertIn('概率优惠，不能保证领到',text)
            self.assertIn('无法复算',text)
            self.assertIn('https://u.jd.com/FGLytTW',text)
            self.assertIn('来源优惠条件试算',text)
        # A new article fetch must not refresh the time on an older merchant observation.
        with db.connect() as c:
            saved=json.loads(c.execute('SELECT detail_json FROM auto_reviews WHERE opportunity_id=1').fetchone()[0])
            saved['external_checks']=[{'checked_at':'2026-10-01 01:00:00','price_cents':1890}]
            c.execute("UPDATE auto_reviews SET detail_json=?,detail_checked_at=datetime('now','-31 minutes'),next_check_at=NULL WHERE opportunity_id=1",(json.dumps(saved),))
        with patch('scanner._fetch',return_value=current_html):
            self.assertEqual(ar.run_cycle(opportunity_id=1)['probed'],1)
        with db.connect() as c:
            saved=json.loads(c.execute('SELECT detail_json FROM auto_reviews WHERE opportunity_id=1').fetchone()[0])
            self.assertEqual(saved['external_checks'][0]['checked_at'],'2026-10-01 01:00:00')

    def test_source_linked_benefit_page_shows_each_candidate_without_claiming_usability(self):
        from benefits import add
        item=sample();item.update(title='垃圾袋14.36元',content='券后价2.61元')
        with patch('scanner.xianbao_rows',return_value=parse_items([item])):
            scanner.scan_source(1)
        raw=('领取6-3优惠券，立减6.46元，官方补贴减1.29元，新品礼金减1元，'
             '领取20元优惠券，淘金币可抵3.99元起。')
        add('垃圾袋优惠入口','https://u.jd.com/ClaimDetail',source_opportunity_id=1,
            origin_text=raw,kind='coupon')
        with app.test_client() as client:
            detail=client.get('/opportunities/1').get_data(as_text=True)
        self.assertIn('自动抽取 6 条优惠候选（逐条未核实）',detail)
        self.assertIn('官方补贴减1.29元',detail)
        self.assertIn('淘金币可抵3.99元起',detail)
        self.assertIn('不计入现金扣款',detail)
        self.assertIn('不证明券有效、商品适用',detail)

    def test_face_wipe_candidate_shows_math_qualification_gaps_and_correct_category(self):
        item=sample()
        item.update(title='全棉时代 洗脸巾20抽*1包 加厚 0.9元',
                    content=('京东目前活动售价秒杀价4.9元，下单领取满3.1-3优惠券，首礼金1元，'
                             '下单1件，实付低至0.9元。直达为原价，需要从首页秒杀频道加购下单。'),
                    catename='微博线报-其他-京东')
        rows=parse_items([item])
        with patch('scanner.xianbao_rows',return_value=rows):
            scanner.scan_source(1)
        ar.review_all()
        with db.connect() as c:
            saved=c.execute('SELECT topic FROM opportunities WHERE id=1').fetchone()
            self.assertEqual(saved['topic'],'home')
        with app.test_client() as client:
            detail=client.get('/opportunities/1').get_data(as_text=True)
            card=client.get('/?q=全棉时代&topic=home&layout=cards&view=current').get_data(as_text=True)
        self.assertIn('来源声称金额的算术可复算',detail)
        self.assertIn('本人资格',detail)
        self.assertIn('运费金额或适用地区未明确',detail)
        self.assertIn('清洁巾与棉柔巾',detail)
        self.assertIn('¥0.90',card)
        self.assertIn('/包',card)
        self.assertIn('¥4.50',card)
        self.assertIn('/百抽',card)
        self.assertIn('金额可复算 · 叠加/资格/运费待核',card)

    def test_old_saved_other_topic_is_still_found_by_current_home_filter(self):
        item=sample()
        item.update(title='全棉时代 洗脸巾20抽*1包加厚100%纯棉 券后0.9元',
                    content=('京东秒杀价4.9元，下单领取满3.1-3优惠券，首礼金1元，'
                             '下单1件，实付低至0.9元。需要从首页秒杀频道加购下单。'),
                    catename='微博线报-其他-京东')
        with patch('scanner.xianbao_rows',return_value=parse_items([item])):
            scanner.scan_source(1)
        ar.review_all()
        # Reproduce the real stale row without rewriting its stored history.
        with db.connect() as c:
            c.execute("UPDATE opportunities SET topic='other' WHERE id=1")
        with app.test_client() as client:
            detail=client.get('/opportunities/1').get_data(as_text=True)
            listing=client.get('/?q=全棉时代&topic=home&layout=list&view=current').get_data(as_text=True)
            cards=client.get('/?q=全棉时代&topic=home&layout=cards&view=current').get_data(as_text=True)
            excluded=client.get('/?q=全棉时代&topic=food&layout=list&view=current').get_data(as_text=True)
        self.assertIn('家用日用品',detail)
        self.assertIn('/opportunities/1',listing)
        self.assertIn('来源声称金额的算术可复算',detail)
        self.assertIn('金额可复算 · 叠加/资格/运费待核',cards)
        self.assertNotIn('/opportunities/1',excluded)
        with db.connect() as c:
            self.assertEqual(c.execute('SELECT topic FROM opportunities WHERE id=1').fetchone()[0],'other')
