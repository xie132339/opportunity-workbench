import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import db
import autoreview as ar
from app import app


class ReviewRulesTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026,10,4,8)
        self.row = dict(title='洁云 抽纸3层90抽24包 12.32元',url='https://example.com/a',
            status='pending',published_at=ar.stamp(self.now),last_seen_at=ar.stamp(self.now),
            last_success=ar.stamp(self.now),enabled=1,source_status='healthy',interval_minutes=30)

    def test_single_public_price_and_pack(self):
        r = ar.classify(self.row,self.now)
        self.assertEqual(r['advertised_cents'],1232)
        self.assertEqual(r['state'],'observed')
        self.assertIn('90抽',r['specification'])

    def test_multiple_prices_not_guessed(self):
        self.assertIsNone(ar.extract('原价20元，新人12元')[0])
        self.assertIsNone(ar.extract('12-20元')[0])
        self.assertIsNone(ar.extract('1万元')[0])
        self.assertIsNone(ar.extract('90抽24包')[0])
        self.assertIsNone(ar.extract('领取100元券')[0])
        self.assertEqual(ar.extract('荣耀600 元气版 券后2039.15元')[0],203915)
        self.assertNotIn('316',ar.extract('316L不锈钢筷子')[1])

    def test_unit_price_and_conflicting_total(self):
        self.assertTrue(ar.price_conflicts(329950,'下单1件，实付低至6599元'))
        self.assertFalse(ar.price_conflicts(483,'需买6件，实付29元'))
        self.assertFalse(ar.price_conflicts(465,'需买4件，实付18.6元'))

    def test_closed_wanted_and_duplicate(self):
        for title in ['【已出】手机','[求购] 手机','抽纸已售罄']:
            self.assertEqual(ar.classify(dict(self.row,title=title),self.now)['state'],'excluded')
        self.assertEqual(ar.classify(self.row,self.now,True)['state'],'excluded')

    def test_old_future_unknown_never_current(self):
        for delta in [-121,1]:
            row=dict(self.row,published_at=ar.stamp(self.now+timedelta(minutes=delta)))
            self.assertEqual(ar.classify(row,self.now)['state'],'stale')
        self.assertEqual(ar.classify(dict(self.row,published_at=None),self.now)['state'],'missing_time')

    def test_failed_source_degrades(self):
        self.assertEqual(ar.classify(dict(self.row,source_status='failed'),self.now)['state'],'source_unavailable')

    def test_adapter_allowlist(self):
        self.assertTrue(ar.supported('https://www.smzdm.com/p/123/'))
        for url in ['https://uland.taobao.com/coupon/edetail','https://www.smzdm.com.evil/p/123/',
                    'http://127.0.0.1/p/123/','https://www.smzdm.com/p/123/?redirect=evil']:
            self.assertFalse(ar.supported(url))

    def test_detail_no_comments_or_update_time(self):
        r = ar.parse_detail('''<h1 class="J_title">88VIP 抽纸24包90抽</h1>
          <div class="info"><div class="price"><span class="price-large"><span class="num">12.32</span></span>
          <span class="price-desc">需用券</span><del>54.9元</del></div></div>
          <div class="baoliao-block"><p itemprop="description">省钱卡，新客券</p></div>
          <div class="comment">我买到1元</div><div class="recommend">0.1元</div>
          <script type="application/ld+json">{"pubDate":"2026-10-03T11:00:57","upDate":"2026-10-04T15:00:00"}</script>''')
        self.assertEqual(r['advertised_cents'],1232)
        self.assertEqual(r['published_at'],'2026-10-03 03:00:57')
        self.assertIn('新客',r['conditions'])
        self.assertNotIn('我买到',r['evidence'])

    def test_challenge_is_failure(self):
        with self.assertRaises(ValueError):
            ar.parse_detail('<h1>安全验证</h1><p>10元</p>')

    def test_real_1982_selected_one_pack_conflicts_with_ten(self):
        # Actual stored public excerpt; regression evidence, not a current buyable quote.
        row = dict(self.row,
            title='限移动端：清风 棉柔亲肤 悬挂式抽纸 4层*250抽*10提(170*132mm) 5.87元（淘金币可抵0.07元起）',
            snippet='该价格商品规格：套餐类型：【体验装】1提-棉柔亲肤 【不送挂钩】 天猫商城该商品正在促销，参加立减1.03元活动，最终到手价5.87元/件，喜欢可入。使用淘金币再省0.07元起，根据账号情况可能抵更多，建议亲测。 前往购买')
        result=ar.classify(row,self.now)
        self.assertEqual(result['state'],'conflict')
        self.assertIn('10提',result['reason'])
        self.assertIn('1提',result['reason'])
        self.assertIn('该价格商品规格',result['evidence'])
        self.assertIn('根据账号情况',result['conditions'])

    def test_real_1996_order_quantity_is_not_pack_quantity(self):
        row=dict(self.row,title='清风 悬挂式抽纸 4层1000张 1大提 2.6元',
                 snippet='购买方案 1 店铺 指数交易-同款价更低 ,商品面价 3.6元 2 领券 满1.01减1 3 加购 购买 1 件 4 实付 2.6元 ( 实付单件2.6元 )')
        result=ar.classify(row,self.now)
        self.assertNotEqual(result['state'],'conflict')
        self.assertEqual(result['advertised_cents'],260)
        self.assertIn('购买 1 件',result['conditions'])

    def test_marketing_other_packs_do_not_determine_selected_spec(self):
        title='抽纸24包12元'
        self.assertIsNone(ar.selected_spec_conflict(title,'另有1包体验装，赠品2包，满24元可用券'))
        self.assertIsNone(ar.selected_spec_conflict(title,'该价格商品规格：24包 天猫商城任选1件'))
        self.assertIsNone(ar.selected_spec_conflict('抽纸12元','该价格商品规格：24包'))
        self.assertIsNone(ar.selected_spec_conflict('抽纸10包/20包','该价格商品规格：1包'))

    def test_all_feeds_keep_body_restrictions_without_using_body_coupon_as_price(self):
        row=dict(self.row,title='抽纸24包12.32元',snippet='限新客，会员可领2元券，需买2件，返现需到账。')
        result=ar.classify(row,self.now)
        self.assertIsNone(result['advertised_cents'])
        self.assertEqual(result['state'],'activity')
        self.assertIn('需买2件',result['conditions'])
        self.assertIn('返现需到账',result['evidence'])

    def test_public_plan_price_is_not_coupon_or_personal_checkout(self):
        row=dict(self.row,url='https://guangdiu.com/detail.php?id=2010',
                 title='OPPO Reno15 手机 2549.15元（店铺会员优惠10元）',
                 snippet='购买方案 1 店铺 OPPO京东自营旗舰店 ,商品面价 3399元 2 领券 满3000减400 3 加购 购买 1 件 4 实付 2549.15元 ( 实付单件2549.15元 ) 商品介绍 另一个12元')
        result=ar.classify(row,self.now)
        self.assertEqual(result['advertised_cents'],254915)
        self.assertEqual(result['state'],'conditional')
        offer=ar.public_offer(row['url'],row['snippet'])
        self.assertEqual((offer['unit_cents'],offer['total_cents'],offer['quantity']),(254915,254915,1))
        self.assertEqual(offer['store'],'OPPO京东自营旗舰店')

    def test_public_plan_keeps_quantity_total_and_selected_spec(self):
        body='该价格商品规格：套餐类型：12包 天猫商城买2件，实付97.8元，最终到手价48.9元/件，淘金币可抵4.89元起'
        offer=ar.public_offer('https://guangdiu.com/detail.php?id=1971',body)
        self.assertEqual((offer['unit_cents'],offer['total_cents'],offer['quantity']),(4890,9780,2))
        self.assertEqual(offer['selected_spec'],'套餐类型：12包')
        self.assertFalse(offer['error'])
        rounded=ar.public_offer('https://guangdiu.com/detail.php?id=1989','购买3件 实付38.18元 ( 实付单件12.73元 )')
        self.assertFalse(rounded['error'])

    def test_xianbao_ocr_payment_typo_extracts_explicit_order_total(self):
        url='https://new.ixbk.net/haodan/7144218.html'
        title='维达线条小狗悬挂抽纸M码210抽大提 3.5元，维达线条小狗悬挂抽纸6提装 21元'
        body='🎀维达线条小狗悬挂抽纸\n💰3.5🉐M碼210抽大提‼️\n按图进秒刹+琻壁+天绛\n需.拍6提装，实.咐21元\n下单:'
        offer=ar.public_offer(url,body,title)
        self.assertEqual((offer['total_cents'],offer['quantity']),(2100,1))
        self.assertFalse(offer['error'])
        row=dict(self.row,url=url,title=title,snippet=body)
        self.assertEqual(ar.classify(row,self.now)['advertised_cents'],2100)
        brief=ar.offer_summary(title,url,body)
        self.assertEqual(brief['total_cents'],2100)
        self.assertEqual(brief['price_status']['label'],'')

    def test_instant_retail_buyer_platform_names_are_not_lost(self):
        brief=ar.offer_summary('同款纸品来自抖音商城、美团闪购、淘宝闪购和京东秒送','https://example.com/post','')
        self.assertEqual(brief['platforms'],'抖音商城 / 美团闪购 / 淘宝闪购 / 京东秒送')

    def test_public_plan_rejects_conflicting_totals_and_does_not_guess(self):
        url='https://guangdiu.com/detail.php?id=1'
        self.assertTrue(ar.public_offer(url,'购买2件 实付5元 实付单件4元')['error'])
        self.assertTrue(ar.public_offer(url,'实付单件4元 实付单件5元')['error'])
        offer=ar.public_offer(url,'最终到手价4元/件 淘金币可抵0.1元')
        self.assertIsNone(offer['quantity'])
        self.assertIsNone(offer['total_cents'])
        self.assertIsNone(ar.public_offer(url,'最终到手价4元起')['unit_cents'])
        self.assertEqual(ar.public_offer('https://example.com/detail.php','实付单件4元'),{})

    def test_public_plan_does_not_compare_different_title_eligibility(self):
        row=dict(self.row,url='https://guangdiu.com/detail.php?id=29774778',
                 title='精华液16.19元+6.57元淘金币（需新客）回头客到手15.19元',
                 snippet='购买1件 实付16.19元 ( 实付单件16.19元 )')
        self.assertEqual(ar.classify(row,self.now)['state'],'conditional')
        self.assertEqual(ar.classify(dict(row,title='精华液15.19元'),self.now)['state'],'conflict')

    def test_title_offer_structures_order_and_spec_without_guessing_variant(self):
        url='https://new.ixbk.net/haodan/1.html'
        offer=ar.public_offer(url,'','氏蜂社土蜂蜜1500g*1盒礼盒装 59元')
        self.assertEqual((offer['total_cents'],offer['quantity']),(5900,1))
        self.assertEqual(offer['selected_spec'],'1500g × 1盒')
        multi=ar.public_offer(url,'','纸尿裤NB/S/M/L 多规格 20.49元')
        self.assertEqual(multi['selected_spec'],'')
        two=ar.public_offer(url,'','拖鞋拍2件 10.8元')
        self.assertEqual((two['total_cents'],two['quantity']),(1080,2))

    def test_readable_summary_keeps_discount_basis_without_inventing_stacking(self):
        title='小米 REDMI 红米 Note 17 手机 6GB+128GB 浅水青 券后1104.15元'
        body='京东该商品参加1件8.5折、满1000减40元优惠券的促销活动，当前到手价1104.15元，降价前售价为1299.00元，本次降幅15%。可用券及活动：满1000减40元优惠券、特价1104.15、满件折1-0.85'
        brief=ar.offer_summary(title,'https://guangdiu.com/detail.php?id=1',body,'券后\n用券\n'+body)
        self.assertEqual(brief['promotions'],['1件8.5折','满1000减40元'])
        self.assertEqual(brief['previous_cents'],129900)
        self.assertEqual(brief['platforms'],'京东')
        self.assertNotIn('1104.15',brief['title'])
        self.assertNotIn('满件折1-0.85',str(brief))
        self.assertEqual(ar.offer_summary('荣耀600 元气版','', '')['title'],'荣耀600 元气版')

    def test_readable_summary_keeps_restrictions_and_does_not_invent_missing_price(self):
        brief=ar.offer_summary('限移动端：抽纸 48.9元（淘金币可抵4.89元起）',
            'https://guangdiu.com/detail.php?id=1','该价格商品规格：12包 天猫买2件 实付97.8元 最终到手价48.9元/件 限新客')
        self.assertEqual(brief['title'],'抽纸')
        self.assertIn('新客',brief['qualifications'])
        self.assertIn('移动端',brief['qualifications'])
        self.assertIn('淘金币',brief['qualifications'])
        self.assertEqual(brief['quantity'],2)
        self.assertIsNone(ar.offer_summary('纸','', '')['previous_cents'])


class ReviewPipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch=patch.object(db,'DB_PATH',Path(self.tmp.name)/'test.sqlite3')
        self.db_patch.start()
        with db.connect() as c:
            c.executescript(db.SCHEMA)
            c.execute("INSERT INTO sources(id,platform,name,category,url,method,status,last_success) VALUES(1,'测试','测试','零售优惠','https://www.smzdm.com/','html','healthy',CURRENT_TIMESTAMP)")
            c.execute("INSERT INTO events(id,source_id,external_key,title,url,fingerprint,published_at) VALUES(1,1,'x','纸12元','https://www.smzdm.com/p/123/','x',CURRENT_TIMESTAMP)")
            c.execute("INSERT INTO opportunities(id,event_id,source_id,title,category,url) VALUES(1,1,1,'纸12元','零售优惠','https://www.smzdm.com/p/123/')")

    def tearDown(self):
        self.db_patch.stop()
        self.tmp.cleanup()

    def test_pipeline_idempotent_no_fake_checkout(self):
        html='<h1 class="J_title">抽纸24包</h1><div class="info"><div class="price"><span class="price-large"><span class="num">12.32</span></span></div></div>'
        with patch('scanner._fetch',return_value=html) as fetch:
            self.assertEqual(ar.run_cycle()['probed'],1)
            self.assertEqual(ar.run_cycle()['probed'],0)
            fetch.assert_called_once()
        with db.connect() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM auto_reviews').fetchone()[0],1)
            r=c.execute('SELECT * FROM opportunities').fetchone()
            self.assertEqual(r['status'],'pending')
            self.assertIsNone(r['buy_cents'])
            for table in ['quotes','buy_checks','notification_deliveries']:
                self.assertEqual(c.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0],0)
        with app.test_client() as client:
            for url in ['/','/?view=all','/verification','/opportunities/1']:
                self.assertEqual(client.get(url).status_code,200,url)

    def test_public_price_filter_without_checkout_and_excludes_conflict(self):
        with db.connect() as c:
            c.execute("INSERT INTO auto_reviews(opportunity_id,state,reason,advertised_cents) VALUES(1,'observed','公开报价',1232)")
        with app.test_client() as client:
            self.assertIn('/opportunities/1',client.get('/?view=all&budget=13').get_data(as_text=True))
            self.assertNotIn('/opportunities/1',client.get('/?view=all&budget=12').get_data(as_text=True))
            with db.connect() as c:
                self.assertIsNone(c.execute('SELECT buy_cents FROM opportunities WHERE id=1').fetchone()[0])
                c.execute("UPDATE auto_reviews SET state='conflict'")
            self.assertNotIn('/opportunities/1',client.get('/?view=all&budget=13').get_data(as_text=True))

    def test_failure_backoff_no_immediate_retry(self):
        with patch('scanner._fetch',side_effect=PermissionError('403')) as fetch:
            ar.run_cycle()
            ar.run_cycle()
            self.assertEqual(fetch.call_count,1)
        with db.connect() as c:
            r=c.execute('SELECT * FROM auto_reviews').fetchone()
            self.assertEqual(r['state'],'retry')
            self.assertEqual(r['attempts'],1)
            self.assertIsNotNone(r['next_check_at'])

    def test_active_lease_skips_network(self):
        with db.connect() as c:
            c.execute('INSERT INTO review_runs(id,started_at) VALUES(1,CURRENT_TIMESTAMP)')
        with patch('scanner._fetch') as fetch:
            self.assertTrue(ar.run_cycle()['busy'])
            fetch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
