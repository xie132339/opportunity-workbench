"""Core regressions D01/D02/D08; all prices are synthetic, never production evidence."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone, date
from pathlib import Path
from unittest.mock import patch

import db
import app as workbench
from services import alert_dispatch, opportunity_analysis


def fixture():
    now = (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
    opp = dict.fromkeys(workbench.COST_FIELDS, 0)
    opp.update(id=1,title='测试纸',category='零售优惠',status='verified',specification='TEST-SKU 1整单',
               offer_type='standard',eligibility='eligible',buy_checked_at=now,buy_cents=2000,
               source_method='rss',source_enabled=1,source_status='healthy',source_interval=30,
               source_last_success=now,last_seen_at=now,published_at=now,auto_state='observed')
    quotes = [dict(kind='sold',amount_cents=3000,same_spec=1,evidence_url=f'https://example.com/sold/{i}',
                   conditions='隔离合成样本',specification=opp['specification'],price_at=date.today().isoformat(),
                   offer_type='standard',final_quote=0,valid_until=None) for i in range(3)]
    rule = dict(id=1,name='测试门槛',enabled=1,category='零售优惠',include_words='纸',exclude_words='售罄',
                max_buy_cents=3000,min_profit_cents=1000)
    return opp,quotes,rule


class CoreRulesTests(unittest.TestCase):
    def setUp(self):
        self.opp,self.quotes,self.rule=fixture()

    def allowed(self, rules=None):
        return workbench.is_evidence_candidate(self.opp,self.quotes,[self.rule] if rules is None else rules)

    def test_not_historical_low_does_not_block_resale(self):
        self.assertTrue(self.allowed())  # No history samples at all; exit evidence stays required.
        self.assertFalse(workbench.historical_buy_assessment(self.opp,self.quotes)[2])

    def test_profit_boundary_and_one_cent(self):
        self.assertTrue(self.allowed())  # Exactly the configured threshold.
        self.rule['min_profit_cents']=1001
        self.assertFalse(self.allowed())
        self.rule['min_profit_cents']=500
        for q in self.quotes:
            q['amount_cents']=2001
        self.assertFalse(self.allowed())

    def test_missing_or_zero_policy_never_defaults_to_profit_positive(self):
        self.assertFalse(self.allowed([]))
        for field in ['max_buy_cents','min_profit_cents']:
            for value in [None,0,-1]:
                rule=dict(self.rule,**{field:value})
                self.assertFalse(self.allowed([rule]))

    def test_budget_includes_shipping(self):
        self.opp['buy_shipping_cents']=500
        self.rule.update(min_profit_cents=500,max_buy_cents=2499)
        self.assertFalse(self.allowed())
        self.rule['max_buy_cents']=2500
        self.assertTrue(self.allowed())

    def test_match_and_enabled_apply(self):
        for changes in [dict(enabled=0),dict(category='二手与闲置'),dict(include_words='手机'),dict(exclude_words='纸')]:
            self.assertFalse(self.allowed([dict(self.rule,**changes)]))

    def test_cannot_mix_policy_thresholds(self):
        self.assertFalse(self.allowed([dict(self.rule,min_profit_cents=2000),
                                       dict(self.rule,min_profit_cents=100,max_buy_cents=1000)]))

    def test_missing_checkout_exit_cost_and_conflict_block(self):
        for change in [dict(status='pending'),dict(eligibility='unknown'),dict(buy_cents=None),
                       dict(reserve_cents=None),dict(auto_state='conflict')]:
            opp=dict(self.opp,**change)
            self.assertFalse(workbench.is_evidence_candidate(opp,self.quotes,[self.rule]))
        self.assertFalse(workbench.is_evidence_candidate(self.opp,[],[self.rule]))

    def test_future_and_expired_timestamps_block(self):
        for field in ['buy_checked_at','source_last_success','last_seen_at','published_at']:
            for hours in [1,-3]:
                opp=dict(self.opp,**{field:(datetime.now(timezone.utc)+timedelta(hours=hours)).isoformat()})
                self.assertFalse(workbench.is_evidence_candidate(opp,self.quotes,[self.rule]),field)
        self.opp['buy_checked_at']=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()
        self.assertIsNone(workbench.estimated_profit(self.opp,self.quotes)[0])
        self.assertFalse(workbench.historical_buy_assessment(self.opp,self.quotes)[2])


class CoreFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.db_patch=patch.object(db,'DB_PATH',Path(self.temp.name)/'isolated.sqlite3')
        self.db_patch.start()
        opp,quotes,rule=fixture()
        with db.connect() as c:
            c.executescript(db.SCHEMA)
            c.execute("INSERT INTO sources(id,platform,name,category,url,method,status,last_success) VALUES(1,'测试','测试','零售优惠','https://example.com/feed','rss','healthy',CURRENT_TIMESTAMP)")
            c.execute("INSERT INTO events(id,source_id,external_key,title,url,fingerprint,published_at) VALUES(1,1,'one','测试纸','https://example.com/item','one',CURRENT_TIMESTAMP)")
            keys=['title','category','status','specification','offer_type','eligibility','buy_checked_at',*workbench.COST_FIELDS]
            c.execute('INSERT INTO opportunities(id,event_id,source_id,url,'+','.join(keys)+') VALUES(1,1,1,\'https://example.com/item\','+','.join('?' for _ in keys)+')',[opp[k] for k in keys])
            for q in quotes:
                c.execute('INSERT INTO quotes(opportunity_id,'+','.join(q)+') VALUES(1,'+','.join('?' for _ in q)+')',list(q.values()))
            c.execute('INSERT INTO strategies('+','.join(rule)+') VALUES('+','.join('?' for _ in rule)+')',list(rule.values()))
        self.client=workbench.app.test_client()
        with self.client.session_transaction() as session:
            session['csrf']='test-csrf'

    def tearDown(self):
        self.db_patch.stop()
        self.temp.cleanup()

    def test_existing_form_update_changes_candidate_list_and_detail(self):
        self.assertIn('测试纸',self.client.get('/?candidate=1').get_data(as_text=True))
        self.assertIn('转售测算达标',self.client.get('/opportunities/1').get_data(as_text=True))
        response=self.client.post('/strategies/1',data=dict(csrf='test-csrf',name='更新',max_buy='30',min_profit='10.01',enabled='1'))
        self.assertEqual(response.status_code,302)
        self.assertNotIn('测试纸',self.client.get('/?candidate=1').get_data(as_text=True))
        self.assertIn('未进入转售候选',self.client.get('/opportunities/1').get_data(as_text=True))
        with db.connect() as c:
            self.assertEqual(c.execute('SELECT min_profit_cents FROM strategies WHERE id=1').fetchone()[0],1001)

    def test_products_show_only_own_source_benefits_and_pool_is_separate(self):
        with db.connect() as c:
            for index in range(25):
                cursor=c.execute("""INSERT INTO benefit_resources(url,title,kind,state,reason,source_type)
                           VALUES(?,?,'coupon','unsupported','test','collected')""",
                          (f'https://offers.example/coupon/{index}', f'优惠入口{index:02d}'))
                if index==0:
                    c.execute("""INSERT INTO benefit_resource_sources
                        (resource_id,source_key,source_type,source_opportunity_id,source_title)
                        VALUES(?,'opportunity:1','collected',1,'测试纸')""",(cursor.lastrowid,))
        center=self.client.get('/?view=all').get_data(as_text=True)
        self.assertIn('本商品优惠',center);self.assertIn('原文关联 1 个入口',center)
        self.assertNotIn('优惠资源归属台账',center)
        detail=self.client.get('/opportunities/1').get_data(as_text=True)
        self.assertIn('本商品能用什么优惠',detail);self.assertIn('系统从公开来源识别到明确适用本商品的优惠：0 个',detail)
        self.assertIn('优惠入口00',detail);self.assertNotIn('优惠入口01',detail)
        pool=self.client.get('/benefits').get_data(as_text=True)
        self.assertIn('优惠资源归属台账',pool);self.assertIn('下一页',pool)
        self.assertNotIn('优惠入口04',pool)
        second=self.client.get('/benefits?page=2').get_data(as_text=True)
        self.assertIn('第 2 页',second)
        self.assertIn('优惠入口04',second)

    def test_search_price_sort_orders_visible_quotes_and_keeps_unpriced_last(self):
        with db.connect() as c:
            c.execute("INSERT INTO auto_reviews(opportunity_id,state,reason,advertised_cents) VALUES(1,'observed','test quote',1200)")
            c.execute("INSERT INTO events(id,source_id,external_key,title,url,fingerprint,published_at) VALUES(2,1,'cheap','更低的测试纸 5元','https://example.com/cheap','cheap',CURRENT_TIMESTAMP)")
            c.execute("INSERT INTO opportunities(id,event_id,source_id,title,category,status,url,offer_type) VALUES(2,2,1,'更低的测试纸 5元','零售优惠','pending','https://example.com/cheap','standard')")
            c.execute("INSERT INTO auto_reviews(opportunity_id,state,reason,advertised_cents) VALUES(2,'observed','test quote',500)")
            c.execute("INSERT INTO events(id,source_id,external_key,title,url,fingerprint,published_at) VALUES(3,1,'unknown','没写价格的测试纸','https://example.com/unknown','unknown',CURRENT_TIMESTAMP)")
            c.execute("INSERT INTO opportunities(id,event_id,source_id,title,category,status,url,offer_type) VALUES(3,3,1,'没写价格的测试纸','零售优惠','pending','https://example.com/unknown','standard')")
        page=self.client.get('/?view=all&sort=price_low').get_data(as_text=True)
        self.assertLess(page.index('/opportunities/2'),page.index('/opportunities/1'))
        self.assertGreater(page.index('/opportunities/3'),page.index('/opportunities/1'))
        with db.connect() as c:
            c.execute("INSERT INTO events(id,source_id,external_key,title,url,fingerprint,published_at) VALUES(4,1,'diaper','纸尿裤 1元','https://example.com/diaper','diaper',CURRENT_TIMESTAMP)")
            c.execute("INSERT INTO opportunities(id,event_id,source_id,title,category,status,url,offer_type) VALUES(4,4,1,'纸尿裤 1元','零售优惠','pending','https://example.com/diaper','standard')")
            c.execute("INSERT INTO auto_reviews(opportunity_id,state,reason,advertised_cents) VALUES(4,'observed','test quote',100)")
            c.execute("INSERT INTO events(id,source_id,external_key,title,url,fingerprint,published_at) VALUES(5,1,'tissue','抽纸6包 12元','https://example.com/tissue','tissue',CURRENT_TIMESTAMP)")
            c.execute("INSERT INTO opportunities(id,event_id,source_id,title,category,status,url,offer_type) VALUES(5,5,1,'抽纸6包 12元','零售优惠','pending','https://example.com/tissue','standard')")
            c.execute("INSERT INTO auto_reviews(opportunity_id,state,reason,advertised_cents) VALUES(5,'observed','test quote',1200)")
        paper_page=self.client.get('/?view=all&sort=paper_unit_low').get_data(as_text=True)
        self.assertLess(paper_page.index('/opportunities/5'),paper_page.index('/opportunities/4'))

    def test_single_character_search_ignores_unrelated_source_snippet(self):
        with db.connect() as c:
            c.execute("INSERT INTO events(id,source_id,external_key,title,url,snippet,fingerprint,published_at) VALUES(6,1,'bank','工商银行5元','https://example.com/bank','纸质账单优惠活动','bank',CURRENT_TIMESTAMP)")
            c.execute("INSERT INTO opportunities(id,event_id,source_id,title,category,status,url,offer_type) VALUES(6,6,1,'工商银行5元','零售优惠','pending','https://example.com/bank','standard')")
            c.execute("INSERT INTO events(id,source_id,external_key,title,url,snippet,fingerprint,published_at) VALUES(8,1,'snack','纸皮馅饼组合装','https://example.com/snack','纸皮馅饼活动价15.9元','snack',CURRENT_TIMESTAMP)")
            c.execute("INSERT INTO opportunities(id,event_id,source_id,title,category,status,url,offer_type) VALUES(8,8,1,'纸皮馅饼组合装','零售优惠','pending','https://example.com/snack','standard')")
            c.execute("INSERT INTO events(id,source_id,external_key,title,url,snippet,fingerprint,published_at) VALUES(9,1,'toy','响纸发声小狮子','https://example.com/toy','商品优惠价31元','toy',CURRENT_TIMESTAMP)")
            c.execute("INSERT INTO opportunities(id,event_id,source_id,title,category,status,url,offer_type) VALUES(9,9,1,'响纸发声小狮子','零售优惠','pending','https://example.com/toy','standard')")
            c.execute("INSERT INTO events(id,source_id,external_key,title,url,snippet,fingerprint,published_at) VALUES(10,1,'paper-bun','纸皮烧麦','https://example.com/paper-bun','纸皮烧麦优惠价19元','paper-bun',CURRENT_TIMESTAMP)")
            c.execute("INSERT INTO opportunities(id,event_id,source_id,title,category,status,url,offer_type) VALUES(10,10,1,'纸皮烧麦','零售优惠','pending','https://example.com/paper-bun','standard')")
            c.execute("INSERT INTO events(id,source_id,external_key,title,url,snippet,fingerprint,published_at) VALUES(7,1,'snippet-paper','限时活动','https://example.com/snippet-paper','抽纸30抽，9.9元','snippet-paper',CURRENT_TIMESTAMP)")
            c.execute("INSERT INTO opportunities(id,event_id,source_id,title,category,status,url,offer_type) VALUES(7,7,1,'限时活动','零售优惠','pending','https://example.com/snippet-paper','standard')")
        single=self.client.get('/?q=纸&view=all').get_data(as_text=True)
        self.assertNotIn('/opportunities/6',single)
        self.assertNotIn('/opportunities/8',single)
        self.assertNotIn('/opportunities/9',single)
        self.assertNotIn('/opportunities/10',single)
        self.assertIn('本次命中 1 / 1 个已启用采集入口',single)
        multi=self.client.get('/?q=抽纸&view=all').get_data(as_text=True)
        self.assertIn('/opportunities/7',multi)

    def test_source_feed_is_visible_and_archive_link_keeps_current_search(self):
        with db.connect() as c:
            c.execute("UPDATE sources SET name='纸品关键词采集' WHERE id=1")
        page=self.client.get('/?q=测试纸&layout=list&view=current').get_data(as_text=True)
        self.assertIn('测试 · 纸品关键词采集',page)
        import re
        from html import unescape
        from urllib.parse import parse_qs, urlparse
        match=re.search(r'<a href="([^"]+)">查看全部来源与历史',page)
        self.assertIsNotNone(match)
        params=parse_qs(urlparse(unescape(match.group(1))).query)
        self.assertEqual(params.get('q'),['测试纸'])
        self.assertEqual(params.get('view'),['all'])
        archive=self.client.get(match.group(1)).get_data(as_text=True)
        self.assertIn('测试 · 纸品关键词采集',archive)
        detail=self.client.get('/opportunities/1').get_data(as_text=True)
        source_at=detail.index('线索来源：</strong>测试 · 纸品关键词采集')
        self.assertLess(source_at,detail.index('本商品当前结论'))

    def test_price_header_is_a_clickable_sort_toggle(self):
        low=self.client.get('/?q=纸&sort=price_low&view=all').get_data(as_text=True)
        self.assertIn('sortable-header',low)
        self.assertIn('mobile-sort-toggle',low)
        self.assertIn('sort-indicator',low)
        self.assertIn('来源报价排序；点击切换升序和降序',low)
        self.assertIn('sort=price_high',low)
        high=self.client.get('/?q=纸&sort=price_high&view=all').get_data(as_text=True)
        self.assertIn('sort=price_low',high)
        self.assertIn('↓',high)

    def test_form_rejects_partial_zero_and_missing_csrf(self):
        for values in [dict(max_buy='30'),dict(min_profit='10'),dict(max_buy='30',min_profit='0'),dict(max_buy='0',min_profit='10')]:
            self.client.post('/strategies',data=dict(csrf='test-csrf',name='invalid',**values))
        self.assertEqual(self.client.post('/strategies',data={'name':'bad'}).status_code,400)
        with db.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM strategies').fetchone()[0],1)

    def test_excluded_notice_removed_from_current_but_kept_in_archive(self):
        with db.connect() as c:
            c.execute('INSERT INTO notifications(event_id,opportunity_id) VALUES(1,1)')
            c.execute("INSERT INTO auto_reviews(opportunity_id,state,reason) VALUES(1,'excluded','已售')")
        self.assertNotIn('测试纸',self.client.get('/strategies').get_data(as_text=True))
        self.assertIn('测试纸',self.client.get('/strategies?view=all').get_data(as_text=True))
        with db.connect() as c:
            self.assertFalse(workbench.is_current_notice(workbench.notice_rows(c)[0]))

    def test_sending_rechecks_policy_after_queueing(self):
        original=workbench.resale_assessment
        calls=[]
        def change_before_send(opp,quotes,rules=None):
            calls.append(1)
            if len(calls)==2:
                with db.connect() as c:
                    c.execute('UPDATE strategies SET enabled=0')
            return original(opp,quotes,rules)
        with patch.object(alert_dispatch,'active_channels',return_value=['qq']),patch.object(alert_dispatch,'deliver') as send,patch.object(opportunity_analysis,'resale_assessment',side_effect=change_before_send),patch.object(alert_dispatch,'resale_assessment',side_effect=change_before_send):
            result=workbench.dispatch_verified_alerts()
            self.assertEqual(result,dict(eligible=1,sent=0,failed=0))
            send.assert_not_called()
        with db.connect() as c:
            self.assertEqual(c.execute('SELECT status FROM notification_deliveries').fetchone()[0],'skipped')

    def test_dispatch_uses_new_gate_without_historical_samples(self):
        with patch.object(alert_dispatch,'active_channels',return_value=['qq']),patch.object(alert_dispatch,'deliver') as send:
            self.assertEqual(workbench.dispatch_verified_alerts()['sent'],1)
            self.assertEqual(workbench.dispatch_verified_alerts()['sent'],0)
            send.assert_called_once()
            self.assertIn('测试门槛',send.call_args.args[2])
        # Mock only; no network sender is invoked in any test.


if __name__=='__main__':
    unittest.main()
