import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone, timedelta

import db
from merchant_verification import check_page, extract_page, run_cycle, summarize


class MerchantVerificationTests(unittest.TestCase):
    def test_reads_single_explicit_cny_jsonld_offer(self):
        page = '''<html><title>测试商品</title><script type="application/ld+json">
        {"@type":"Product","name":"测试商品","offers":{"@type":"Offer","price":"12.30","priceCurrency":"CNY","availability":"https://schema.org/InStock"}}
        </script></html>'''
        result = extract_page(page)
        self.assertEqual(result['state'], 'public_price_observed')
        self.assertEqual(result['price_cents'], 1230)
        self.assertIn('未提供账号结算', result['reason'])

    def test_dynamic_missing_currency_or_multiple_prices_do_not_verify(self):
        fixtures = [
            '<meta property="og:price:amount" content="12.30">',
            '<script type="application/ld+json">{"@type":"Product","offers":[{"price":"10","priceCurrency":"CNY"},{"price":"11","priceCurrency":"CNY"}]}</script>',
            '<script type="application/ld+json">{"@type":"Product","offers":{"price":"10","priceCurrency":"USD"}}</script>',
        ]
        states = [extract_page(page)['state'] for page in fixtures]
        self.assertEqual(states, ['unsupported_currency', 'ambiguous', 'unsupported_currency'])

    def test_login_or_challenge_html_is_not_a_read_product_page(self):
        for title in ('登录 - 京东', '安全验证 | 天猫', '请先登录 - 淘宝'):
            page = '<html><title>' + title + '</title></html>'
            result = extract_page(page)
            self.assertEqual(result['state'], 'access_gate')
            self.assertIn('未读取到商品报价', result['reason'])
        self.assertEqual(extract_page('<title>商品详情</title>')['state'], 'page_read_no_price')

    def test_same_price_with_different_stock_markers_is_not_a_price_conflict(self):
        page = '''<script type="application/ld+json">{"@type":"Product","offers":[
          {"price":"19.90","priceCurrency":"CNY","availability":"InStock"},
          {"price":"19.90","priceCurrency":"CNY","availability":"OutOfStock"}
        ]}</script>'''
        result = extract_page(page)
        self.assertEqual(result['state'], 'public_price_observed')
        self.assertEqual(result['price_cents'], 1990)
        self.assertEqual(result['availability'], '')

    def test_page_probe_only_accepts_canonical_supported_target_and_no_redirects(self):
        with patch('merchant_verification.requests.get') as get:
            unsafe = check_page('https://evil.example/product/1')
            get.assert_not_called()
        self.assertEqual(unsafe['state'], 'unsupported_url')

        response = MagicMock(status_code=302, headers={'Location': 'https://127.0.0.1/private'},
                             encoding='utf-8', text='')
        response.__enter__.return_value = response
        with patch('merchant_verification.requests.get', return_value=response) as get:
            result = check_page('https://item.jd.com/123456.html')
        self.assertEqual(result['state'], 'redirect')
        self.assertFalse(get.call_args.kwargs['allow_redirects'])
        self.assertNotIn('cookies', get.call_args.kwargs)

    def test_cycle_persists_latest_observation_with_ttl(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(db, 'DB_PATH', Path(temp) / 'test.db'):
            db.initialize()
            now = datetime.now(timezone.utc)
            checked = (now - timedelta(seconds=5)).strftime('%Y-%m-%d %H:%M:%S')
            next_check = (now + timedelta(hours=1)).strftime('%Y-%m-%d %H:%M:%S')
            short = 'https://u.jd.com/Abc123'
            product = 'https://item.jd.com/123456.html'
            with db.connect() as conn:
                source = conn.execute("INSERT INTO sources(platform,name,category,url,method,status,last_success) VALUES('测试','测试','零售优惠','https://example.com/feed','rss','healthy',CURRENT_TIMESTAMP)").lastrowid
                event = conn.execute("INSERT INTO events(source_id,external_key,title,url,metadata_json,fingerprint,published_at) VALUES(?,?,?,?,?,?,datetime('now'))",
                                     (source, 'x', '测试商品', 'https://example.com/post', json.dumps({'activity_links':[short]}), 'f')).lastrowid
                opportunity = conn.execute("INSERT INTO opportunities(event_id,source_id,title,category) VALUES(?,?,?,'零售优惠')", (event, source, '测试商品')).lastrowid
                conn.execute("INSERT INTO link_resolutions(url,target_url,state,checked_at,next_check_at) VALUES(?,?,'resolved',?,?)", (short, product, checked, next_check))
            result = dict(state='public_price_observed', reason='test', page_title='商品', price_cents=1230, currency='CNY', availability='')
            with patch('merchant_verification.check_page', return_value=result):
                self.assertEqual(run_cycle(), {'public_price_observed': 1})
            with db.connect() as conn:
                observation = conn.execute('SELECT * FROM public_price_observations WHERE opportunity_id=?',
                                           (opportunity,)).fetchone()
            self.assertEqual(observation['price_cents'], 1230)
            self.assertEqual(observation['product_url'], product)
            with db.connect() as conn:
                saved = dict(conn.execute('SELECT * FROM merchant_page_checks WHERE opportunity_id=?', (opportunity,)).fetchone())
            self.assertEqual(saved['price_cents'], 1230)
            self.assertEqual(summarize({'id':opportunity,'metadata_json':json.dumps({'activity_links':[short], 'resolved_link_evidence':[{'state':'resolved','source_url':short,'target_url':saved['product_url'],'checked_at':checked,'next_check_at':next_check}]}),'detail_json':'{}'}, {(opportunity,saved['product_url']):saved})['state'], 'public_price_observed')
            with patch('merchant_verification.check_page') as checker:
                self.assertEqual(run_cycle(), {})
                checker.assert_not_called()

    def test_successful_product_page_keeps_sampling_after_feed_post_expires(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(db, 'DB_PATH', Path(temp) / 'test.db'):
            db.initialize()
            product = 'https://item.jd.com/123456.html'
            with db.connect() as conn:
                source = conn.execute("INSERT INTO sources(platform,name,category,url,method,enabled,status) VALUES('测试','测试','零售优惠','https://example.com/feed','rss',0,'paused')").lastrowid
                event = conn.execute("INSERT INTO events(source_id,external_key,title,url,fingerprint,published_at) VALUES(?,?,?,?,?,datetime('now','-1 day'))",
                                     (source, 'x', '过期帖子商品', 'https://example.com/post', 'old')).lastrowid
                opportunity = conn.execute("INSERT INTO opportunities(event_id,source_id,title,category,status) VALUES(?,?,?,'零售优惠','pending')",
                                           (event, source, '过期帖子商品')).lastrowid
                conn.execute("""INSERT INTO merchant_page_checks
                    (opportunity_id,product_url,state,price_cents,currency,checked_at,next_check_at)
                    VALUES(?,?,'public_price_observed',1000,'CNY',datetime('now','-10 minutes'),datetime('now','-1 minute'))""",
                             (opportunity, product))
            result = dict(state='public_price_observed', reason='test', page_title='商品',
                          price_cents=900, currency='CNY', availability='')
            with patch('merchant_verification.check_page', return_value=result) as checker:
                self.assertEqual(run_cycle(limit=1), {'public_price_observed': 1})
            checker.assert_called_once_with(product)
            with db.connect() as conn:
                history = conn.execute('SELECT COUNT(*) FROM public_price_observations WHERE product_url=?',
                                       (product,)).fetchone()[0]
            self.assertEqual(history, 1)
            with db.connect() as conn:
                conn.execute("UPDATE merchant_page_checks SET next_check_at=datetime('now','-1 minute') WHERE opportunity_id=?",
                             (opportunity,))
            updated = dict(result, price_cents=800)
            with patch('merchant_verification.check_page', return_value=updated):
                self.assertEqual(run_cycle(limit=1), {'public_price_observed': 1})
            with db.connect() as conn:
                samples = conn.execute('SELECT COUNT(*),MAX(price_cents) FROM public_price_observations WHERE product_url=?',
                                       (product,)).fetchone()
            self.assertEqual(tuple(samples), (1, 800))


if __name__ == '__main__':
    unittest.main()
