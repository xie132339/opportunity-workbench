import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import db
import autoreview as ar
from app import app

from scenario_groups import grouped_scenarios

@grouped_scenarios({
    'test_price_extraction_and_order_conflict_boundaries': (
        'single_public_price_and_pack',
        'price_extraction_and_total_conflict_rules_reject_ambiguous_amounts',
    ),
    'test_current_and_catalog_freshness_boundaries': (
        'source_and_time_gates_prevent_noncurrent_rows_from_becoming_current',
        'fresh_merchant_catalog_price_uses_observation_time_not_post_time',
        'catalog_starting_and_estimated_prices_are_not_exact_quotes',
        'stale_catalog_observation_is_not_current_even_without_post_time',
    ),
    'test_detail_adapter_allowlist_and_parser_safety': (
        'adapter_allowlist',
        'detail_no_comments_or_update_time',
        'challenge_is_failure',
        'guangdiu_detail_is_main_article_scoped',
    ),
    'test_selected_specification_evidence_boundaries': (
        'selected_spec_normalization_and_conflict_regressions',
        'title_spec_is_only_a_hint_until_source_selects_the_priced_variant',
    ),
    'test_public_price_claims_keep_coupon_checkout_and_eligibility_distinct': (
        'all_feeds_keep_body_restrictions_without_using_body_coupon_as_price',
        'public_plan_price_is_not_coupon_or_personal_checkout',
        'post_cashback_net_cost_is_not_an_order_total_or_price_quote',
        'public_plan_does_not_compare_different_title_eligibility',
    ),
    'test_purchase_plan_totals_quantity_and_ambiguity': (
        'public_plan_keeps_quantity_total_and_selected_spec',
        'xianbao_ocr_payment_typo_does_not_invent_order_quantity',
        'public_plan_rejects_conflicting_totals_and_does_not_guess',
        'title_price_and_pack_size_do_not_become_order_total_or_purchase_quantity',
        'explicit_order_body_keeps_total_and_quantity',
    ),
    'test_readable_summary_preserves_price_basis_and_restrictions': (
        'readable_summary_keeps_discount_basis_without_inventing_stacking',
        'readable_summary_keeps_restrictions_and_does_not_invent_missing_price',
        'title_amount_is_visible_as_a_clue_but_not_a_quote',
    ),
})
class ReviewRulesTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026,10,4,8)
        self.row = dict(title='洁云 抽纸3层90抽24包 12.32元',url='https://example.com/a',
            status='pending',published_at=ar.stamp(self.now),last_seen_at=ar.stamp(self.now),
            last_success=ar.stamp(self.now),enabled=1,source_status='healthy',interval_minutes=30)

    def _case_single_public_price_and_pack(self):
        r = ar.classify(self.row,self.now)
        self.assertEqual(r['advertised_cents'],1232)
        self.assertEqual(r['state'],'missing_price')
        self.assertIn('明确整单金额与购买件数',r['reason'])
        self.assertIn('90抽',r['specification'])

    def _case_price_extraction_and_total_conflict_rules_reject_ambiguous_amounts(self):
        self.assertIsNone(ar.extract('原价20元，新人12元')[0])
        self.assertIsNone(ar.extract('12-20元')[0])
        self.assertIsNone(ar.extract('1万元')[0])
        self.assertIsNone(ar.extract('90抽24包')[0])
        self.assertIsNone(ar.extract('领取100元券')[0])
        self.assertEqual(ar.extract('荣耀600 元气版 券后2039.15元')[0],203915)
        self.assertNotIn('316',ar.extract('316L不锈钢筷子')[1])
        self.assertTrue(ar.price_conflicts(329950,'下单1件，实付低至6599元'))
        self.assertFalse(ar.price_conflicts(483,'需买6件，实付29元'))
        self.assertFalse(ar.price_conflicts(465,'需买4件，实付18.6元'))
        self.assertFalse(ar.price_conflicts(483,'实付29元'))
        self.assertFalse(ar.price_conflicts(483,'买6件或买12件，实付29元'))

    def _case_source_and_time_gates_prevent_noncurrent_rows_from_becoming_current(self):
        for title in ['【已出】手机','[求购] 手机','抽纸已售罄']:
            with self.subTest(excluded_title=title):
                self.assertEqual(ar.classify(dict(self.row,title=title),self.now)['state'],'excluded')
        self.assertEqual(ar.classify(self.row,self.now,True)['state'],'excluded')
        for delta in [-121,1]:
            row=dict(self.row,published_at=ar.stamp(self.now+timedelta(minutes=delta)))
            with self.subTest(published_delta_minutes=delta):
                self.assertEqual(ar.classify(row,self.now)['state'],'stale')
        self.assertEqual(ar.classify(dict(self.row,published_at=None),self.now)['state'],'missing_time')
        self.assertEqual(ar.classify(dict(self.row,source_status='failed'),self.now)['state'],
                         'source_unavailable')

    def _case_fresh_merchant_catalog_price_uses_observation_time_not_post_time(self):
        row=dict(self.row,title='米家冰箱 对开636L 1899元',
                 url='https://www.mi.com/shop/buy?product_id=22504',
                 source_parser='mi',published_at=None,
                 last_seen_at=ar.stamp(self.now),last_success=ar.stamp(self.now),
                 enabled=1,source_status='healthy',interval_minutes=60)
        result=ar.classify(row,self.now)
        self.assertEqual((result['state'],result['advertised_cents']),('observed',189900))
        self.assertIn('目录单一公开标价',result['reason'])
        self.assertIn('不代表详情页',result['reason'])

    def _case_catalog_starting_and_estimated_prices_are_not_exact_quotes(self):
        common=dict(self.row,source_parser='mi',published_at=None,
                    last_seen_at=ar.stamp(self.now),last_success=ar.stamp(self.now),
                    enabled=1,source_status='healthy',interval_minutes=60)
        starting=ar.classify(dict(common,title='笔记本 3799元起'),self.now)
        self.assertEqual(starting['state'],'missing_price')
        self.assertIsNone(starting['advertised_cents'])
        estimate=ar.classify(dict(common,title='手环 预估到手价 ¥229'),self.now)
        self.assertEqual(estimate['state'],'conditional')
        self.assertIsNone(estimate['advertised_cents'])
        estimate_with_detail=ar.classify(dict(common,title='手环 预估到手价 ¥229',
            detail_json=json.dumps({'title':'荣耀手环','advertised_cents':22900,'conditions':'当前展示'})),self.now)
        self.assertEqual(estimate_with_detail['state'],'conditional')
        self.assertIsNone(estimate_with_detail['advertised_cents'])

    def _case_stale_catalog_observation_is_not_current_even_without_post_time(self):
        row=dict(self.row,title='米家冰箱 对开636L 1899元',
                 url='https://www.mi.com/shop/buy?product_id=22504',
                 source_parser='mi',published_at=None,
                 last_seen_at=ar.stamp(self.now-timedelta(hours=3)),
                 last_success=ar.stamp(self.now-timedelta(hours=3)),
                 enabled=1,source_status='healthy',interval_minutes=60)
        result=ar.classify(row,self.now)
        self.assertEqual(result['state'],'source_unavailable')

    def _case_adapter_allowlist(self):
        self.assertTrue(ar.supported('https://www.smzdm.com/p/123/'))
        self.assertTrue(ar.supported('https://guangdiu.com/detail.php?id=29832200'))
        self.assertTrue(ar.supported('https://product.suning.com/123/456.html'))
        for url in ['https://uland.taobao.com/coupon/edetail','https://www.smzdm.com.evil/p/123/',
                    'http://127.0.0.1/p/123/','https://www.smzdm.com/p/123/?redirect=evil',
                    'https://guangdiu.com.evil/detail.php?id=29832200',
                    'https://product.suning.com.evil/123/456.html']:
            self.assertFalse(ar.supported(url))

    def _case_guangdiu_detail_is_main_article_scoped(self):
        from services.guangdiu_detail import parse_detail, supports_url
        url = 'https://guangdiu.com/detail.php?id=29832200'
        html = '''<div id="mainleft"><h1>山丘 抽纸加厚4层76抽*8包 7.3元</h1>
          <div id="dabstract">两个商品一起付款，折主商品7.3元，凑单0.01元，包邮。<br>满14元减5元。</div>
          </div><aside class="recommend">关联商品 99.9元</aside>'''
        detail = parse_detail(html)
        self.assertTrue(supports_url(url))
        self.assertFalse(supports_url('https://guangdiu.com/detail.php?id=oops'))
        self.assertIn('7.3元', detail['title'])
        self.assertEqual(detail['headline_amount_cents'],730)
        self.assertEqual(detail['headline_amount_evidence'],'逛丢详情主文章标题')
        self.assertIsNone(detail['advertised_cents'])
        self.assertIn('满14元减5元', detail['conditions'])
        self.assertNotIn('99.9元', detail['evidence'])
        self.assertTrue(ar.detail_probe_needed(url, detail['title'], detail['conditions']))

    def _case_detail_no_comments_or_update_time(self):
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

    def _case_challenge_is_failure(self):
        with self.assertRaises(ValueError):
            ar.parse_detail('<h1>安全验证</h1><p>10元</p>')

    def _case_selected_spec_normalization_and_conflict_regressions(self):
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
        with self.subTest(scenario='order_quantity_is_not_pack_quantity'):
            row=dict(self.row,title='清风 悬挂式抽纸 4层1000张 1大提 2.6元',
                     snippet='购买方案 1 店铺 指数交易-同款价更低 ,商品面价 3.6元 2 领券 满1.01减1 3 加购 购买 1 件 4 实付 2.6元 ( 实付单件2.6元 )')
            result=ar.classify(row,self.now)
            self.assertNotEqual(result['state'],'conflict')
            self.assertEqual(result['advertised_cents'],260)
            self.assertIn('购买 1 件',result['conditions'])
        with self.subTest(scenario='marketing_mentions_do_not_override_selected_spec'):
            title='抽纸24包12元'
            self.assertIsNone(ar.selected_spec_conflict(title,'另有1包体验装，赠品2包，满24元可用券'))
            self.assertIsNone(ar.selected_spec_conflict(title,'该价格商品规格：24包 天猫商城任选1件'))
            self.assertIsNone(ar.selected_spec_conflict('抽纸12元','该价格商品规格：24包'))
            self.assertIn('数量口径冲突',ar.selected_spec_conflict('抽纸10包/20包','该价格商品规格：1包京东商城'))
            self.assertIsNone(ar.selected_spec_conflict('抽纸10包/20包','该价格商品规格：10包京东商城'))
        with self.subTest(scenario='slash_separated_options_are_not_a_combined_spec'):
            self.assertEqual(ar.structured_spec('抽纸10包/20包'),'')
            self.assertEqual(ar.structured_spec('卫生纸5层/6层'),'')
            self.assertEqual(ar.structured_spec('抽纸400抽/包*18包'),'400抽 × 18包')
        with self.subTest(scenario='small_pack_counts_are_preserved_and_duplicate_layers_removed'):
            title='得宝迷你系列手帕纸5片*54小包 20元'
            self.assertEqual(ar.structured_spec(title),'5片 × 54小包')
            self.assertEqual(ar.extract('洁柔卷纸4层 4层 135g/卷*10卷')[1],
                             '4层 · 135g · 10卷')
            result=ar.classify(dict(self.row,title=title),self.now)
            self.assertEqual(result['specification'],'5片 · 54小包')
        with self.subTest(scenario='selected_variant_must_be_one_of_title_options'):
            row=dict(self.row,title='抽纸10包/20包 10元',
                     snippet='该价格商品规格：1包 京东商城')
            result=ar.classify(row,self.now)
            self.assertEqual(result['state'],'conflict')
            self.assertIn('不在标题选项中',result['reason'])

    def _case_all_feeds_keep_body_restrictions_without_using_body_coupon_as_price(self):
        row=dict(self.row,title='抽纸24包12.32元',snippet='限新客，会员可领2元券，需买2件，返现需到账。')
        result=ar.classify(row,self.now)
        self.assertIsNone(result['advertised_cents'])
        self.assertEqual(result['state'],'activity')
        self.assertIn('需买2件',result['conditions'])
        self.assertIn('返现需到账',result['evidence'])

    def _case_public_plan_price_is_not_coupon_or_personal_checkout(self):
        row=dict(self.row,url='https://guangdiu.com/detail.php?id=2010',
                 title='OPPO Reno15 手机 2549.15元（店铺会员优惠10元）',
                 snippet='购买方案 1 店铺 OPPO京东自营旗舰店 ,商品面价 3399元 2 领券 满3000减400 3 加购 购买 1 件 4 实付 2549.15元 ( 实付单件2549.15元 ) 商品介绍 另一个12元')
        result=ar.classify(row,self.now)
        self.assertEqual(result['advertised_cents'],254915)
        self.assertEqual(result['state'],'conditional')
        offer=ar.public_offer(row['url'],row['snippet'])
        self.assertEqual((offer['unit_cents'],offer['total_cents'],offer['quantity']),(254915,254915,1))
        self.assertEqual(offer['store'],'OPPO京东自营旗舰店')

    def _case_public_plan_keeps_quantity_total_and_selected_spec(self):
        body='该价格商品规格：套餐类型：12包 天猫商城买2件，实付97.8元，最终到手价48.9元/件，淘金币可抵4.89元起'
        offer=ar.public_offer('https://guangdiu.com/detail.php?id=1971',body)
        self.assertEqual((offer['unit_cents'],offer['total_cents'],offer['quantity']),(4890,9780,2))
        self.assertEqual(offer['selected_spec'],'套餐类型：12包')
        self.assertFalse(offer['error'])
        rounded=ar.public_offer('https://guangdiu.com/detail.php?id=1989','购买3件 实付38.18元 ( 实付单件12.73元 )')
        self.assertFalse(rounded['error'])

    def _case_xianbao_ocr_payment_typo_does_not_invent_order_quantity(self):
        url='https://new.ixbk.net/haodan/7144218.html'
        title='维达线条小狗悬挂抽纸M码210抽大提 3.5元，维达线条小狗悬挂抽纸6提装 21元'
        body='🎀维达线条小狗悬挂抽纸\n💰3.5🉐M碼210抽大提‼️\n按图进秒刹+琻壁+天绛\n需.拍6提装，实.咐21元\n下单:'
        offer=ar.public_offer(url,body,title)
        self.assertEqual((offer['total_cents'],offer['quantity']),(2100,None))
        self.assertFalse(offer['error'])
        row=dict(self.row,url=url,title=title,snippet=body)
        self.assertEqual(ar.classify(row,self.now)['advertised_cents'],2100)
        brief=ar.offer_summary(title,url,body)
        self.assertEqual(brief['total_cents'],2100)
        self.assertEqual(brief['price_status']['label'],'')

    def test_instant_retail_buyer_platform_names_are_not_lost(self):
        brief=ar.offer_summary('同款纸品来自抖音商城、美团闪购、淘宝闪购和京东秒送','https://example.com/post','')
        self.assertEqual(brief['platforms'],'抖音商城 / 美团闪购 / 淘宝闪购 / 京东秒送')

    def _case_post_cashback_net_cost_is_not_an_order_total_or_price_quote(self):
        url='https://www.smzdm.com/p/183470204/'
        body=('活动售价24.99元，下单领取满40减10元优惠券，下单2件。'
              '返现后实付低至29.98元。')
        offer=ar.public_offer(url,body)
        self.assertIsNone(offer['total_cents'])
        self.assertEqual((offer['net_after_rebate_cents'],offer['quantity']),(2998,2))
        brief=ar.offer_summary('爱媛38号果冻橙 4.5斤装 14.99元（返现后）',url,body)
        from services.product_search import listing_quote,quote_note
        self.assertIsNone(listing_quote({'auto_state':'conditional'},brief))
        self.assertIn('返现后的净成本',quote_note(brief))
        self.assertIn('到账条件未核实',quote_note(brief))

    def _case_public_plan_rejects_conflicting_totals_and_does_not_guess(self):
        url='https://guangdiu.com/detail.php?id=1'
        self.assertTrue(ar.public_offer(url,'购买2件 实付5元 实付单件4元')['error'])
        self.assertTrue(ar.public_offer(url,'实付单件4元 实付单件5元')['error'])
        offer=ar.public_offer(url,'最终到手价4元/件 淘金币可抵0.1元')
        self.assertIsNone(offer['quantity'])
        self.assertIsNone(offer['total_cents'])
        self.assertIsNone(ar.public_offer(url,'最终到手价4元起')['unit_cents'])
        self.assertEqual(ar.public_offer('https://example.com/detail.php','实付单件4元'),{})

    def _case_public_plan_does_not_compare_different_title_eligibility(self):
        row=dict(self.row,url='https://guangdiu.com/detail.php?id=29774778',
                 title='精华液16.19元+6.57元淘金币（需新客）回头客到手15.19元',
                 snippet='购买1件 实付16.19元 ( 实付单件16.19元 )')
        self.assertEqual(ar.classify(row,self.now)['state'],'conditional')
        self.assertEqual(ar.classify(dict(row,title='精华液15.19元'),self.now)['state'],'conflict')

    def _case_title_price_and_pack_size_do_not_become_order_total_or_purchase_quantity(self):
        url='https://new.ixbk.net/haodan/1.html'
        offer=ar.public_offer(url,'','氏蜂社土蜂蜜1500g*1盒礼盒装 59元')
        self.assertEqual((offer['total_cents'],offer['quantity']),(None,None))
        self.assertEqual(offer['selected_spec'],'')
        brief=ar.offer_summary('氏蜂社土蜂蜜1500g*1盒礼盒装 59元',url,'')
        self.assertEqual(brief['source_spec'],'1500g × 1盒')
        self.assertEqual(brief['price_status']['label'],'标题金额已提取，购买口径待补全')
        multi=ar.public_offer(url,'','纸尿裤NB/S/M/L 多规格 20.49元')
        self.assertEqual(multi['selected_spec'],'')
        two=ar.public_offer(url,'','拖鞋拍2件 10.8元')
        self.assertEqual((two['total_cents'],two['quantity']),(None,2))

    def _case_explicit_order_body_keeps_total_and_quantity(self):
        offer=ar.public_offer('https://new.ixbk.net/haodan/1.html','购买1件，实付59元',
                              '氏蜂社土蜂蜜1500g*1盒礼盒装 59元')
        self.assertEqual((offer['total_cents'],offer['quantity']),(5900,1))

    def _case_readable_summary_keeps_discount_basis_without_inventing_stacking(self):
        title='小米 REDMI 红米 Note 17 手机 6GB+128GB 浅水青 券后1104.15元'
        body='京东该商品参加1件8.5折、满1000减40元优惠券的促销活动，当前到手价1104.15元，降价前售价为1299.00元，本次降幅15%。可用券及活动：满1000减40元优惠券、特价1104.15、满件折1-0.85'
        brief=ar.offer_summary(title,'https://guangdiu.com/detail.php?id=1',body,'券后\n用券\n'+body)
        self.assertEqual(brief['promotions'],['1件8.5折','满1000减40元'])
        self.assertEqual(brief['previous_cents'],129900)
        self.assertEqual(brief['platforms'],'京东')
        self.assertNotIn('1104.15',brief['title'])
        self.assertNotIn('满件折1-0.85',str(brief))
        self.assertEqual(ar.offer_summary('荣耀600 元气版','', '')['title'],'荣耀600 元气版')

    def _case_title_spec_is_only_a_hint_until_source_selects_the_priced_variant(self):
        title='某品牌抽纸100抽3层6包 5元'
        implied=ar.offer_summary(title,'https://new.ixbk.net/haodan/1.html','购买1件 实付5元')
        self.assertEqual(implied['selected_spec'],'')
        self.assertEqual(implied['source_spec'],'100抽 × 3层 × 6包')
        explicit=ar.offer_summary(title,'https://new.ixbk.net/haodan/1.html',
                                  '该价格商品规格：100抽3层6包 京东商城 购买1件 实付5元')
        self.assertEqual(explicit['selected_spec'],'100抽3层6包')

    def _case_readable_summary_keeps_restrictions_and_does_not_invent_missing_price(self):
        brief=ar.offer_summary('限移动端：抽纸 48.9元（淘金币可抵4.89元起）',
            'https://guangdiu.com/detail.php?id=1','该价格商品规格：12包 天猫买2件 实付97.8元 最终到手价48.9元/件 限新客')
        self.assertEqual(brief['title'],'抽纸')
        self.assertIn('新客',brief['qualifications'])
        self.assertIn('移动端',brief['qualifications'])
        self.assertIn('淘金币',brief['qualifications'])
        self.assertEqual(brief['quantity'],2)
        self.assertIsNone(ar.offer_summary('纸','', '')['previous_cents'])

    def _case_title_amount_is_visible_as_a_clue_but_not_a_quote(self):
        from services.product_search import listing_quote, quote_note
        title = '山丘 抽纸加厚4层76抽*8包餐巾纸卫生纸巾无漂白原生竹浆本色纸 7.3元'
        body = '两个商品一起付款，折主商品7.3元，凑单0.01元，包邮。满14元减5元。'
        detail = {'title': title, 'conditions': body, 'evidence_kind': 'guangdiu_detail'}
        brief = ar.offer_summary(title, 'https://guangdiu.com/detail.php?id=29832200', body,
                                 detail_json=json.dumps(detail, ensure_ascii=False))
        self.assertEqual(brief['price_status']['headline_amount_cents'], 730)
        self.assertEqual(brief['price_status']['state'], 'title_amount_unbound')
        self.assertIsNone(brief['total_cents'])
        self.assertIsNone(brief['quantity'])
        self.assertIsNone(listing_quote({'auto_state': 'missing_price'}, brief))
        self.assertIn('¥7.30', quote_note(brief))
        self.assertIn('不参与低价排序', quote_note(brief))

@grouped_scenarios({
    'test_real_detail_probe_adapters': (
        'guangdiu_detail_cycle_extracts_and_displays_headline_money_without_promoting_it',
        'merchant_product_detail_cycle_upgrades_missing_catalog_price_from_exact_offer',
        'honor_page_estimate_renders_as_a_claim_without_entering_search_or_budget',
    ),
})
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
            page=client.get('/?view=all').get_data(as_text=True)
            self.assertIn('/opportunities/1',page)
            self.assertIn('未形成可用报价',page)
            # An isolated parser amount without a purchase plan cannot pass budget filters.
            self.assertNotIn('/opportunities/1',client.get('/?view=all&budget=13').get_data(as_text=True))
            self.assertNotIn('/opportunities/1',client.get('/?view=all&budget=12').get_data(as_text=True))
            with db.connect() as c:
                self.assertIsNone(c.execute('SELECT buy_cents FROM opportunities WHERE id=1').fetchone()[0])
                c.execute("UPDATE auto_reviews SET state='conflict'")
            self.assertNotIn('/opportunities/1',client.get('/?view=all&budget=13').get_data(as_text=True))

    def _case_guangdiu_detail_cycle_extracts_and_displays_headline_money_without_promoting_it(self):
        title = '山丘 抽纸加厚4层76抽*8包餐巾纸卫生纸巾无漂白原生竹浆本色纸 7.3元'
        url = 'https://guangdiu.com/detail.php?id=29832200'
        body = '两个商品一起付款，折主商品7.3元，凑单0.01元，包邮。满14元减5元。'
        html = f'''<html><div id="mainleft"><h1>{title}</h1>
          <div id="dabstract">{body}</div></div>
          <aside class="recommend">推荐商品 99.9元</aside></html>'''
        with db.connect() as c:
            c.execute("UPDATE sources SET platform='逛丢',name='逛丢',parser='guangdiu',url='https://guangdiu.com/',last_success=CURRENT_TIMESTAMP WHERE id=1")
            c.execute("UPDATE events SET title=?,url=?,snippet=?,published_at=CURRENT_TIMESTAMP,last_seen_at=CURRENT_TIMESTAMP WHERE id=1",
                      (title,url,body))
            c.execute("UPDATE opportunities SET title=?,url=? WHERE id=1",(title,url))
        with patch('scanner._fetch',return_value=html) as fetch:
            result = ar.run_cycle(opportunity_id=1)
        fetch.assert_called_once_with(url)
        with db.connect() as c:
            stored = c.execute('SELECT * FROM auto_reviews WHERE opportunity_id=1').fetchone()
            detail = json.loads(stored['detail_json'])
            self.assertEqual(detail['advertised_cents'],730)
            self.assertEqual(detail['headline_amount_cents'],730)
            self.assertNotIn('99.9元',detail['evidence'])
            self.assertEqual(stored['advertised_cents'],730)
            self.assertIn(stored['state'],('conditional','missing_price'))
        with app.test_client() as client:
            page = client.get('/?view=current&layout=cards').get_data(as_text=True)
            self.assertIn('标题金额线索',page)
            self.assertIn('¥7.30',page)
            self.assertIn('不参与低价排序',page)
            self.assertNotIn('99.9元',page)
            self.assertNotIn('/opportunities/1',client.get('/?budget=8').get_data(as_text=True))

    def _case_merchant_product_detail_cycle_upgrades_missing_catalog_price_from_exact_offer(self):
        url = 'https://product.suning.com/123/456.html'
        title = '苏宁自营 抽纸商品'
        html = '''<html><h1>苏宁自营 抽纸商品</h1>
        <script type="application/ld+json">{"@context":"https://schema.org",
          "@type":"Product","@id":"https://product.suning.com/123/456.html",
          "name":"苏宁自营 抽纸商品","sku":"SKU-456","offers":{
          "@type":"Offer","price":"7.30","priceCurrency":"CNY",
          "availability":"https://schema.org/InStock"}}</script></html>'''
        with db.connect() as c:
            c.execute('DELETE FROM auto_reviews WHERE opportunity_id=1')
            c.execute("UPDATE sources SET platform='苏宁',name='苏宁',parser='suning',url='https://product.suning.com/',last_success=CURRENT_TIMESTAMP WHERE id=1")
            c.execute("UPDATE events SET title=?,url=?,snippet='',published_at=NULL,last_seen_at=CURRENT_TIMESTAMP WHERE id=1",
                      (title,url))
            c.execute("UPDATE opportunities SET title=?,url=? WHERE id=1",(title,url))
        with patch('scanner._fetch',return_value=html) as fetch:
            ar.run_cycle(opportunity_id=1)
        fetch.assert_called_once_with(url)
        with db.connect() as c:
            stored = c.execute('SELECT * FROM auto_reviews WHERE opportunity_id=1').fetchone()
            detail = json.loads(stored['detail_json'])
            self.assertEqual(detail['advertised_cents'],730)
            self.assertEqual(detail['price_evidence'],'schema.org Product/Offer')
            self.assertEqual(stored['state'],'observed')
            self.assertEqual(stored['advertised_cents'],730)
        with app.test_client() as client:
            page = client.get('/?view=current').get_data(as_text=True)
            self.assertIn('¥7.30',page)
            self.assertIn('商城目录公开标价',page)

    def _case_honor_page_estimate_renders_as_a_claim_without_entering_search_or_budget(self):
        from services.merchant_product_page import parse_product_page
        url = 'https://www.honor.com/cn/shop/product/10086983762557.html?cid=132368'
        html = '''<h1>荣耀Earbuds开放式耳机 极夜黑</h1>
          <span id="pro-price-hand">预估到手价</span>
          <input id="pro-price-hide" value="699.00">'''
        detail = parse_product_page(url, html)
        with db.connect() as c:
            c.execute("UPDATE sources SET platform='荣耀商城',name='荣耀商城',parser='honor',url='https://www.honor.com/cn/shop/',status='healthy',enabled=1,last_success=CURRENT_TIMESTAMP WHERE id=1")
            c.execute("UPDATE events SET title=?,url=?,snippet='商城页面标注预估到手价',published_at=NULL,last_seen_at=CURRENT_TIMESTAMP WHERE id=1",
                      (detail['title'],url))
            c.execute("UPDATE opportunities SET title=?,url=?,resource_kind='purchase',topic='home' WHERE id=1",
                      (detail['title'],url))
            c.execute('DELETE FROM auto_reviews WHERE opportunity_id=1')
            c.execute("INSERT INTO auto_reviews(opportunity_id,state,reason,conditions,detail_json,detail_checked_at) VALUES(1,'conditional','页面预估价','预估到手价',?,CURRENT_TIMESTAMP)",
                      (json.dumps(detail,ensure_ascii=False),))
        with app.test_client() as client:
            search = client.get('/?view=all').get_data(as_text=True)
            verification = client.get('/verification?state=conditional').get_data(as_text=True)
            detail_page = client.get('/opportunities/1').get_data(as_text=True)
            budget = client.get('/?view=all&budget=700').get_data(as_text=True)
        for page in (search, verification, detail_page):
            self.assertIn('699.00',page)
            self.assertIn('预估到手价',page)
            self.assertIn('不是',page)
        self.assertNotIn('/opportunities/1', budget)

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

    def test_interrupted_cycle_releases_its_lease(self):
        with patch.object(ar, 'review_all', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                ar.run_cycle()
        with db.connect() as c:
            run = c.execute('SELECT started_at,finished_at FROM review_runs WHERE id=1').fetchone()
        self.assertIsNotNone(run['started_at'])
        self.assertIsNotNone(run['finished_at'])

        with patch.object(ar, 'review_all', return_value={'observed': 1}):
            self.assertEqual(ar.run_cycle(), {'counts': {'observed': 1}, 'probed': 0})

if __name__ == '__main__':
    unittest.main()
