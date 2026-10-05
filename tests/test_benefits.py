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
  with patch('benefits.requests.get',return_value=self.response('<title>特价补贴购</title><p>已抢光 14:00再来</p>'.encode())):
   e=inspect_page(target);self.assertIn('已抢光',e['note']);self.assertIn('14:00再来',e['note']);self.assertNotIn('verified',e)
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

 def test_old_unknown_activity_link_is_kept_with_provenance(self):
  with db.connect() as c:
   c.execute("INSERT INTO sources(id,platform,name,category,url,method,status) VALUES(901,'社区','历史优惠源','优惠','https://example.com','rss','healthy')")
   c.execute("INSERT INTO events(id,source_id,external_key,title,url,snippet,fingerprint,metadata_json,published_at,last_seen_at) VALUES(901,901,'old','旧活动入口','https://example.com/post/old','原文优惠','old',?,'2024-01-01 00:00:00','2024-01-01 00:00:00')",(json.dumps({'activity_links':['https://offers.example/campaign/1']}),))
   c.execute("INSERT INTO opportunities(event_id,source_id,title,category,url) VALUES(901,901,'普通家居优惠','优惠','https://example.com/post/old')")
  sync()
  with db.connect() as c:
   rows=listing(c);self.assertEqual(len(rows),1);self.assertEqual(rows[0]['state'],'unsupported')
   self.assertEqual(rows[0]['url'],'https://offers.example/campaign/1');self.assertEqual(len(rows[0]['sources']),1)
   self.assertEqual(rows[0]['sources'][0]['platform'],'社区')
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
