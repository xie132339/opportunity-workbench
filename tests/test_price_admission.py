import json
import unittest
from datetime import datetime,timezone
from unittest.mock import patch
import autoreview as ar
import scanner
import db
import test_xianbao as xb
from app import app,is_current_notice
from services.product_search import is_promotional_price_candidate,listing_quote
from xianbao import parse_items

class PriceRecoveryTests(unittest.TestCase):
    def test_weight_range_is_not_price_range(self):
        self.assertEqual(ar.extract('爱媛38号果冻橙净重4.5-5斤 19.9元')[0],1990)
        self.assertIsNone(ar.extract('抽纸12-20元')[0])
        self.assertIsNone(ar.extract('2-3件价格10元至20元')[0])

    def test_line_feed_purchase_plan_separates_package_and_piece(self):
        title='咸菜罐160ml×3只 2.98元 三个装，折0.99元/个！'
        body='店铺：京喜自营官方店 ,商品面价7.98元\n领券：满5.01减5\n加购：购买 1 件\n实付：2.98元 ( 实付单件2.98元 )'
        now=datetime.now(timezone.utc).replace(tzinfo=None)
        row=dict(title=title,url='https://new.ixbk.net/weibo/123.html',snippet=body,status='pending',
                 published_at=ar.stamp(now),last_seen_at=ar.stamp(now),last_success=ar.stamp(now),
                 enabled=1,source_status='healthy',interval_minutes=1,detail_checked_at=ar.stamp(now),
                 detail_json=json.dumps(dict(title=title,conditions=body,advertised_cents=None)))
        result=ar.classify(row,now)
        self.assertEqual(result['advertised_cents'],298)
        self.assertEqual(result['state'],'conditional')
        offer=ar.public_offer(row['url'],body)
        self.assertEqual((offer['quantity'],offer['unit_cents'],offer['total_cents']),(1,298,298))
        self.assertEqual(offer['store'],'京喜自营官方店')
        self.assertTrue(ar.public_offer(row['url'],body+' 实付单件3.98元')['error'])

    def test_crowdfunding_amount_is_a_separate_non_checkout_candidate_quote(self):
        row={'auto_state':'campaign_upcoming','advertised_cents':50900,
             'auto_reason':'原文标注众筹价；尚未开始。'}
        quote=listing_quote(row,{})
        self.assertEqual(quote['kind'],'campaign_public')
        self.assertEqual(quote['amount_cents'],50900)
        self.assertIn('众筹公开档位价',quote['label'])
        row['display_quote']=quote
        self.assertFalse(is_promotional_price_candidate(row,{}))
        row['auto_state']='campaign_active'
        self.assertTrue(is_promotional_price_candidate(row,{}))

class AdmissionTests(unittest.TestCase):
    setUp=xb.XianbaoTests.setUp
    tearDown=xb.XianbaoTests.tearDown

    def seed(self,title,body,state,price):
        item=xb.sample();item.update(title=title,content=body,content_html='')
        with patch('scanner.xianbao_rows',return_value=parse_items([item])):scanner.scan_source(1)
        with db.connect() as c:c.execute('UPDATE auto_reviews SET state=?,advertised_cents=?',(state,price))

    def test_default_product_search_shows_quotes_and_keeps_unpriced_clues_separate(self):
        self.seed('限时活动5元','没有具体商品和规格','observed',500)
        with app.test_client() as c:
            default=c.get('/?buy_platform=all').get_data(as_text=True)
            self.assertNotIn('/opportunities/1',default)
            self.assertIn('公开优惠',default)
            self.assertIn('当前线索池（含普通价格参考与待补证据）',default)
            self.assertIn('/opportunities/1',c.get('/?buy_platform=all&view=current').get_data(as_text=True))
            self.assertNotIn('/opportunities/1',c.get('/?buy_platform=all&view=ready').get_data(as_text=True))
            self.assertNotIn('/opportunities/1',c.get('/?buy_platform=all&view=offers').get_data(as_text=True))
            self.assertIn('/opportunities/1',c.get('/?buy_platform=all&view=all').get_data(as_text=True))
            self.assertIn('未形成可用报价',c.get('/?buy_platform=all&view=current').get_data(as_text=True))

    def test_missing_price_is_visible_as_a_fresh_lead_not_a_notice(self):
        self.seed('终于活动开始了','没有具体价格','missing_price',None)
        with app.test_client() as c:
            page=c.get('/?buy_platform=all').get_data(as_text=True)
            self.assertNotIn('/opportunities/1',page)
            self.assertNotIn('/opportunities/1',c.get('/?buy_platform=all&view=offers').get_data(as_text=True))
            self.assertNotIn('/opportunities/1',c.get('/?buy_platform=all&view=ready').get_data(as_text=True))
            lead_page=c.get('/?buy_platform=all&view=current').get_data(as_text=True)
            self.assertIn('/opportunities/1',lead_page)
            self.assertIn('未形成可用报价',lead_page)
            self.assertNotIn('金额未提取',lead_page)
            self.assertNotIn('金额口径待确认',lead_page)
            self.assertIn('/opportunities/1',c.get('/?buy_platform=all&view=all').get_data(as_text=True))
            self.assertIn('终于活动开始了',c.get('/verification?state=missing_price').get_data(as_text=True))
        self.assertFalse(is_current_notice(dict(opp_status='pending',auto_state='missing_price')))

    def test_title_price_and_pack_size_do_not_display_a_fake_one_item_order(self):
        self.seed('某品牌抽纸100抽3层6包 5元','原文只有包邮提示，未写购买件数或实付方案','observed',500)
        with app.test_client() as c:
            page=c.get('/?buy_platform=all&view=current&layout=cards').get_data(as_text=True)
            self.assertIn('未形成可用报价',page)
            self.assertIn('原文未提取',page)
            visible=page.split('<details class="offer-evidence">',1)[0]
            self.assertIn('¥5.00',visible)
            self.assertIn('标题金额线索',visible)
            self.assertIn('不参与低价排序',visible)
            self.assertNotIn('金额口径待确认',page)
            self.assertNotIn('金额未提取',page)
            self.assertNotIn('来源原文报价 · 1件',page)
            detail=c.get('/opportunities/1').get_data(as_text=True)
            self.assertIn('标题金额线索',detail)
            self.assertIn('¥5.00',detail)
            self.assertIn('标题金额线索',detail)
            self.assertIn('购买件数',detail)
            self.assertIn('原文未提取',detail)
            self.assertNotIn('金额口径待确认',detail)
            self.assertNotIn('金额未提取',detail)
            self.assertNotIn('单件订单，单价与整单金额相同',detail)
            verification=c.get('/verification?state=observed').get_data(as_text=True)
            self.assertIn('原文金额线索：¥5.00',verification)
            self.assertIn('不作为报价',verification)
            self.assertNotIn('来源原文整单声称价',verification)

    def test_post_cashback_amount_is_a_detail_claim_not_a_current_quote(self):
        title='愛媛38号果冻橙4.5斤装 14.99元（需买2件，需用券）'
        body='此款目前活动售价24.99元，下单领取满40减10元优惠券，下单2件。返现后实付低至29.98元。'
        self.seed(title,body,'conditional',None)
        with app.test_client() as c:
            page=c.get('/opportunities/1').get_data(as_text=True)
            self.assertIn('来源声称返现后净成本 ¥29.98（2件）',page)
            self.assertIn('这不是当前应付价，不用于低价排序或利润判断',page)
            self.assertIn('返现前现金支出、到账时间与返款条件未核实',page)
            self.assertNotIn('来源原文整单报价',page)

    def test_budget_filter_requires_a_displayable_quote_not_an_amount_hint(self):
        self.seed('某品牌抽纸100抽3层6包 5元','标题中的5元没有购买件数或整单价','observed',500)
        with app.test_client() as c:
            clue=c.get('/?buy_platform=all&view=current').get_data(as_text=True)
            self.assertIn('/opportunities/1',clue)
            self.assertIn('¥5.00',clue)
            self.assertIn('标题金额线索',clue)
            self.assertIn('不参与低价排序',clue)
            self.assertNotIn('/opportunities/1',c.get('/?buy_platform=all').get_data(as_text=True))
            self.assertNotIn('/opportunities/1',c.get('/?buy_platform=all&budget=5').get_data(as_text=True))
        with db.connect() as c:
            c.execute("UPDATE events SET snippet='该价格商品规格：100抽3层6包，商品原价10元，活动售价5元，下单1件，实付5元，包邮'")
        with app.test_client() as c:
            budget=c.get('/?buy_platform=all&budget=5').get_data(as_text=True)
            self.assertIn('/opportunities/1',budget)
            self.assertIn('来源原文整单声称价 · 1件',budget)

    def test_queued_price_is_visible_but_not_promoted_to_a_notice(self):
        self.seed('纸巾2元','包邮','queued',200)
        with app.test_client() as c:
            page=c.get('/?buy_platform=all').get_data(as_text=True)
            self.assertNotIn('/opportunities/1',page)
            self.assertIn('/opportunities/1',c.get('/?buy_platform=all&view=current').get_data(as_text=True))
            self.assertNotIn('¥2.00',page)
            self.assertNotIn('/opportunities/1',c.get('/?buy_platform=all&view=ready').get_data(as_text=True))
            lead_page=c.get('/?buy_platform=all&view=current').get_data(as_text=True)
            self.assertIn('/opportunities/1',lead_page)
            self.assertIn('自动复查排队中',lead_page)
            with db.connect() as sql:sql.execute("UPDATE auto_reviews SET state='observed'")
            self.assertNotIn('/opportunities/1',c.get('/?buy_platform=all').get_data(as_text=True))
            self.assertIn('/opportunities/1',c.get('/?buy_platform=all&view=current').get_data(as_text=True))
            self.assertIn('/opportunities/1',c.get('/?buy_platform=all&view=all').get_data(as_text=True))
        self.assertFalse(is_current_notice(dict(opp_status='pending',auto_state='queued')))

    def test_conflicted_fresh_price_stays_visible_with_conflict_state(self):
        self.seed('纸巾2元','来源正文明确实付3元','conflict',200)
        with app.test_client() as c:
            page=c.get('/?buy_platform=all&view=current').get_data(as_text=True)
            self.assertIn('/opportunities/1',page)
            self.assertIn('原文报价／规格冲突',page)
            self.assertNotIn('/opportunities/1',c.get('/?buy_platform=all&view=ready').get_data(as_text=True))
        self.assertFalse(is_current_notice(dict(opp_status='pending',auto_state='conflict')))

    def test_activity_not_forced_into_cash_price(self):
        self.seed('100元打车券包','领取优惠券','activity',None)
        with app.test_client() as c:
            self.assertNotIn('/opportunities/1',c.get('/?buy_platform=all&resource=coupon').get_data(as_text=True))
            self.assertIn('/opportunities/1',c.get('/?buy_platform=all&view=all&resource=coupon').get_data(as_text=True))
            self.assertNotIn('/opportunities/1',c.get('/?buy_platform=all&resource=purchase').get_data(as_text=True))
            self.assertNotIn('价格未明确',c.get('/?buy_platform=all&resource=coupon').get_data(as_text=True))

    def test_source_price_visible_without_retired_public_history_widget(self):
        body='该价格商品规格：100抽3层6包\n商品原价10元，活动售价5元，下单1件，实付5元，包邮'
        self.seed('某品牌抽纸100抽3层6包 5元',body,'observed',500)
        with app.test_client() as c:
            text=c.get('/?buy_platform=all').get_data(as_text=True)
            self.assertIn('/opportunities/1',text)
            self.assertIn('公开优惠',text)
            self.assertIn('入口抓取正常',text)
            self.assertIn('不代表优惠或捡漏已核实',text)
            self.assertIn('来源原文整单声称价 · 1件',text)
            self.assertIn('¥5.00',text)
            self.assertIn('aria-label="商品结果展示方式"',text)
            self.assertIn('列表每页 100 条，卡片每页 36 条',text)
            self.assertIn('线索来源<select name="platform"',text)
            self.assertIn('不是对京东、淘宝、拼多多等商城实时全站搜价',text)
            self.assertNotIn('已核实整单价',text)
            verified=c.get('/?buy_platform=all&view=ready').get_data(as_text=True)
            self.assertIn('/opportunities/1',verified)
            self.assertIn('自动分析不会等待商家人工核实',verified)
            archive=c.get('/?buy_platform=all&view=all&layout=cards').get_data(as_text=True)
            self.assertIn('/opportunities/1',archive);self.assertIn('来源原文整单声称价 · 1件',archive)
            cards=c.get('/?buy_platform=all&layout=cards').get_data(as_text=True)
            self.assertIn('来源原文整单声称价 · 1件',cards)
            self.assertNotIn('公开商品页观测：',cards)
            self.assertIn('来源原文整单声称价 · 1件',cards)
            detail=c.get('/opportunities/1').get_data(as_text=True)
            self.assertIn('本商品当前结论',detail);self.assertIn('自动分析资料',detail);self.assertIn('同口径价格比较',detail)

        with db.connect() as sql:
            sql.execute("UPDATE auto_reviews SET state='conditional'")
            sql.execute("UPDATE events SET snippet=snippet || '\n随机领2元券'")
        with app.test_client() as c:
            text=c.get('/?buy_platform=all').get_data(as_text=True)
            self.assertNotIn('/opportunities/1',text)
            self.assertIn('当前没有通过完整准入的公开优惠',text)
            self.assertIn('/opportunities/1',c.get('/?buy_platform=all&view=current').get_data(as_text=True))
            self.assertIn('条件报价（含优惠／资格限制）',c.get('/?buy_platform=all&view=current').get_data(as_text=True))
            self.assertNotIn('/opportunities/1',c.get('/?buy_platform=all&view=ready').get_data(as_text=True))
            archive=c.get('/?buy_platform=all&view=all&layout=cards').get_data(as_text=True)
            self.assertIn('/opportunities/1',archive);self.assertIn('优惠条件无法复算',archive)
            detail=c.get('/opportunities/1').get_data(as_text=True)
            self.assertIn('本商品当前结论',detail)
            self.assertIn('优惠条件无法复算',detail)

    def test_promotion_candidate_requires_a_bound_price_reduction_not_promo_copy(self):
        row=dict(display_quote=dict(kind='source_claim',amount_cents=2000),catalog_listing=False,
                 title='日常商品 20元',snippet='',auto_conditions='')
        brief=dict(promotions=[],qualifications=[],previous_cents=None,kind='purchase')
        self.assertFalse(is_promotional_price_candidate(row,brief))
        self.assertFalse(is_promotional_price_candidate(
            dict(row,title='日常商品限时活动价 20元'),brief))
        self.assertFalse(is_promotional_price_candidate(row,dict(brief,promotions=['满30减10'])))
        self.assertTrue(is_promotional_price_candidate(
            dict(row,auto_conditions='降价前售价为30元，券后20元'),
            dict(brief,previous_cents=3000,quantity=1)))
        self.assertTrue(is_promotional_price_candidate(
            dict(row,auto_conditions='原价30元，券后20元'),dict(brief,quantity=1)))
        self.assertFalse(is_promotional_price_candidate(
            dict(row,auto_conditions='原价30元，券后20元，下单2件，实付40元'),
            dict(brief,quantity=2)))
        self.assertFalse(is_promotional_price_candidate(
            dict(row,display_quote=dict(kind='source_claim',amount_cents=None),
                 title='日常商品限时活动价 20元'),brief))
        formula_brief=dict(brief, audit=dict(base_cents=3000, quantity=1,
            formula=dict(state='consistent',expression='30.00 − 10.00 = 20.00元',
                         calculated_cents=2000,claimed_cents=2000)))
        self.assertTrue(is_promotional_price_candidate(row,formula_brief))
        plan_brief=dict(brief,quantity=2,audit=dict(base_cents=3000,quantity=2,
            plan=dict(state='conditional_match',cases=[dict(matches_claim=True,
                 cash_cents=2000,goods_cents=4000)])))
        self.assertTrue(is_promotional_price_candidate(row,plan_brief))
        incomplete_plan=dict(plan_brief,audit=dict(base_cents=3000,quantity=2,
            plan=dict(state='conditional_match',cases=[dict(matches_claim=True,cash_cents=2000)])))
        self.assertFalse(is_promotional_price_candidate(row,incomplete_plan))
        mismatched_plan=dict(plan_brief,audit=dict(base_cents=3000,quantity=1,
            plan=dict(state='conditional_match',cases=[dict(matches_claim=True,
                 cash_cents=2000,goods_cents=4000)])))
        self.assertFalse(is_promotional_price_candidate(row,mismatched_plan))
        self.assertFalse(is_promotional_price_candidate(
            dict(row,catalog_listing=True,title='目录商品限时活动价 20元'),
            dict(brief,promotions=['满30减10'])))
        self.assertFalse(is_promotional_price_candidate(
            dict(display_quote=dict(kind='catalog_public',amount_cents=2000),catalog_listing=True,
                 title='目录商品 20元'), brief))
        self.assertTrue(is_promotional_price_candidate(
            dict(display_quote=dict(kind='catalog_public',amount_cents=2000),catalog_listing=True,
                 title='目录商品 原价30元，活动价20元'), dict(brief,quantity=None)))

    def test_title_spec_is_displayed_as_hint_and_not_as_selected_variant(self):
        self.seed('某品牌抽纸100抽3层6包 5元','活动售价5元，下单1件，实付5元，包邮','observed',500)
        with app.test_client() as c:
            current=c.get('/?buy_platform=all&view=current').get_data(as_text=True)
            self.assertIn('标题规格线索：100抽 × 3层 × 6包（未确认报价对应变体）',current)
            self.assertNotIn('原文明示报价规格：100抽 × 3层 × 6包',current)
            self.assertIn('/opportunities/1',current)
            self.assertNotIn('/opportunities/1',c.get('/?buy_platform=all&view=ready').get_data(as_text=True))
            detail=c.get('/opportunities/1').get_data(as_text=True)
            self.assertIn('标题规格线索',detail)
            self.assertIn('不能据此完成同款比较',detail)

    def test_keyword_search_also_checks_captured_source_copy(self):
        self.seed('限时优惠活动','纸巾30抽十包，商品价格9.9元','observed',990)
        with app.test_client() as c:
            text=c.get('/?buy_platform=all&q=纸&view=current').get_data(as_text=True)
            self.assertIn('/opportunities/1',text)
            self.assertIn('限时优惠活动',text)

    def test_single_item_price_is_not_rendered_twice(self):
        body='该价格商品规格：100抽3层6包\n活动售价5元，下单1件，实付5元，包邮'
        self.seed('某品牌抽纸100抽3层6包 5元',body,'observed',500)
        with app.test_client() as c:
            text=c.get('/?buy_platform=all&view=all&layout=cards').get_data(as_text=True)
        self.assertIn('来源原文整单声称价 · 1件',text)
        self.assertNotIn('折合每件（由整单换算）',text)
        self.assertEqual(text.count('¥5.00'),1)
        with app.test_client() as c:
            detail=c.get('/opportunities/1').get_data(as_text=True)
        self.assertIn('来源原文声称价 · 1件',detail)
        self.assertNotIn('<th>折合每件</th>',detail)

    def test_multi_item_price_keeps_unit_and_order_amount(self):
        body='该价格商品规格：100抽3层6包\n活动售价5元，下单2件，实付10元，包邮'
        self.seed('某品牌抽纸100抽3层6包 5元',body,'observed',1000)
        with app.test_client() as c:
            text=c.get('/?buy_platform=all&view=all&layout=cards').get_data(as_text=True)
        self.assertIn('整单声称价算术均摊 · 非商家单价',text)
        self.assertIn('来源原文整单声称价 · 2件',text)
        self.assertIn('¥5.00',text)
        self.assertIn('¥10.00',text)
        with app.test_client() as c:
            detail=c.get('/opportunities/1').get_data(as_text=True)
        self.assertIn('整单 ¥10.00',detail)
        self.assertIn('整单声称价算术均摊 · 不代表该SKU商家单价',detail)
        self.assertIn('¥5.00',detail)
