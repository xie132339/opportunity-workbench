import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import db
import app as app_module
import scanner
import autoreview
from app import app
from offer import resource_kind, resource_topic
from xianbao import parse_items, validate_url, BASE


def sample():
    return dict(id=1,title='洗衣液2瓶 12元',url='/weibo/123.html',content='限新客，包邮',
                content_html='<p>条件</p><a href="https://item.jd.com/123.html">商品</a><script>bad</script>',
                catename='微博线报-日用-京东',shijianchuo=int(datetime.now(timezone.utc).timestamp()))


class XianbaoTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.patcher=patch.object(db,'DB_PATH',Path(self.temp.name)/'test.sqlite3')
        self.patcher.start()
        with db.connect() as c:
            c.executescript(db.SCHEMA)
            for i,path in [(1,'push'),(2,'push_10')]:
                c.execute("INSERT INTO sources(id,platform,name,category,url,method,interval_minutes) VALUES(?,'线报酷',?,'零售优惠',?,'xianbao',1)",
                          (i,path,BASE+'/plus/json/'+path+'.json'))

    def tearDown(self):
        self.patcher.stop();self.temp.cleanup()

    def test_exact_endpoint_allowlist(self):
        validate_url(BASE+'/plus/json/push.json')
        for url in [BASE+'/plus/json/push.json?redirect=x','https://evil.test/push.json','http://127.0.0.1/push.json']:
            with self.assertRaises(ValueError):validate_url(url)

    def test_original_time_metadata_and_links(self):
        row=parse_items([sample()])[0]
        self.assertIn('限新客',row[3]);self.assertIsNotNone(row[4])
        self.assertEqual(row[5]['source_category'],'微博线报-日用-京东')
        self.assertEqual(row[5]['activity_links'],['https://item.jd.com/123.html'])
        item=sample();item.pop('shijianchuo')
        self.assertIsNone(parse_items([item])[0][4])
        item['url']='https://evil.test/a';self.assertEqual(parse_items([item]),[])
        with self.assertRaises(ValueError):parse_items({'error':'login'})

    def test_mechanisms_not_all_free(self):
        cases={'0.01元咖啡':'purchase','免费领取纸巾':'free_claim','0元试用':'trial',
               '付邮领取礼品':'shipping','抽奖送手机':'lottery','积分兑换咖啡':'points',
               '返后0元电锅':'rebate','100元打车券包':'coupon'}
        for title,kind in cases.items():self.assertEqual(resource_kind(title),kind,title)
        self.assertEqual(resource_kind('电锅169元','晒反40到手169'),'rebate')
        self.assertEqual(resource_topic('洗衣液'),'home')

    def test_pipeline_dedup_filter_and_current_refresh(self):
        rows=parse_items([sample()])
        with patch('scanner.xianbao_rows',return_value=rows):
            self.assertEqual(scanner.scan_source(1)['new'],1)
            self.assertEqual(scanner.scan_source(1)['new'],0)
            self.assertEqual(scanner.scan_source(2)['new'],1)
        with db.connect() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM auto_reviews WHERE state='queued'").fetchone()[0],1)
            self.assertEqual(c.execute("SELECT count(*) FROM opportunities WHERE topic='home' AND resource_kind='purchase'").fetchone()[0],2)
            c.execute("UPDATE events SET last_seen_at=datetime('now','-1 minute') WHERE source_id=2")
        autoreview.review_all()
        with db.connect() as c:
            self.assertEqual(c.execute("SELECT state FROM auto_reviews WHERE opportunity_id=2").fetchone()[0],'excluded')
            self.assertEqual(c.execute('SELECT count(*) FROM buy_checks').fetchone()[0],0)
        with app.test_client() as client:
            current=client.get('/?topic=home&view=current').get_data(as_text=True)
            self.assertIn('/opportunities/1',current)
            self.assertIn('自动复查排队中',current)
            self.assertNotIn('洗衣液',client.get('/?topic=food').get_data(as_text=True))
            self.assertEqual(client.get('/opportunities/1').status_code,200)

    def test_personal_trade_ledger_is_removed_but_external_quotes_remain(self):
        with patch('scanner.xianbao_rows',return_value=parse_items([sample()])):
            scanner.scan_source(1)
        with db.connect() as c:
            c.execute("INSERT INTO quotes(opportunity_id,kind,amount_cents,evidence_url) VALUES(1,'listing',900,'https://example.com/market')")
            c.execute("INSERT INTO quotes(opportunity_id,kind,amount_cents) VALUES(1,'historical_buy',500)")
            c.execute("INSERT INTO trades(opportunity_id,state,quantity,buy_cents) VALUES(1,'holding',1,500)")

        with app.test_client() as client:
            detail=client.get('/opportunities/1')
            self.assertEqual(detail.status_code,200)
            page=detail.get_data(as_text=True)
            self.assertIn('公开行情依据',page)
            self.assertIn('外部挂牌价',page)
            self.assertIn('¥9.00',page)
            self.assertNotIn('历史个人实付',page)
            self.assertNotIn('买入、库存和结算',page)
            self.assertNotIn('保存交易',page)
            self.assertNotIn('交易与复盘',page)
            self.assertEqual(client.get('/market').status_code,200)
            for path in ['/trades','/trades/1/settle','/opportunities/1/trades','/opportunities/1/details']:
                self.assertEqual(client.get(path).status_code,404,path)

        with db.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM trades').fetchone()[0],1)
            self.assertEqual(c.execute("SELECT count(*) FROM quotes WHERE kind='historical_buy'").fetchone()[0],1)

    def test_coupon_face_value_not_cash_price(self):
        item=sample();item.update(title='100元打车立减券包',content='领取优惠券')
        with patch('scanner.xianbao_rows',return_value=parse_items([item])):scanner.scan_source(1)
        with db.connect() as c:
            review=c.execute('SELECT * FROM auto_reviews').fetchone()
            self.assertEqual(review['state'],'queued');self.assertIsNone(review['advertised_cents'])
        with app.test_client() as client:
            self.assertNotIn('/opportunities/1',client.get('/?resource=coupon').get_data(as_text=True))
            self.assertNotIn('100元打车立减券包',client.get('/?resource=free_claim').get_data(as_text=True))

    def test_scan_all_reclassifies_once_after_the_batch(self):
        with patch('scanner.scan_source', side_effect=lambda source_id, **kwargs:
                   dict(source_id=source_id, review=kwargs['review'])) as scan, \
             patch('autoreview.review_all') as review:
            results=scanner.scan_all()
        self.assertEqual([item['review'] for item in results],[False,False])
        self.assertEqual(scan.call_count,2)
        review.assert_called_once_with()

    def test_serving_application_does_not_reclassify_all_records(self):
        with patch.object(app_module,'initialize'), patch.object(app_module.app,'run'), \
             patch('autoreview.review_all') as review, patch.object(sys,'argv',['app.py','serve']):
            app_module.main()
        review.assert_not_called()

    def test_verification_list_shows_currently_parsed_spec_without_updating_snapshot(self):
        item=sample();item['title']='得宝迷你系列手帕纸5片*54小包 20元'
        with patch('scanner.xianbao_rows',return_value=parse_items([item])):
            scanner.scan_source(1)
        with db.connect() as c:
            c.execute("UPDATE auto_reviews SET specification='5片' WHERE opportunity_id=1")
        with app.test_client() as client:
            response=client.get('/verification?state=queued')
            page=response.get_data(as_text=True)
        self.assertEqual(response.status_code,200)
        self.assertIn('5片 × 54小包',page)
        self.assertIn('来源金额、状态与复查时间来自最近一次核验快照',page)
        self.assertIn('自动复查排队中',page)
        with db.connect() as c:
            row=c.execute('SELECT state,specification FROM auto_reviews WHERE opportunity_id=1').fetchone()
        self.assertEqual((row['state'],row['specification']),('queued','5片'))

    def test_source_form_requires_allowlisted_url(self):
        with app.test_client() as client:
            with client.session_transaction() as session:session['csrf']='x'
            client.post('/sources',data=dict(csrf='x',platform='线报酷',name='非法',category='零售优惠',
                                           method='xianbao',url='https://evil.test/data',interval_minutes='1'))
        with db.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM sources').fetchone()[0],2)

    def test_local_rsshub_bypasses_environment_proxy(self):
        with patch('scanner.requests.Session') as factory:
            session = factory.return_value.__enter__.return_value
            session.get.return_value.status_code = 200
            session.get.return_value.content = b'<rss version="2.0"><channel><title>x</title><item><title>paper</title><link>https://example.com/p/1</link></item></channel></rss>'
            rows = scanner._rss_rows('http://127.0.0.1:1200/test','http://127.0.0.1:1200')
            self.assertIs(session.trust_env, False)
            self.assertEqual(len(rows),1)
