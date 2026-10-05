import json
import unittest
from datetime import datetime,timezone
from unittest.mock import patch
import autoreview as ar
import scanner
import db
import test_xianbao as xb
from app import app,is_current_notice
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

class AdmissionTests(unittest.TestCase):
    setUp=xb.XianbaoTests.setUp
    tearDown=xb.XianbaoTests.tearDown

    def seed(self,title,body,state,price):
        item=xb.sample();item.update(title=title,content=body,content_html='')
        with patch('scanner.xianbao_rows',return_value=parse_items([item])):scanner.scan_source(1)
        with db.connect() as c:c.execute('UPDATE auto_reviews SET state=?,advertised_cents=?',(state,price))

    def test_default_product_search_uses_evidence_gate_and_keeps_lead_inbox(self):
        self.seed('限时活动5元','没有具体商品和规格','observed',500)
        with app.test_client() as c:
            self.assertIn('/opportunities/1',c.get('/').get_data(as_text=True))
            self.assertNotIn('/opportunities/1',c.get('/?view=ready').get_data(as_text=True))
            self.assertIn('/opportunities/1',c.get('/?view=current').get_data(as_text=True))
            self.assertIn('/opportunities/1',c.get('/?view=all').get_data(as_text=True))
            self.assertIn('资料达到商品搜索准入 0 条',c.get('/').get_data(as_text=True))

    def test_missing_price_is_visible_as_a_fresh_lead_not_a_notice(self):
        self.seed('终于活动开始了','没有具体价格','missing_price',None)
        with app.test_client() as c:
            page=c.get('/').get_data(as_text=True)
            self.assertIn('/opportunities/1',page)
            self.assertNotIn('/opportunities/1',c.get('/?view=ready').get_data(as_text=True))
            lead_page=c.get('/?view=current').get_data(as_text=True)
            self.assertIn('/opportunities/1',lead_page)
            self.assertIn('金额未提取',lead_page)
            self.assertIn('缺结构化价格来源',lead_page)
            self.assertIn('/opportunities/1',c.get('/?view=all').get_data(as_text=True))
            self.assertIn('终于活动开始了',c.get('/verification?state=missing_price').get_data(as_text=True))
        self.assertFalse(is_current_notice(dict(opp_status='pending',auto_state='missing_price')))

    def test_queued_price_is_visible_but_not_promoted_to_a_notice(self):
        self.seed('纸巾2元','包邮','queued',200)
        with app.test_client() as c:
            page=c.get('/').get_data(as_text=True)
            self.assertIn('/opportunities/1',page)
            self.assertNotIn('/opportunities/1',c.get('/?view=ready').get_data(as_text=True))
            lead_page=c.get('/?view=current').get_data(as_text=True)
            self.assertIn('/opportunities/1',lead_page)
            self.assertIn('自动复查排队中',lead_page)
            with db.connect() as sql:sql.execute("UPDATE auto_reviews SET state='observed'")
            self.assertIn('/opportunities/1',c.get('/').get_data(as_text=True))
            self.assertIn('/opportunities/1',c.get('/?view=current').get_data(as_text=True))
            self.assertIn('/opportunities/1',c.get('/?view=all').get_data(as_text=True))
        self.assertFalse(is_current_notice(dict(opp_status='pending',auto_state='queued')))

    def test_conflicted_fresh_price_stays_visible_with_conflict_state(self):
        self.seed('纸巾2元','来源正文明确实付3元','conflict',200)
        with app.test_client() as c:
            page=c.get('/?view=current').get_data(as_text=True)
            self.assertIn('/opportunities/1',page)
            self.assertIn('原文报价／规格冲突',page)
            self.assertNotIn('/opportunities/1',c.get('/?view=ready').get_data(as_text=True))
        self.assertFalse(is_current_notice(dict(opp_status='pending',auto_state='conflict')))

    def test_activity_not_forced_into_cash_price(self):
        self.seed('100元打车券包','领取优惠券','activity',None)
        with app.test_client() as c:
            self.assertNotIn('/opportunities/1',c.get('/?resource=coupon').get_data(as_text=True))
            self.assertIn('/opportunities/1',c.get('/?view=all&resource=coupon').get_data(as_text=True))
            self.assertNotIn('/opportunities/1',c.get('/?resource=purchase').get_data(as_text=True))
            self.assertNotIn('价格未明确',c.get('/?resource=coupon').get_data(as_text=True))

    def test_source_price_visible_without_retired_public_history_widget(self):
        body='该价格商品规格：100抽3层6包\n活动售价5元，下单1件，实付5元，包邮'
        self.seed('某品牌抽纸100抽3层6包 5元',body,'observed',500)
        with app.test_client() as c:
            text=c.get('/').get_data(as_text=True)
            self.assertIn('/opportunities/1',text)
            self.assertIn('商品搜索（仅资料达到准入）',text)
            self.assertIn('来源原文声称',text)
            self.assertIn('¥5.00',text)
            self.assertIn('aria-label="商品结果展示方式"',text)
            self.assertIn('列表每页 100 条，卡片每页 36 条',text)
            self.assertIn('线索来源<select name="platform"',text)
            self.assertIn('不是对京东、淘宝、拼多多等商城实时全站搜价',text)
            self.assertNotIn('已核实整单价',text)
            verified=c.get('/?view=ready').get_data(as_text=True)
            self.assertIn('/opportunities/1',verified)
            self.assertIn('自动分析不会等待商家人工核实',verified)
            archive=c.get('/?view=all&layout=cards').get_data(as_text=True)
            self.assertIn('/opportunities/1',archive);self.assertIn('来源原文报价 · 1件',archive)
            cards=c.get('/?layout=cards').get_data(as_text=True)
            self.assertIn('可分析来源报价',cards)
            self.assertNotIn('公开商品页观测：',cards)
            self.assertIn('来源原文报价 · 1件',cards)
            detail=c.get('/opportunities/1').get_data(as_text=True)
            self.assertIn('本商品当前结论',detail);self.assertIn('自动分析资料',detail);self.assertIn('同口径价格比较',detail)

        with db.connect() as sql:
            sql.execute("UPDATE auto_reviews SET state='conditional'")
            sql.execute("UPDATE events SET snippet=snippet || '\n随机领2元券'")
        with app.test_client() as c:
            text=c.get('/').get_data(as_text=True)
            self.assertIn('/opportunities/1',text)
            self.assertIn('/opportunities/1',c.get('/?view=current').get_data(as_text=True))
            self.assertIn('条件报价（含优惠／资格限制）',c.get('/?view=current').get_data(as_text=True))
            self.assertNotIn('/opportunities/1',c.get('/?view=ready').get_data(as_text=True))
            archive=c.get('/?view=all&layout=cards').get_data(as_text=True)
            self.assertIn('/opportunities/1',archive);self.assertIn('优惠条件无法复算',archive)
            detail=c.get('/opportunities/1').get_data(as_text=True)
            self.assertIn('本商品当前结论',detail)
            self.assertIn('优惠条件无法复算',detail)

    def test_title_spec_is_displayed_as_hint_and_not_as_selected_variant(self):
        self.seed('某品牌抽纸100抽3层6包 5元','活动售价5元，下单1件，实付5元，包邮','observed',500)
        with app.test_client() as c:
            current=c.get('/?view=current').get_data(as_text=True)
            self.assertIn('标题规格线索：100抽 × 3层 × 6包（未确认报价对应变体）',current)
            self.assertNotIn('原文明示报价规格：100抽 × 3层 × 6包',current)
            self.assertIn('/opportunities/1',current)
            self.assertNotIn('/opportunities/1',c.get('/?view=ready').get_data(as_text=True))
            detail=c.get('/opportunities/1').get_data(as_text=True)
            self.assertIn('标题规格线索',detail)
            self.assertIn('不能据此完成同款比较',detail)

    def test_keyword_search_also_checks_captured_source_copy(self):
        self.seed('限时优惠活动','纸巾30抽十包，商品价格9.9元','observed',990)
        with app.test_client() as c:
            text=c.get('/?q=纸&view=current').get_data(as_text=True)
            self.assertIn('/opportunities/1',text)
            self.assertIn('限时优惠活动',text)

    def test_single_item_price_is_not_rendered_twice(self):
        body='该价格商品规格：100抽3层6包\n活动售价5元，下单1件，实付5元，包邮'
        self.seed('某品牌抽纸100抽3层6包 5元',body,'observed',500)
        with app.test_client() as c:
            text=c.get('/?view=all&layout=cards').get_data(as_text=True)
        self.assertIn('来源原文报价 · 1件',text)
        self.assertIn('单件订单，单价与整单金额相同，不重复展示；此处显示来源公开声称值。',text)
        self.assertNotIn('折合每件（由整单换算）',text)
        self.assertEqual(text.count('¥5.00'),1)
        with app.test_client() as c:
            detail=c.get('/opportunities/1').get_data(as_text=True)
        self.assertIn('1件订单，不重复列单价',detail)
        self.assertNotIn('<th>折合每件</th>',detail)

    def test_multi_item_price_keeps_unit_and_order_amount(self):
        body='该价格商品规格：100抽3层6包\n活动售价5元，下单2件，实付10元，包邮'
        self.seed('某品牌抽纸100抽3层6包 5元',body,'observed',1000)
        with app.test_client() as c:
            text=c.get('/?view=all&layout=cards').get_data(as_text=True)
        self.assertIn('折合每件（由整单换算）',text)
        self.assertIn('来源原文整单报价 · 2件',text)
        self.assertIn('¥5.00',text)
        self.assertIn('¥10.00',text)
        with app.test_client() as c:
            detail=c.get('/opportunities/1').get_data(as_text=True)
        self.assertIn('整单 ¥10.00',detail)
        self.assertIn('折合每件 ¥5.00',detail)
