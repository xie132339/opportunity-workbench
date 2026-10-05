import json,unittest,tempfile
from pathlib import Path
from unittest.mock import patch,MagicMock
from datetime import datetime,timezone,timedelta
import requests,db
from link_resolution import supported,intermediate_hop,canonical_target,login_return_target,resolve,enrich,run_cycle
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
            'https://p.pinduoduo.com/AbC123':'https://mobile.yangkeduo.com/goods.html?goods_id=456',
        }
        for source,target in targets.items():
            with patch('link_resolution.requests.get',return_value=self.response(302,location=target)):
                self.assertEqual(resolve(source)['target_url'],target)
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
    def test_worker_cache_prevents_repeat_requests(self):
        with tempfile.TemporaryDirectory() as temp,patch.object(db,'DB_PATH',Path(temp)/'test.db'):
            db.initialize()
            with patch('link_resolution.resolve',return_value=dict(state='resolved',target_url='https://item.jd.com/123.html',reason='test')) as resolver:
                self.assertEqual(run_cycle(urls=['https://u.jd.com/Abc']),{'resolved':1})
                self.assertEqual(run_cycle(urls=['https://u.jd.com/Abc']),{});self.assertEqual(resolver.call_count,1)
