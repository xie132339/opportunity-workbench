import json,tempfile,unittest
from datetime import datetime,timezone,timedelta
from pathlib import Path
from unittest.mock import patch,MagicMock
import db
from benefits import claims,add,sync,listing,inspect_page,run_cycle,stats,reclassify,product_match_candidates,import_authorized_record
from offer import resource_kind
from link_resolution import resolve,activity_target,enrich
from comparison import merchant_identity

class BenefitTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.patcher=patch.object(db,'DB_PATH',Path(self.temp.name)/'isolated.db');self.patcher.start();db.initialize()
 def tearDown(self):self.patcher.stop();self.temp.cleanup()
 def response(self,body=b'',code=200,location=''):
  r=MagicMock(status_code=code,headers={'location':location});r.__enter__.return_value=r;r.iter_content.return_value=[body];return r
 def test_claims_not_cash_price_or_invented_date(self):
  for title,value in [('10点 跳转𝐚𝐩𝐩抢 5.9-5卷','满5.9减5'),('199-100宠物劵','满199减100'),('京东特价版8-4全品劵','满8减4')]:
   c=claims(title);self.assertIn(value,c['discount']);self.assertEqual(resource_kind(title),'coupon')
  self.assertIn('日期与场次未确认',claims('10点抢券')['schedule'])
  self.assertEqual(resource_kind('plus积分兑换'),'points');self.assertEqual(resource_kind('试用会场多个低价'),'trial');self.assertEqual(resource_kind('京喜特价'),'campaign')
 def test_first_order_shorthand_claim_is_renderable_but_keeps_unit_uncertainty(self):
  claim=claims('京东券后价：首购-1')
  self.assertIn('优惠约¥1.00',claim['discount'])
  self.assertIn('原文简写未标币种',claim['discount'])
  self.assertEqual(claim['discount_claims'][0]['verification'],'source_claim_only')
  add('首购礼金优惠线索','https://u.jd.com/FirstOrder',source_type='user_supplied',
      origin_text='首购-1',kind='coupon')
  from app import app
  html=app.test_client().get('/benefits?q=首购').get_data(as_text=True)
  self.assertIn('首购礼金声称 ¥1.00',html)
  self.assertIn('原文未标人民币单位，仅为简写解析假设',html)
 def test_lottery_guarantee_is_rendered_as_non_cash_source_claim(self):
  claim=claims('预约抽奖2元保底')
  self.assertIn('抽奖最低奖励¥2.00',claim['discount'])
  self.assertIn('未核实',claim['discount'])
  self.assertEqual(claim['discount_claims'][0]['claim_mode'],'source_claimed_minimum_reward')
  add('预约抽奖保底优惠线索','https://offers.example/lottery-minimum',source_type='user_supplied',
      origin_text='预约抽奖2元保底',kind='lottery')
  from app import app
  html=app.test_client().get('/benefits?q=预约抽奖').get_data(as_text=True)
  self.assertIn('抽奖最低奖励',html)
  self.assertIn('保底规则与兑现未核实',html)
  self.assertIn('不计为商品现金折价',html)

 def test_opportunity_detail_does_not_mislabel_claimed_lottery_minimum_as_already_drawn(self):
  with db.connect() as c:
   source=c.execute("INSERT INTO sources(platform,name,category,url,method,status) VALUES('线报酷','测试入口','优惠','https://example.com/feed','rss','healthy')").lastrowid
   event=c.execute("INSERT INTO events(source_id,external_key,title,url,snippet,fingerprint) VALUES(?,?,?,?,?,?)",(source,'lottery','预约抽奖活动','https://example.com/lottery','预约抽奖2元保底','lottery')).lastrowid
   opportunity=c.execute("INSERT INTO opportunities(event_id,source_id,title,category,url,resource_kind) VALUES(?,?,?,?,?,'lottery')",(event,source,'预约抽奖活动','优惠','https://example.com/lottery')).lastrowid
  from app import app
  html=app.test_client().get(f'/opportunities/{opportunity}').get_data(as_text=True)
  self.assertIn('来源声称抽奖有最低奖励',html)
  self.assertIn('保底规则与兑现未核实',html)
  self.assertIn('不计入商品现金折价',html)
  self.assertNotIn('来源称已抽中',html)

 def test_new_discount_forms_render_without_promoting_coupon_face_value_to_deduction(self):
  face=claims('领取20元优惠券')
  percent=claims('立减5%优惠活动')
  rate=claims('会员95折')
  self.assertIn('面额约¥20.00',face['discount'])
  self.assertIn('门槛与可抵金额未核实',face['discount'])
  self.assertIn('立减5%',percent['discount'])
  self.assertIn('9.5折',rate['discount'])
  add('通用格式优惠回归','https://u.jd.com/ClaimFormats',source_type='user_supplied',
      origin_text='会员95折，立减5%，领取20元优惠券',kind='coupon')
  from app import app
  html=app.test_client().get('/benefits?q=通用格式').get_data(as_text=True)
  self.assertIn('优惠券面额候选 20.00元',html)
  self.assertIn('券门槛和实际可扣金额未核实',html)
  self.assertIn('立减5.00%',html)
  self.assertIn('9.50 折',html)
  self.assertIn('source_claim_only',html)
 def test_mixed_offer_renders_subsidy_gift_and_noncash_credit_as_separate_claims(self):
  add('混合优惠候选回归','https://u.jd.com/MixedOffer',source_type='user_supplied',
      origin_text=('活动售价14.36元，领取6-3优惠券，参与立减6.46元，官方补贴减1.29元，'
                   '新品礼金减1元，淘金币可抵3.99元起，实付低至2.61元'),kind='coupon')
  from app import app
  html=app.test_client().get('/benefits?q=混合优惠候选').get_data(as_text=True)
  self.assertIn('补贴金额候选 ¥1.29',html)
  self.assertIn('新品礼金候选 ¥1.00',html)
  self.assertIn('淘金币抵扣价值声称 ¥3.99起',html)
  self.assertIn('非现金权益，不计入现金扣款',html)
  self.assertIn('不得当作可用优惠或到手价',html)
 def test_activity_redirect_retained_not_merchant_sku(self):
  url='https://pro.m.jd.com/mall/active/Abc/index.html?sku=123'
  with patch('link_resolution.requests.get',return_value=self.response(code=302,location=url)):
   r=resolve('https://u.jd.com/Abc');self.assertEqual(r['state'],'activity');self.assertEqual(r['target_url'],url)
  row=dict(id=1,title='权益',metadata_json=json.dumps({'activity_links':['https://u.jd.com/Abc']}))
  cache={'https://u.jd.com/Abc':dict(r,checked_at='2000-01-01 00:00:00',next_check_at='2099-01-01 00:00:00')}
  self.assertTrue(merchant_identity(enrich(row,cache))['key'].startswith('title:'))
 def test_arbitrary_or_login_urls_never_fetched(self):
  for u in ['https://pro.m.jd.com.evil/mall/active/A/index.html','http://127.0.0.1/secret','https://pro.m.jd.com:bad/mall/active/A/index.html','https://user@pro.m.jd.com/mall/active/A/index.html','https://plogin.m.jd.com/login']:
   self.assertIsNone(activity_target(u))
   with patch('benefits.requests.get') as get:inspect_page(u);get.assert_not_called()
 def test_actual_page_notice_and_login_not_coupon_validation(self):
  target='https://pro.m.jd.com/jdlite/active/ABC/index.html'
  with patch('benefits.requests.get',return_value=self.response('<title>特价补贴购</title><p>已抢光 14:00再来 满199减100宠物券，支付立减5元，会员最高8.5折</p>'.encode())):
   e=inspect_page(target);self.assertIn('已抢光',e['note']);self.assertIn('14:00再来',e['note']);self.assertNotIn('verified',e)
   self.assertEqual(e['coupon_claims'][0]['matched_text'],'满199减100宠物券')
   self.assertEqual(e['coupon_claims'][0]['verification'],'source_claim_only')
   self.assertEqual(e['coupon_claims'][0]['evidence_type'],'target_page')
   self.assertEqual([claim['mechanism'] for claim in e['coupon_claims']],[
    'threshold_discount_claim','fixed_reduction_claim','pay_rate_claim'])
  with patch('benefits.requests.get',return_value=self.response(code=302,location='https://plogin.m.jd.com/login')) as get:
   self.assertEqual(inspect_page(target)['state'],'login_required');self.assertEqual(get.call_count,1)
 def test_repeat_import_does_not_duplicate_or_forge_publish_time(self):
  for _ in range(2):self.assertTrue(add('10点5.9-5卷','https://u.jd.com/Abc',source_type='user_supplied'))
  with db.connect() as c:
   rows=listing(c);self.assertEqual(len(rows),1);self.assertIsNone(rows[0]['published_at']);self.assertEqual(rows[0]['source_type'],'user_supplied');self.assertTrue(rows[0]['old_check'])
 def test_listing_supports_bounded_pages(self):
  for index in range(25):add(f'优惠入口{index:02d}',f'https://offers.example/coupon/{index}',kind='coupon')
  with db.connect() as c:
   first=listing(c,limit=21,offset=0);second=listing(c,limit=21,offset=20)
  self.assertEqual(len(first),21);self.assertEqual(len(second),5)
  self.assertTrue(set(item['id'] for item in first[:20]).isdisjoint(item['id'] for item in second))
 def test_background_discovers_coupon_from_existing_feed(self):
  with db.connect() as c:
   c.execute("INSERT INTO sources(id,platform,name,category,url,method,status) VALUES(900,'test','test','优惠','https://example.com','rss','healthy')")
   c.execute("INSERT INTO events(id,source_id,external_key,title,url,snippet,fingerprint,metadata_json) VALUES(900,900,'x','199-100宠物劵','https://example.com/p/1','抢券','x',?)",(json.dumps({'activity_links':['https://u.jd.com/Abc']}),))
   c.execute("INSERT INTO opportunities(event_id,source_id,title,category,url) VALUES(900,900,'199-100宠物劵','优惠','https://example.com/p/1')")
  sync();sync()
  with db.connect() as c:
   rows=listing(c);self.assertEqual(len(rows),1);self.assertEqual(rows[0]['source_type'],'collected');self.assertEqual(rows[0]['source_url'],'https://example.com/p/1')
 def test_automatic_page_refresh_and_backoff(self):
  add('活动','https://u.jd.com/Abc',source_type='user_supplied')
  with patch('benefits.resolve',return_value=dict(state='activity',target_url='https://pro.m.jd.com/mall/active/ABC/index.html',reason='activity')),patch('benefits.inspect_page',return_value=dict(state='read',note='活动页，规则未核实')) as check:
   self.assertEqual(run_cycle(),{'read':1});self.assertEqual(run_cycle(),{});self.assertEqual(check.call_count,1)
  with db.connect() as c:
   r=listing(c)[0];self.assertFalse(r['old_check']);self.assertEqual(r['evidence']['state'],'read');self.assertIsNone(r['published_at'])
 def test_user_supplied_entries_are_checked_before_older_generic_queue(self):
  manual='https://pro.m.jd.com/mall/active/MANUAL/index.html'
  ordinary='https://pro.m.jd.com/mall/active/ORDINARY/index.html'
  add('手动提交优惠入口',manual,source_type='user_supplied',kind='campaign')
  add('普通历史优惠入口',ordinary,source_type='collected',kind='campaign')
  with db.connect() as c:
   # The old ordering preferred NULL next_check_at rows, starving this user entry.
   c.execute("UPDATE benefit_resources SET next_check_at='2000-01-01 00:00:00' WHERE url=?",(manual,))
   c.execute("UPDATE benefit_resources SET next_check_at=NULL WHERE url=?",(ordinary,))
   before=c.execute('SELECT COUNT(*) FROM benefit_resources').fetchone()[0]
  with patch('benefits.inspect_page',return_value=dict(state='read',note='测试读取')) as check:
   self.assertEqual(run_cycle(limit=1),{'read':1})
   self.assertEqual(check.call_args.args[0],manual)
  with db.connect() as c:
   self.assertIsNotNone(c.execute('SELECT checked_at FROM benefit_resources WHERE url=?',(manual,)).fetchone()[0])
   self.assertIsNone(c.execute('SELECT checked_at FROM benefit_resources WHERE url=?',(ordinary,)).fetchone()[0])
   self.assertEqual(c.execute('SELECT COUNT(*) FROM benefit_resources').fetchone()[0],before)
   self.assertIsNotNone(c.execute('SELECT 1 FROM benefit_resources WHERE url=?',(ordinary,)).fetchone())

 def test_http_refresh_does_not_renew_browser_observation(self):
  from benefits import record_browser_observation
  add('5.9-5券','https://u.jd.com/Abc',source_type='user_supplied')
  evidence=dict(source_url='https://u.jd.com/Abc',checked_at='2020-01-01T00:00:00Z',coupons=[dict(scope_label='家居',amount='4',symbol='￥',threshold_text='满4.01元可用')])
  record_browser_observation(evidence)
  with patch('benefits.resolve',return_value=dict(state='activity',target_url='https://pro.m.jd.com/mall/active/A/index.html',reason='activity')),patch('benefits.inspect_page',return_value=dict(state='read',note='test')):
   run_cycle()
  with db.connect() as c:
   item=listing(c)[0];self.assertTrue(item['observation_old']);self.assertEqual(item['observation_time'],'2020-01-01 00:00:00');self.assertEqual(item['browser_observation']['coupons'][0]['amount'],'4')
 def test_product_relation_requires_same_source_record_not_same_platform_guess(self):
  from benefits import related
  with db.connect() as c:
   c.execute("INSERT INTO sources(id,platform,name,category,url,method,status) VALUES(990,'京东','测试','优惠','https://example.com','rss','healthy')")
   c.execute("INSERT INTO events(id,source_id,external_key,title,url,fingerprint) VALUES(990,990,'one','纸巾','https://example.com/one','one')")
   c.execute("INSERT INTO opportunities(id,event_id,source_id,title,category,url) VALUES(1,990,990,'纸巾','零售优惠','https://example.com/one')")
  add('10点医疗器械膨胀卷','https://u.jd.com/Abc');add('199-100宠物劵','https://u.jd.com/Def')
  add('满5.9减5券','https://u.jd.com/Ghi',source_opportunity_id=1,kind='coupon')
  with db.connect() as c:
   c.execute("UPDATE benefit_resources SET state='activity' WHERE url='https://u.jd.com/Ghi'")
   self.assertEqual(related(c,2),[])
   r=related(c,1);self.assertEqual(len(r),1);self.assertIn('满5.9减5',r[0]['title'])
   self.assertEqual(r[0]['relation_state'],'source_linked')
   c.execute("""UPDATE benefit_product_relations SET state='confirmed',basis='商家规则明确SKU适用',
                evidence_url='https://merchant.example/rule',checked_at='2026-10-04 12:00:00',
                valid_until='2026-10-05 00:00:00',stackable='no' WHERE opportunity_id=1""")
   confirmed=related(c,1)[0];self.assertEqual(confirmed['relation_state'],'confirmed')
   self.assertIn('公开证据支持适用',confirmed['relation_label'])
 def test_pending_source_link_is_visible_on_its_product(self):
  from benefits import related,linked_counts
  with db.connect() as c:
   c.execute("INSERT INTO sources(id,platform,name,category,url,method,status) VALUES(991,'京东','测试','优惠','https://example.com','rss','healthy')")
   c.execute("INSERT INTO events(id,source_id,external_key,title,url,fingerprint) VALUES(991,991,'pending','纸巾','https://example.com/pending','pending')")
   c.execute("INSERT INTO opportunities(id,event_id,source_id,title,category,url) VALUES(2,991,991,'纸巾','零售优惠','https://example.com/pending')")
  add('待识别入口：纸巾','https://u.jd.com/Pending',source_opportunity_id=2,kind='pending')
  with db.connect() as c:
   c.execute("UPDATE benefit_resources SET state='blocked' WHERE url='https://u.jd.com/Pending'")
   items=related(c,2);counts=linked_counts(c,[2])
  self.assertEqual(len(items),1);self.assertEqual(items[0]['kind'],'pending')
  self.assertEqual(counts[2]['source_linked'],1)

 def test_cross_source_discount_candidate_requires_same_explicit_merchant_item_id(self):
  with db.connect() as c:
   for sid in (994,995):
    c.execute("INSERT INTO sources(id,platform,name,category,url,method,status,last_success) VALUES(?,?,?,'零售优惠',?,'rss','healthy',CURRENT_TIMESTAMP)",
              (sid,'京东',f'来源{sid}',f'https://feed{sid}.example'))
    item_id='123456' if sid==994 else '999999'
    c.execute("INSERT INTO events(id,source_id,external_key,title,url,snippet,fingerprint,metadata_json,published_at) VALUES(?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
              (sid,sid,'item',f'商品{item_id}',f'https://post{sid}.example','线索',str(sid),
               json.dumps({'activity_links':[f'https://item.jd.com/{item_id}.html']})))
    c.execute("INSERT INTO opportunities(id,event_id,source_id,title,category,url) VALUES(?,?,?,?,?,?)",
              (sid,sid,sid,f'商品{item_id}','零售优惠',f'https://post{sid}.example'))
  add('满99减10券','https://u.jd.com/Coupon123',source_opportunity_id=994,kind='coupon')
  add('满99减10券','https://u.jd.com/Coupon999',source_opportunity_id=995,kind='coupon')
  target=dict(id=1,title='同商品线索',url='https://target.example/post',
              metadata_json=json.dumps({'activity_links':['https://item.jd.com/123456.html']}),
              detail_json='{}')
  with db.connect() as c:
   candidates=product_match_candidates(c,target)
  self.assertEqual([x['url'] for x in candidates],['https://u.jd.com/Coupon123'])
  self.assertEqual(candidates[0]['relation_state'],'product_match_candidate')
  self.assertIn('未核实',candidates[0]['relation_label'])
  self.assertIn('尚未单独读取',candidates[0]['evidence']['note'])

 def test_authorized_coupon_import_matches_only_fresh_explicit_product_scope(self):
  observed=datetime.now(timezone.utc).isoformat()
  base=dict(provider='已授权平台接口',platform='jd',scope_type='item',
            source_url='https://open.example/docs/coupon',observed_at=observed,
            discount_cents=1000,threshold_cents=9900,eligibility='京东Plus会员',region='北京',
            valid_until=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat(),
            stackable='unknown',raw_rules='满99元减10元')
  import_authorized_record(dict(base,title='同款券',coupon_url='https://pro.m.jd.com/mall/active/Coupon/index.html',
                                eligible_product_keys=['jd:123456']))
  import_authorized_record(dict(base,title='其他SKU券',coupon_url='https://offers.example/c/other',
                                scope_type='sku',eligible_product_keys=['jd:123456']))
  import_authorized_record(dict(base,title='已过期券',coupon_url='https://offers.example/c/expired',
                                valid_until=(datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat(),
                                eligible_product_keys=['jd:123456']))
  import_authorized_record(dict(base,title='滞后券',coupon_url='https://offers.example/c/stale',
                                observed_at=(datetime.now(timezone.utc)-timedelta(hours=3)).isoformat(),
                                eligible_product_keys=['jd:123456']))
  target=dict(id=1,title='同款商品',url='https://item.jd.com/123456.html')
  with db.connect() as c:candidates=product_match_candidates(c,target)
  self.assertEqual([item['title'] for item in candidates],['同款券'])
  self.assertEqual(candidates[0]['discount'],'接口数据声称减 ¥10.00，门槛 ¥99.00')
  self.assertIn('未核实',candidates[0]['relation_label'])
  self.assertEqual(candidates[0]['evidence']['eligibility'],'京东Plus会员')

 def test_authorized_coupon_import_rejects_bad_records_without_partial_resource(self):
  record=dict(provider='接口',platform='jd',scope_type='item',title='错误券',
              coupon_url='https://offers.example/c/bad',source_url='https://open.example/docs',
              observed_at=datetime.now(timezone.utc).isoformat(),eligible_product_keys=['taobao:123'],
              discount_cents=100)
  with self.assertRaises(ValueError):import_authorized_record(record)
  with db.connect() as c:self.assertEqual(c.execute("SELECT COUNT(*) FROM benefit_resources").fetchone()[0],0)

 def test_authorized_coupon_import_accepts_exact_apple_part_number_scope(self):
  observed=datetime.now(timezone.utc).isoformat()
  base=dict(provider='已授权平台接口',platform='apple',scope_type='item',
            source_url='https://open.example/docs/apple-coupons',observed_at=observed,
            eligible_product_keys=['apple:FHFA4CH/A'],discount_cents=50000)
  import_authorized_record(dict(base,title='Mac翻新专属优惠',coupon_url='https://offers.example/c/apple-mac'))
  import_authorized_record(dict(base,title='其他颜色商品券',coupon_url='https://offers.example/c/apple-other',
                                eligible_product_keys=['apple:FHFD4CH/A']))
  target=dict(id=1,title='翻新 MacBook Neo',url='https://www.apple.com.cn/shop/product/fhfa4ch/a',
              metadata_json='{}',detail_json='{}')
  with db.connect() as c:candidates=product_match_candidates(c,target)
  self.assertEqual([item['title'] for item in candidates],['Mac翻新专属优惠'])
  self.assertEqual(candidates[0]['relation_state'],'product_match_candidate')

 def test_old_unknown_activity_link_is_kept_with_provenance(self):
  with db.connect() as c:
   c.execute("INSERT INTO sources(id,platform,name,category,url,method,status) VALUES(901,'社区','历史优惠源','优惠','https://example.com','rss','healthy')")
   c.execute("INSERT INTO events(id,source_id,external_key,title,url,snippet,fingerprint,metadata_json,published_at,last_seen_at) VALUES(901,901,'old','旧活动入口','https://example.com/post/old','原文优惠','old',?,'2024-01-01 00:00:00','2024-01-01 00:00:00')",(json.dumps({'activity_links':['https://offers.example/campaign/1']}),))
   c.execute("INSERT INTO opportunities(event_id,source_id,title,category,url) VALUES(901,901,'普通家居优惠','优惠','https://example.com/post/old')")
  sync();sync()
  with db.connect() as c:
   rows=listing(c);self.assertEqual(len(rows),1);self.assertEqual(rows[0]['state'],'unsupported')
   self.assertEqual(rows[0]['url'],'https://offers.example/campaign/1');self.assertEqual(len(rows[0]['sources']),1)
   self.assertEqual(rows[0]['sources'][0]['platform'],'社区')
   self.assertEqual(rows[0]['source_last_seen_at'],'2024-01-01 00:00:00')
   self.assertEqual(rows[0]['last_seen_at'],'2024-01-01 00:00:00')
   self.assertEqual(rows[0]['sources'][0]['last_seen_at'],'2024-01-01 00:00:00')
   self.assertEqual(c.execute('SELECT COUNT(*) FROM events').fetchone()[0],1)
   self.assertEqual(c.execute('SELECT COUNT(*) FROM opportunities').fetchone()[0],1)

 def test_benefit_page_distinguishes_source_reobservation_from_offer_validity(self):
  with db.connect() as c:
   c.execute("INSERT INTO sources(id,platform,name,category,url,method,status) VALUES(907,'聚合线索','旧优惠源','优惠','https://example.com','rss','healthy')")
   c.execute("INSERT INTO events(id,source_id,external_key,title,url,snippet,fingerprint,published_at,last_seen_at) VALUES(907,907,'old','旧满减券','https://example.com/post/old','满99减10','old','2024-01-01 00:00:00','2024-01-02 00:00:00')")
   c.execute("INSERT INTO opportunities(id,event_id,source_id,title,category,url) VALUES(907,907,907,'旧满减券','优惠','https://example.com/post/old')")
  add('旧满99减10券','https://u.jd.com/Old',source_opportunity_id=907,
      source_url='https://example.com/post/old',kind='coupon',
      published_at='2024-01-01 00:00:00',source_observed_at='2024-01-02 00:00:00')
  from app import app
  response=app.test_client().get('/benefits')
  self.assertEqual(response.status_code,200)
  html=response.get_data(as_text=True)
  self.assertIn('来源采集端最近读到',html)
  self.assertIn('不代表优惠仍有效',html)
  self.assertIn('商品适用证明：0 条；原文同条关联：1 条',html)
  self.assertIn('不能作为优惠已适用或可计算到手价的证据',html)
 def test_page_shows_read_but_unparsed_and_keeps_source_claim_types_distinct(self):
  add('满100减10家用券','https://u.jd.com/ClaimTypes',source_type='user_supplied',
      origin_text='满100减10家用券，支付立减5元，会员最高8.5折',kind='coupon')
  with db.connect() as c:
   c.execute("UPDATE benefit_resources SET state='activity',checked_at=CURRENT_TIMESTAMP,next_check_at=datetime('now','+15 minutes'),evidence_json=? WHERE url='https://u.jd.com/ClaimTypes'",
       (json.dumps({'state':'read','coupon_claims':[],'note':'页面文字已读取'}),))
  from app import app
  html=app.test_client().get('/benefits?q=家用券').get_data(as_text=True)
  self.assertIn('页面已同步，未识别到明确金额/门槛',html)
  self.assertIn('满 ¥100.00 减 ¥10.00',html)
  self.assertIn('立减 ¥5.00',html)
  self.assertIn('8.50 折',html)
  self.assertIn('source_claim_only',html)
  self.assertIn('2处原文命中',html)
  self.assertIn('逐条查看优惠候选与原文命中位置',html)
  self.assertIn('不得当作可用优惠或到手价',html)

 def test_new_claim_mechanisms_render_without_500_or_cash_mislabel(self):
  add('新机制渲染回归','https://u.jd.com/NewClaimMechanisms',source_type='user_supplied',
      origin_text='满1件减1200元，补贴5元，淘金币1.92元，4个号抽到了1元',kind='coupon')
  from app import app
  response=app.test_client().get('/benefits?q=新机制渲染回归')
  self.assertEqual(response.status_code,200)
  html=response.get_data(as_text=True)
  self.assertIn('满1件减 ¥1200.00',html)
  self.assertIn('补贴金额候选 ¥5.00',html)
  self.assertIn('淘金币金额等价声称约¥1.92',html)
  self.assertIn('原文未说明可直接抵扣',html)
  self.assertIn('来源声称曾随机抽中奖励 ¥1.00',html)
  self.assertIn('不得当作可用优惠或到手价',html)

 def test_opportunity_detail_labels_coin_equivalent_without_claiming_cash_reduction(self):
  body='领取减1922元优惠券，来源称淘金币1.92元，补贴5元，满1件减1200元'
  with db.connect() as c:
   c.execute("INSERT INTO sources(id,platform,name,category,url,method,status) VALUES(980,'淘宝','回归','零售优惠','https://example.com/feed','manual','healthy')")
   c.execute("INSERT INTO events(id,source_id,external_key,title,url,snippet,fingerprint) VALUES(980,980,'coin-equivalent','清洁剂优惠','https://example.com/post',?, 'coin-equivalent')",(body,))
   c.execute("INSERT INTO opportunities(id,event_id,source_id,title,category,url) VALUES(980,980,980,'清洁剂优惠','零售优惠','https://example.com/post')")
   c.execute("INSERT INTO auto_reviews(opportunity_id,state,reason,conditions,detail_json) VALUES(980,'observed','source text only',?,?)",
             (body,json.dumps({'conditions':body},ensure_ascii=False)))
  from app import app
  response=app.test_client().get('/opportunities/980')
  self.assertEqual(response.status_code,200)
  html=response.get_data(as_text=True)
  self.assertIn('非现金积分/金币线索（不计入现金减项）',html)
  self.assertIn('来源称金额等价',html)
  self.assertIn('原文未说明可直接抵扣',html)
  self.assertNotIn('来源称积分/金币权益可抵',html)
  self.assertIn('券面额声称：1922元优惠券',html)
  self.assertNotIn('立减 ¥1922.00',html)

 def test_same_activity_keeps_every_source_relation(self):
  with db.connect() as c:
   c.execute("INSERT INTO sources(id,platform,name,category,url,method,status) VALUES(902,'A站','A','优惠','https://a.example','rss','healthy')")
   c.execute("INSERT INTO sources(id,platform,name,category,url,method,status) VALUES(903,'B站','B','优惠','https://b.example','rss','healthy')")
   for n in (902,903):
    c.execute("INSERT INTO events(id,source_id,external_key,title,url,snippet,fingerprint,metadata_json) VALUES(?,?,?,?,?,?,?,?)",(n,n,str(n),'同一活动',f'https://{chr(n-805)}.example/post','优惠',str(n),json.dumps({'activity_links':['https://u.jd.com/Same']})))
    c.execute("INSERT INTO opportunities(event_id,source_id,title,category,url) VALUES(?,?,?,?,?)",(n,n,'家居优惠','优惠',f'https://{chr(n-805)}.example/post'))
  sync();sync()
  with db.connect() as c:
   rows=listing(c);self.assertEqual(len(rows),1);self.assertEqual(len(rows[0]['sources']),2)
   self.assertEqual(stats(c)['sources'],2)
 def test_cycle_reclassifies_legacy_non_benefit_supported_link(self):
  add('旧入口','https://u.jd.com/Legacy')
  with db.connect() as c:c.execute("UPDATE benefit_resources SET state='non_benefit',reason='旧分类' WHERE url='https://u.jd.com/Legacy'")
  with patch('benefits.resolve',return_value=dict(state='resolved',target_url='https://item.jd.com/123.html',reason='商品页')) as resolver:
   self.assertEqual(run_cycle(),{'resolved':1});self.assertEqual(resolver.call_count,1)
  with db.connect() as c:
   row=c.execute("SELECT state,target_url FROM benefit_resources WHERE url='https://u.jd.com/Legacy'").fetchone()
  self.assertEqual((row['state'],row['target_url']),('product','https://item.jd.com/123.html'))

 def test_product_resolution_is_classified_and_hidden(self):
  add('待识别链接','https://u.jd.com/Product')
  with patch('benefits.resolve',return_value=dict(state='resolved',target_url='https://item.jd.com/123.html',reason='商品页')):
   self.assertEqual(run_cycle(),{'resolved':1})
  with db.connect() as c:
   self.assertEqual(listing(c),[]);self.assertEqual(stats(c)['product'],1)
 def test_unknown_title_activity_link_is_registered_then_identified(self):
  with db.connect() as c:
   c.execute("INSERT INTO sources(id,platform,name,category,url,method,status) VALUES(904,'消息源','日常线索','优惠','https://example.com','rss','healthy')")
   c.execute("INSERT INTO events(id,source_id,external_key,title,url,snippet,fingerprint,metadata_json) VALUES(904,904,'generic','今天的家居信息','https://example.com/post/generic','看看这个','generic',?)",(json.dumps({'activity_links':['https://u.jd.com/Generic']}),))
   c.execute("INSERT INTO opportunities(event_id,source_id,title,category,url) VALUES(904,904,'今天的家居信息','优惠','https://example.com/post/generic')")
  sync()
  with patch('benefits.resolve',return_value=dict(state='activity',target_url='https://pro.m.jd.com/mall/active/ABC/index.html',reason='活动页')),patch('benefits.inspect_page',return_value=dict(state='read',note='读到活动页')):
   run_cycle()
  with db.connect() as c:
   rows=listing(c);self.assertEqual(len(rows),1);self.assertEqual(rows[0]['state'],'activity');self.assertEqual(rows[0]['kind'],'campaign')
 def test_raw_links_are_retained_without_being_misrepresented_as_benefits(self):
  with db.connect() as c:
   c.execute("INSERT INTO sources(id,platform,name,category,url,method,status) VALUES(905,'消息源','带页脚链接','优惠','https://example.com','rss','healthy')")
   links=['https://social.example/user/profile','https://m.tb.cn/short-offer']
   c.execute("INSERT INTO events(id,source_id,external_key,title,url,snippet,fingerprint,metadata_json) VALUES(905,905,'footer','家居商品12元','https://example.com/post/footer','商品介绍','footer',?)",(json.dumps({'activity_links':links}),))
   c.execute("INSERT INTO opportunities(event_id,source_id,title,category,url) VALUES(905,905,'家居商品12元','优惠','https://example.com/post/footer')")
  sync();reclassify()
  with db.connect() as c:
   summary=stats(c);self.assertEqual(summary['discovered'],2);self.assertEqual(summary['total'],1);self.assertEqual(summary['non_benefit'],1)
   rows=listing(c);self.assertEqual([r['url'] for r in rows],['https://m.tb.cn/short-offer'])
 def test_reclassify_consumes_current_shared_product_resolution(self):
  add('共享商品入口','https://u.jd.com/Shared',kind='pending')
  with db.connect() as c:
   c.execute("INSERT INTO link_resolutions(url,target_url,state,reason,checked_at,next_check_at) VALUES('https://u.jd.com/Shared','https://item.jd.com/456.html','resolved','test',CURRENT_TIMESTAMP,'2099-01-01 00:00:00')")
  self.assertEqual(reclassify(),1)
  with db.connect() as c:
   row=c.execute("SELECT state,target_url FROM benefit_resources WHERE url='https://u.jd.com/Shared'").fetchone()
  self.assertEqual((row['state'],row['target_url']),('product','https://item.jd.com/456.html'))

 def test_legacy_target_evidence_restores_activity_state(self):
  add('旧券入口','https://u.jd.com/Legacy',kind='coupon')
  with db.connect() as c:
   c.execute("UPDATE benefit_resources SET state='pending',target_url='https://pro.m.jd.com/mall/active/ABC/index.html?share=1' WHERE url='https://u.jd.com/Legacy'")
  self.assertEqual(reclassify(),1)
  with db.connect() as c:
   row=listing(c)[0];self.assertEqual(row['state'],'activity');self.assertIn('已有解析证据',row['reason'])
