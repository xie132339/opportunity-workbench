import json,unittest,tempfile,sqlite3
from pathlib import Path
from unittest.mock import patch,MagicMock
from datetime import datetime,timezone,timedelta
import requests,db
from bs4 import BeautifulSoup
from link_resolution import (supported,intermediate_hop,canonical_target,login_return_target,
                             resolve,enrich,run_cycle,probe_resolved_pages)
from comparison import merchant_identity

class LinkResolutionTests(unittest.TestCase):
    def response(self,code=200,body=b'',location=''):
        r=MagicMock(status_code=code,headers={'location':location});r.__enter__.return_value=r;r.iter_content.return_value=[body];return r
    def test_public_two_hops_strips_tracking(self):
        with patch('link_resolution.requests.get',side_effect=[self.response(body=b"var hrl='https://u.jd.com/jda?public=1'"),self.response(302,location='https://item.jd.com/123456.html?tracking=discard')]) as get:
            r=resolve('https://u.jd.com/Abc123');self.assertEqual(r['target_url'],'https://item.jd.com/123456.html');self.assertEqual(get.call_count,2)
            self.assertFalse(get.call_args.kwargs['allow_redirects'])
    def test_guangdiu_to_jd_union_login_chain(self):
        jdc='https://union-click.jd.com/jdc?p=public'
        jda='https://union-click.jd.com/jda?p=public&a=1'
        login='https://passport.jd.com/new/login.aspx?ReturnUrl=https%3A%2F%2Fitem.jd.com%2F10140627203620.html%3Ftrack%3Dx'
        responses=[self.response(body=("var hop='"+jdc+"'").encode()),
                   self.response(body=("var hrl='"+jda+"'").encode()),
                   self.response(302,location=login)]
        with patch('link_resolution.requests.get',side_effect=responses) as get:
            result=resolve('https://guangdiu.com/go.php?id=29808286')
        self.assertEqual(result['state'],'resolved')
        self.assertEqual(result['target_url'],'https://item.jd.com/10140627203620.html')
        self.assertEqual(get.call_count,3)
        self.assertIn('Mozilla/5.0',get.call_args.kwargs['headers']['User-Agent'])
        self.assertTrue(intermediate_hop(jda))
        self.assertFalse(intermediate_hop('https://union-click.jd.com.evil/jda?p=x'))

    def test_invalid_and_external_links_never_fetched(self):
        for u in ['https://u.jd.com:bad/Abc','https://u.jd.com.evil/Abc','http://u.jd.com/Abc','https://user@u.jd.com/Abc','https://u.jd.com/Abc?q=1','https://u.jd.com/Abc#x']:
            with patch('link_resolution.requests.get') as get:self.assertFalse(supported(u));resolve(u);get.assert_not_called()
        self.assertIsNone(canonical_target('https://item.jd.com:bad/123.html'))
        for u in ['http://127.0.0.1/secret','https://login.jd.com/','https://u.jd.com:bad/Abc']:
            with patch('link_resolution.requests.get',return_value=self.response(302,location=u)) as get:
                self.assertEqual(resolve('https://u.jd.com/Abc')['state'],'blocked');self.assertEqual(get.call_count,1)
    def test_validation_size_failure_and_jump_limit(self):
        for code in (401,403,429):
            with patch('link_resolution.requests.get',return_value=self.response(code)) as get:
                self.assertEqual(resolve('https://u.jd.com/Abc')['state'],'blocked');self.assertEqual(get.call_count,1)
        with patch('link_resolution.requests.get',return_value=self.response(body=b'x'*150001)):self.assertEqual(resolve('https://u.jd.com/Abc')['state'],'blocked')
        with patch('link_resolution.requests.get',side_effect=requests.Timeout):self.assertEqual(resolve('https://u.jd.com/Abc')['state'],'retry')
        with patch('link_resolution.requests.get',return_value=self.response(302,location='https://u.jd.com/Abc')) as get:
            self.assertEqual(resolve('https://u.jd.com/Abc')['state'],'unresolved');self.assertEqual(get.call_count,5)

    def test_known_jd_login_return_extracts_only_canonical_product(self):
        login='https://passport.jd.com/new/login.aspx?ReturnUrl=https%3A%2F%2Fitem.jd.com%2F10140627203620.html%3Ftrack%3Dx'
        self.assertEqual(login_return_target(login),'https://item.jd.com/10140627203620.html')
        with patch('link_resolution.requests.get',return_value=self.response(302,location=login)):
            result=resolve('https://u.jd.com/Abc')
        self.assertEqual(result['state'],'resolved');self.assertEqual(result['target_url'],'https://item.jd.com/10140627203620.html')
        for unsafe in [
            'https://passport.jd.com.evil/new/login.aspx?ReturnUrl=https%3A%2F%2Fitem.jd.com%2F1.html',
            'https://passport.jd.com/new/login.aspx?ReturnUrl=http%3A%2F%2F127.0.0.1%2Fsecret',
            'https://passport.jd.com/new/login.aspx?ReturnUrl=https%3A%2F%2Fevil.example%2F1',
            'https://passport.jd.com/other?ReturnUrl=https%3A%2F%2Fitem.jd.com%2F1.html']:
            self.assertIsNone(login_return_target(unsafe))

    def test_domestic_short_links_resolve_only_to_product_pages(self):
        targets={
            'https://m.tb.cn/h.AbC123':'https://item.taobao.com/item.htm?id=123',
            'https://m.tb.cn/h.H5Taobao123':'https://h5.m.taobao.com/awp/core/detail.htm?id=987654&ft=t',
            'https://p.pinduoduo.com/AbC123':'https://mobile.yangkeduo.com/goods.html?goods_id=456',
        }
        for source,target in targets.items():
            with patch('link_resolution.requests.get',return_value=self.response(302,location=target)):
                result=resolve(source)
                expected=canonical_target(target)
                self.assertEqual(result['target_url'],expected)
        self.assertEqual(canonical_target('https://h5.m.taobao.com/awp/core/detail.htm?id=987654&ft=t'),
                         'https://h5.m.taobao.com/awp/core/detail.htm?id=987654')
        self.assertEqual(canonical_target('https://product.suning.com/0000/789.html?track=x'),'https://product.suning.com/0000/789.html')
        self.assertEqual(canonical_target('https://detail.vip.com/detail-12-34.html?pcf=x'),'https://detail.vip.com/detail-12-34.html')
    def test_expired_or_future_cache_not_identity_evidence(self):
        now=datetime.now(timezone.utc);fmt=lambda d:d.strftime('%Y-%m-%d %H:%M:%S')
        url='https://u.jd.com/Abc';row=dict(id=1,title='手机',metadata_json=json.dumps({'activity_links':[url]}),detail_json='{}')
        good=dict(state='resolved',target_url='https://item.jd.com/123.html',checked_at=fmt(now-timedelta(minutes=1)),next_check_at=fmt(now+timedelta(hours=1)))
        self.assertEqual(merchant_identity(enrich(row,{url:good}))['key'],'jd:123')
        for e in [dict(good,next_check_at=fmt(now-timedelta(seconds=1))),dict(good,checked_at=fmt(now+timedelta(seconds=5))),dict(good,state='blocked')]:self.assertTrue(merchant_identity(enrich(row,{url:e}))['key'].startswith('title:'))
        self.assertNotIn('resolved_link_evidence',json.loads(row['metadata_json']))
        row['detail_json']=row['metadata_json']
        self.assertEqual(len(json.loads(enrich(row,{url:good})['metadata_json'])['resolved_link_evidence']),1)
    def test_multiple_resolved_products_stay_ambiguous(self):
        now=datetime.now(timezone.utc);fmt=lambda d:d.strftime('%Y-%m-%d %H:%M:%S');urls=['https://u.jd.com/Abc','https://u.jd.com/Def'];cache={u:dict(state='resolved',target_url=f'https://item.jd.com/{i}.html',checked_at=fmt(now-timedelta(seconds=1)),next_check_at=fmt(now+timedelta(hours=1))) for i,u in enumerate(urls)}
        self.assertTrue(merchant_identity(enrich(dict(id=1,title='两件',metadata_json=json.dumps({'activity_links':urls})),cache))['conflict'])

    def test_direct_product_link_is_available_without_waiting_for_short_link_worker(self):
        source='https://item.jd.com/123456.html?track=source'
        row=enrich(dict(id=1,title='原文商品标题',metadata_json=json.dumps({'activity_links':[source]})),{})
        self.assertEqual(row['product_links'][0]['target_url'],'https://item.jd.com/123456.html')
        self.assertEqual(row['product_links'][0]['resolution_state'],'direct')
        self.assertEqual(row['product_links'][0]['label'],'原文商品页链接')
        with patch('link_resolution.requests.get') as get:
            self.assertEqual(resolve(source)['target_url'],'https://item.jd.com/123456.html')
            get.assert_not_called()
        row=enrich(dict(id=2,title='商城目录商品',url='https://item.jd.com/123456.html',metadata_json='{}'),{})
        self.assertEqual(row['product_links'][0]['target_url'],'https://item.jd.com/123456.html')

    def test_short_link_action_uses_merchant_target_and_keeps_other_link_types_separate(self):
        now=datetime.now(timezone.utc);fmt=lambda d:d.strftime('%Y-%m-%d %H:%M:%S')
        short='https://u.jd.com/Abc';target='https://item.jd.com/123456.html'
        coupon='https://coupon.m.jd.com/coupons/show.action?key=public'
        unknown='https://www.douyin.com/video/123'
        cache={short:dict(state='resolved',target_url=target,checked_at=fmt(now-timedelta(minutes=1)),
            next_check_at=fmt(now+timedelta(hours=1)),product_page_json=json.dumps({'state':'public_price',
            'checked_at':fmt(now-timedelta(minutes=1)),'next_check_at':fmt(now+timedelta(hours=1)),
            'title':'商家商品页真实标题','advertised_cents':1990}))}
        row=enrich(dict(id=1,title='线报文章标题',metadata_json=json.dumps({'activity_links':[short,coupon,unknown]})),cache)
        self.assertEqual(len(row['product_links']),1)
        self.assertEqual(row['product_links'][0]['target_url'],target)
        self.assertEqual(row['product_links'][0]['label'],'短链已解析到商品页')
        self.assertEqual(row['product_links'][0]['product_page']['title'],'商家商品页真实标题')
        self.assertEqual(row['product_links'][0]['title_alignment'],'different')
        self.assertEqual(row['activity_targets'][0]['url'],coupon)
        self.assertEqual(row['unclassified_links'][0]['url'],unknown)

    def test_unresolved_purchase_short_link_is_not_mislabeled_as_product(self):
        now=datetime.now(timezone.utc);fmt=lambda d:d.strftime('%Y-%m-%d %H:%M:%S')
        url='https://u.jd.com/Abc'
        cache={url:dict(state='blocked',target_url=None,reason='平台限制访问',
                        checked_at=fmt(now-timedelta(minutes=1)),next_check_at=fmt(now+timedelta(minutes=30)))}
        row=enrich(dict(id=1,title='商品',metadata_json=json.dumps({'activity_links':[url]})),cache)
        self.assertEqual(row['product_links'],[])
        self.assertEqual(row['pending_purchase_links'][0]['url'],url)
        self.assertIn('平台限制访问',row['pending_purchase_links'][0]['reason'])

    def test_search_and_detail_offer_direct_product_before_source_article(self):
        with tempfile.TemporaryDirectory() as temp,patch.object(db,'DB_PATH',Path(temp)/'ui.db'):
            db.initialize()
            direct='https://item.jd.com/123456.html?track=source'
            with db.connect() as c:
                source=c.execute('''INSERT INTO sources(platform,name,category,url,method,status,enabled)
                    VALUES('线报酷','公开线报','零售优惠','https://new.ixbk.net/push/json','rss','healthy',1)''').lastrowid
                event=c.execute('''INSERT INTO events(source_id,external_key,title,url,snippet,metadata_json,fingerprint,published_at)
                    VALUES(?,?,?,?,?,?,?,CURRENT_TIMESTAMP)''',(source,'post-1','纸巾限时优惠','https://new.ixbk.net/weibo/1.html',
                    '商品线索',json.dumps({'activity_links':[direct]}),'post-1')).lastrowid
                opportunity=c.execute('''INSERT INTO opportunities(event_id,source_id,title,category,url)
                    VALUES(?,?,?,?,?)''',(event,source,'纸巾限时优惠','零售优惠','https://new.ixbk.net/weibo/1.html')).lastrowid
            from app import app
            client=app.test_client()
            listing=BeautifulSoup(client.get('/?buy_platform=all&view=all').get_data(as_text=True),'html.parser')
            actions=listing.select_one('td[data-label="操作"]')
            self.assertIsNotNone(actions)
            self.assertEqual([a.get_text(' ',strip=True) for a in actions.select('a')],
                             ['打开商品 ↗','详情','来源原文 ↗'])
            self.assertEqual(actions.select_one('a.direct-product-link')['href'],'https://item.jd.com/123456.html')
            detail=BeautifulSoup(client.get(f'/opportunities/{opportunity}').get_data(as_text=True),'html.parser')
            direct_action=detail.select_one('.product-entry-actions a.direct-product-link')
            self.assertEqual(direct_action['href'],'https://item.jd.com/123456.html')
            self.assertIn('查看采集来源原文',detail.get_text(' ',strip=True))

    def test_worker_schedules_direct_merchant_product_page_probe(self):
        with tempfile.TemporaryDirectory() as temp,patch.object(db,'DB_PATH',Path(temp)/'direct.db'):
            db.initialize()
            now=datetime.now(timezone.utc).replace(tzinfo=None)
            direct='https://item.jd.com/123456.html'
            with db.connect() as c:
                source_id=c.execute('''INSERT INTO sources(platform,name,category,url,method,enabled)
                    VALUES('京东','test','test','https://example.com/feed','rss',1)''').lastrowid
                event_id=c.execute('''INSERT INTO events(source_id,external_key,title,url,metadata_json,fingerprint,published_at)
                    VALUES(?,?,?,?,?,?,?)''',(source_id,'direct','商品线索','https://example.com/post',
                    '{}','direct',now.strftime('%Y-%m-%d %H:%M:%S'))).lastrowid
                c.execute('INSERT INTO opportunities(event_id,source_id,title,category,url) VALUES(?,?,?,?,?)',
                          (event_id,source_id,'商品线索','test',direct))
            page=dict(state='public_price',checked_at=now.strftime('%Y-%m-%d %H:%M:%S'),
                      next_check_at='2099-01-01 00:00:00',title='真实商品页标题',advertised_cents=1990)
            with patch('link_resolution.inspect_product_page',return_value=page) as probe:
                self.assertEqual(run_cycle(limit=1),{'resolved':1,'product_page_public_price':1})
                self.assertEqual(probe.call_args.args[0],direct)
            with db.connect() as c:
                saved=c.execute('SELECT target_url,state FROM link_resolutions WHERE url=?',(direct,)).fetchone()
            self.assertEqual(tuple(saved),(direct,'resolved'))

    def test_worker_cache_prevents_repeat_requests(self):
        with tempfile.TemporaryDirectory() as temp,patch.object(db,'DB_PATH',Path(temp)/'test.db'):
            db.initialize()
            product_page=dict(state='public_price',checked_at='2026-10-06 00:00:00',next_check_at='2099-01-01 00:00:00',
                              title='纸巾',product_identity='jd:123',advertised_cents=1234,currency='CNY',
                              specification='',price_evidence='schema.org Product/Offer',reason='公开标价')
            with patch('link_resolution.resolve',return_value=dict(state='resolved',target_url='https://item.jd.com/123.html',reason='test')) as resolver, \
                 patch('link_resolution.inspect_product_page',return_value=product_page) as probe:
                self.assertEqual(run_cycle(urls=['https://u.jd.com/Abc']),{'resolved':1,'product_page_public_price':1})
                self.assertEqual(run_cycle(urls=['https://u.jd.com/Abc']),{});self.assertEqual(resolver.call_count,1)
                self.assertEqual(probe.call_count,1)
                with db.connect() as c:
                    saved=json.loads(c.execute('select product_page_json from link_resolutions').fetchone()[0])
                self.assertEqual(saved['advertised_cents'],1234)

    def test_worker_resolves_oldest_fresh_links_before_newer_links(self):
        with tempfile.TemporaryDirectory() as temp,patch.object(db,'DB_PATH',Path(temp)/'queue.db'):
            db.initialize()
            now=datetime.now(timezone.utc).replace(tzinfo=None)
            older='https://u.jd.com/Old001';newer='https://u.jd.com/New002'
            with db.connect() as c:
                source_id=c.execute('''INSERT INTO sources(platform,name,category,url,method,enabled)
                    VALUES('京东','test','test','https://example.com/feed','rss',1)''').lastrowid
                for suffix,title,url,published in (
                    ('older','older item',older,now-timedelta(minutes=20)),
                    ('newer','newer item',newer,now-timedelta(minutes=1)),
                ):
                    event_id=c.execute('''INSERT INTO events(source_id,external_key,title,url,metadata_json,fingerprint,published_at)
                        VALUES(?,?,?,?,?,?,?)''',(source_id,suffix,title,url,json.dumps({'activity_links':[url]}),suffix,published.strftime('%Y-%m-%d %H:%M:%S'))).lastrowid
                    c.execute('''INSERT INTO opportunities(event_id,source_id,title,category,url)
                        VALUES(?,?,?,?,?)''',(event_id,source_id,title,'test',url))
            calls=[]
            def unresolved(url):
                calls.append(url)
                return dict(state='unresolved',target_url=None,reason='test')
            with patch('link_resolution.resolve',side_effect=unresolved):
                self.assertEqual(run_cycle(limit=1),{'unresolved':1})
            self.assertEqual(calls,[older])

    def test_worker_does_not_probe_cached_pages_from_paused_sources(self):
        with tempfile.TemporaryDirectory() as temp,patch.object(db,'DB_PATH',Path(temp)/'paused.db'):
            db.initialize()
            now=datetime.now(timezone.utc)
            stamp=lambda value:value.strftime('%Y-%m-%d %H:%M:%S')
            source='https://u.jd.com/Paused001';target='https://item.jd.com/987654321.html'
            with db.connect() as c:
                source_id=c.execute('''INSERT INTO sources(platform,name,category,url,method,enabled,status)
                    VALUES('已暂停来源','test','test','https://aggregator.example/feed','rss',0,'healthy')''').lastrowid
                event_id=c.execute('''INSERT INTO events(source_id,external_key,title,url,metadata_json,fingerprint,published_at)
                    VALUES(?,?,?,?,?,?,?)''',(source_id,'paused','历史商品','https://aggregator.example/post',
                    json.dumps({'activity_links':[source]}),'paused',stamp(now))).lastrowid
                c.execute('INSERT INTO opportunities(event_id,source_id,title,category,url) VALUES(?,?,?,?,?)',
                          (event_id,source_id,'历史商品','test','https://aggregator.example/post'))
                c.execute('''INSERT INTO link_resolutions(url,target_url,state,reason,checked_at,next_check_at)
                    VALUES(?,?,'resolved','cached',?,?)''',(source,target,stamp(now-timedelta(hours=1)),
                    stamp(now+timedelta(hours=1))))
            with patch('link_resolution.inspect_product_page') as probe:
                self.assertEqual(run_cycle(),{})
                probe.assert_not_called()

    def test_additive_page_evidence_migration_preserves_existing_resolution_rows(self):
        with tempfile.TemporaryDirectory() as temp,patch.object(db,'DB_PATH',Path(temp)/'legacy.db'):
            with sqlite3.connect(db.DB_PATH) as c:
                c.execute('''CREATE TABLE link_resolutions(
                    url TEXT PRIMARY KEY,target_url TEXT,state TEXT NOT NULL,reason TEXT NOT NULL DEFAULT '',
                    checked_at TEXT NOT NULL,next_check_at TEXT NOT NULL)''')
                c.execute("INSERT INTO link_resolutions VALUES('https://u.jd.com/abc','https://item.jd.com/1.html','resolved','old','2026-10-06 00:00:00','2099-01-01 00:00:00')")
            db.ensure_link_resolution_product_page_column()
            with db.connect() as c:
                row=c.execute('select url,target_url,state,reason,product_page_json from link_resolutions').fetchone()
                self.assertEqual(tuple(row),('https://u.jd.com/abc','https://item.jd.com/1.html','resolved','old','{}'))

    def test_probe_stores_public_price_as_separate_non_quote_evidence(self):
        with tempfile.TemporaryDirectory() as temp,patch.object(db,'DB_PATH',Path(temp)/'probe.db'):
            db.initialize()
            source='https://u.jd.com/Abc'; target='https://item.jd.com/123.html'
            now=datetime.now(timezone.utc); fmt=lambda value:value.strftime('%Y-%m-%d %H:%M:%S')
            with db.connect() as c:
                c.execute('''INSERT INTO link_resolutions(url,target_url,state,reason,checked_at,next_check_at)
                    VALUES(?,?,'resolved','redirect ok',?,?)''',(source,target,fmt(now-timedelta(minutes=1)),fmt(now+timedelta(hours=12))))
            page=dict(state='public_price',checked_at=fmt(now),next_check_at=fmt(now+timedelta(hours=6)),
                      title='商品',product_identity='jd:123',advertised_cents=990,currency='CNY',
                      specification='',price_evidence='Product/Offer',reason='公开标价')
            with patch('link_resolution.inspect_product_page',return_value=page):
                self.assertEqual(probe_resolved_pages(source_urls=[source]),{'public_price':1})
            with db.connect() as c:
                cache={row['url']:dict(row) for row in c.execute('select * from link_resolutions')}
                self.assertEqual(c.execute('select count(*) from quotes').fetchone()[0],0)
            row=enrich(dict(id=1,title='商品',metadata_json=json.dumps({'activity_links':[source]})),cache)
            evidence=json.loads(row['metadata_json'])['resolved_link_evidence'][0]['product_page']
            self.assertEqual(evidence['state'],'public_price')
            self.assertTrue(evidence['current'])
            self.assertEqual(evidence['advertised_cents'],990)
