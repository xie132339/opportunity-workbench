import json
import unittest
from datetime import datetime
from unittest.mock import patch

import autoreview as ar
from pricing import discount_audit
from xianbao import extract_links
import test_xianbao as xb
sample = xb.sample
from xianbao import parse_items
import scanner
import db
from app import app

BODY = '京东app搜索 商品标题或关键字，部分有限时补贴3券(概率)，要加购领券\n秒杀会场找到此商品专享价加购，(直达)：\nhttps://u.jd.com/FGLrYPy\n百家好世 扫把簸箕套装组合，再砸落5券，2.4\nhttps://u.jd.com/FGLytTW'
HTML = '<article class="art-main"><h1 class="art-title">扫把套装 2.4元</h1><time class="time" title="2026-10-04 10:02:02"></time><div class="article-content">'+BODY+'</div><div class="art-copyright"><strong class="addr">原文地址：</strong><a href="https://m.weibo.cn/detail/5350236814843988">原帖</a></div></article><div class="comment">实付0元</div>'

class DiscountAuditTests(unittest.TestCase):
    def test_probability_never_reverse_calculates_base(self):
        a=discount_audit('扫把2.4元',BODY)
        self.assertIsNone(a['base_cents']);self.assertNotIn('verified',a)
        self.assertIn('概率优惠，不能保证领到',a['risks'])
        self.assertIn('补贴3券',a['coupons']);self.assertIn('砸落5券',a['coupons'])
        self.assertEqual(a['formula']['state'],'missing')
        self.assertTrue(any('会场' in s for s in a['steps']))
        self.assertTrue(any('运费' in s for s in a['gaps']))

    def test_explicit_equation_only_checks_arithmetic(self):
        a=discount_audit('扫把2.4元','商品面价10.4元 购买1件。价格计算：10.4元-3元-5元=2.4元。运费另计')
        self.assertEqual(a['formula']['calculated_cents'],240)
        self.assertEqual(a['formula']['state'],'consistent');self.assertNotIn('verified',a)
        self.assertEqual(a['base_cents'],1040);self.assertEqual(a['quantity'],1)
        self.assertEqual(discount_audit('','价格计算：18.9-3-5=2.4元')['formula']['state'],'conflict')
        self.assertEqual(discount_audit('','价格计算：2-3=-1元')['formula']['state'],'missing')
        self.assertEqual(discount_audit('','满100减20券，8.5折，到手65元')['formula']['state'],'missing')

    def test_multiple_qualifications_no_single_formula(self):
        a=discount_audit('新人会员价','价格计算：10-3=7元。价格计算：10-5=5元')
        self.assertEqual(a['formula']['state'],'missing')
        self.assertIn('限新客或首单',a['risks'])
        self.assertIn('需要会员或特定权益',a['risks'])

    def test_plain_links_and_article_only(self):
        links=extract_links('<p>https://u.jd.com/FGLrYPy</p><a href="https://u.jd.com/FGLrYPy">重复</a><a href="javascript:alert(1)">x</a>')
        self.assertEqual(links,['https://u.jd.com/FGLrYPy'])
        detail=ar.parse_xianbao_detail(HTML)
        self.assertEqual(len(detail['activity_links']),2)
        self.assertNotIn('实付0元',detail['conditions'])
        self.assertEqual(detail['published_at'],'2026-10-04 02:02:02')
        self.assertIn('weibo.cn',detail['origin_url'])
        with self.assertRaises(ValueError):ar.parse_xianbao_detail('<h1>请登录</h1>')

    def test_conflicting_equation_isolated(self):
        now=datetime(2026,10,4,2,10)
        r=dict(title='商品2.4元',url='https://example.com/a',snippet='价格计算：18.9-3-5=2.4元',
               status='pending',published_at=ar.stamp(now),last_seen_at=ar.stamp(now),last_success=ar.stamp(now),
               enabled=1,source_status='healthy',interval_minutes=10)
        self.assertEqual(ar.classify(r,now)['state'],'conflict')

class DetailPipelineTests(unittest.TestCase):
    setUp = xb.XianbaoTests.setUp
    tearDown = xb.XianbaoTests.tearDown
    # Reuse only the isolated database fixture, not inherited test methods.
    def test_targeted_auto_probe_and_visible_conditions(self):
        item=sample();item.update(title='扫把套装2.4元',content=BODY)
        with patch('scanner.xianbao_rows',return_value=parse_items([item])):scanner.scan_source(1)
        current_html = HTML.replace('2026-10-04 10:02:02',datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        with patch('scanner._fetch',return_value=current_html):
            result=ar.run_cycle(opportunity_id=1)
        self.assertEqual(result['probed'],1)
        with db.connect() as c:
            review=c.execute('SELECT * FROM auto_reviews WHERE opportunity_id=1').fetchone()
            self.assertIsNotNone(review['detail_checked_at'])
            self.assertEqual(review['state'],'conditional')
            self.assertEqual(len(json.loads(review['detail_json'])['activity_links']),2)
            self.assertEqual(c.execute('SELECT count(*) FROM buy_checks').fetchone()[0],0)
        with app.test_client() as client:
            text=client.get('/opportunities/1').get_data(as_text=True)
            self.assertIn('概率优惠，不能保证领到',text)
            self.assertIn('无法复算',text)
            self.assertIn('https://u.jd.com/FGLytTW',text)
            self.assertIn('来源优惠条件试算',text)
        # A new article fetch must not refresh the time on an older merchant observation.
        with db.connect() as c:
            saved=json.loads(c.execute('SELECT detail_json FROM auto_reviews WHERE opportunity_id=1').fetchone()[0])
            saved['external_checks']=[{'checked_at':'2026-10-01 01:00:00','price_cents':1890}]
            c.execute("UPDATE auto_reviews SET detail_json=?,detail_checked_at=datetime('now','-31 minutes'),next_check_at=NULL WHERE opportunity_id=1",(json.dumps(saved),))
        with patch('scanner._fetch',return_value=current_html):
            self.assertEqual(ar.run_cycle(opportunity_id=1)['probed'],1)
        with db.connect() as c:
            saved=json.loads(c.execute('SELECT detail_json FROM auto_reviews WHERE opportunity_id=1').fetchone()[0])
            self.assertEqual(saved['external_checks'][0]['checked_at'],'2026-10-01 01:00:00')

