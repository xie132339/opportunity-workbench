import unittest
import json
from datetime import datetime

import autoreview as ar
from services.merchant_product_page import parse_product_page, supports_url
from services.product_search import listing_quote, quote_note
from scenario_groups import grouped_scenarios


@grouped_scenarios({
    'test_product_page_money_and_identity_boundaries': (
        'exact_public_offer_is_extracted_and_price_ranges_are_not',
        'microdata_offer_is_supported_but_bad_currency_or_stock_is_not',
        'url_allowlist_rejects_redirects_and_unconfigured_pages',
        'jsonld_recommendation_product_cannot_supply_this_page_price',
        'honor_estimated_checkout_claim_is_visible_but_not_a_public_quote',
        'suning_out_of_stock_page_does_not_read_stale_price_outside_main_block',
        'xiaomi_modern_product_urls_keep_product_identity_and_dedupe_tracking',
        'marketplace_product_ids_and_structured_prices_are_bound_to_exact_pages',
        'dynamic_marketplace_shells_and_other_skus_never_become_prices',
    ),
})
class MerchantProductPageTests(unittest.TestCase):
    url = 'https://product.suning.com/123/456.html'

    def _case_exact_public_offer_is_extracted_and_price_ranges_are_not(self):
        html = '''<h1>苏宁自营 抽纸商品</h1>
        <script type="application/ld+json">{"@context":"https://schema.org",
          "@type":"Product","@id":"https://product.suning.com/123/456.html",
          "name":"苏宁自营 抽纸商品","sku":"SKU-456","offers":{
          "@type":"Offer","price":"7.30","priceCurrency":"CNY",
          "availability":"https://schema.org/InStock"}}</script>'''
        result = parse_product_page(self.url, html)
        self.assertEqual(result['advertised_cents'], 730)
        self.assertEqual(result['product_identity'], 'SKU-456')
        self.assertEqual(result['price_evidence'], 'schema.org Product/Offer')
        self.assertIn('不是账号结算价', result['conditions'])

        ranged = html.replace('"@type":"Offer","price":"7.30"',
                              '"@type":"AggregateOffer","lowPrice":"7.30","highPrice":"9.90"')
        result = parse_product_page(self.url, ranged)
        self.assertIsNone(result['advertised_cents'])
        self.assertEqual(result['price_evidence'], '')

        meta_page = '''<head><title>苏宁自营 抽纸商品</title>
          <meta property="product:price:amount" content="7.30">
          <meta property="product:price:currency" content="CNY"></head>
          <body><h1>苏宁自营 抽纸商品</h1><aside>推荐商品 0.01元</aside></body>'''
        meta_result = parse_product_page(self.url, meta_page)
        self.assertEqual(meta_result['advertised_cents'], 730)
        self.assertEqual(meta_result['price_evidence'], 'Open Graph Product price metadata')
        self.assertIn('不是账号结算价', meta_result['conditions'])

    def _case_microdata_offer_is_supported_but_bad_currency_or_stock_is_not(self):
        html = '''<div itemscope itemtype="https://schema.org/Product">
          <h1 itemprop="name">联想键盘</h1><meta itemprop="sku" content="KB-1">
          <div itemprop="offers" itemscope itemtype="https://schema.org/Offer">
          <meta itemprop="price" content="199.00"><meta itemprop="priceCurrency" content="CNY">
          <link itemprop="availability" href="https://schema.org/InStock"></div></div>'''
        url = 'https://item.lenovo.com.cn/product/123.html'
        result = parse_product_page(url, html)
        self.assertEqual(result['advertised_cents'], 19900)

        for changed in (html.replace('CNY', 'USD'), html.replace('InStock', 'OutOfStock')):
            with self.subTest(changed=changed[-90:]):
                self.assertIsNone(parse_product_page(url, changed)['advertised_cents'])

    def _case_url_allowlist_rejects_redirects_and_unconfigured_pages(self):
        self.assertTrue(supports_url(self.url))
        self.assertTrue(supports_url('https://www.mi.com/shop/buy?product_id=22504'))
        for url in (
            'http://product.suning.com/123/456.html',
            'https://product.suning.com.evil/123/456.html',
            'https://product.suning.com/123/456.html?redirect=other',
            'https://www.mi.com/shop/buy?product_id=22504&redirect=other',
        ):
            self.assertFalse(supports_url(url), url)

    def _case_jsonld_recommendation_product_cannot_supply_this_page_price(self):
        html = '''<h1>本页商品</h1><script type="application/ld+json">[
          {"@type":"Product","url":"https://product.suning.com/999/000.html",
           "name":"推荐商品","offers":{"@type":"Offer","price":"0.01","priceCurrency":"CNY"}},
          {"@type":"Product","url":"https://product.suning.com/888/777.html",
           "name":"另一推荐商品","offers":{"@type":"Offer","price":"1.00","priceCurrency":"CNY"}}
        ]</script>'''
        result = parse_product_page(self.url, html)
        self.assertEqual(result['title'], '本页商品')
        self.assertIsNone(result['advertised_cents'])

    def _case_honor_estimated_checkout_claim_is_visible_but_not_a_public_quote(self):
        url = 'https://www.honor.com/cn/shop/product/10086983762557.html?cid=132368'
        html = '''<h1>荣耀 Earbuds 开放式耳机 极夜黑</h1>
          <span id="pro-price-hand">预估到手价</span>
          <input id="pro-price-hide" value="699.00">'''
        result = parse_product_page(url, html)
        self.assertEqual(result['page_amount_cents'], 69900)
        self.assertEqual(result['page_amount_label'], '荣耀商城页面预估到手价')
        self.assertIsNone(result['advertised_cents'])
        brief = ar.offer_summary(result['title'], url, result['conditions'],
                                 detail_json=json.dumps(result, ensure_ascii=False))
        self.assertEqual(brief['price_status']['state'], 'merchant_page_amount_unbound')
        self.assertEqual(brief['price_status']['page_amount_cents'], 69900)
        self.assertIsNone(brief.get('total_cents'))
        self.assertIsNone(listing_quote({'auto_state': 'conditional'}, brief))
        self.assertIn('不参与低价排序', quote_note(brief))

    def _case_suning_out_of_stock_page_does_not_read_stale_price_outside_main_block(self):
        html = '''<h1>抽纸 4提</h1>
          <div id="priceDom"><div class="price">此商品已下架</div><span id="mainPrice"></span></div>
          <div id="recommend"><span id="qg_qgprice">38.00</span></div>'''
        result = parse_product_page('https://product.suning.com/0000000000/12448937063.html', html)
        self.assertEqual(result['availability_status'], 'out_of_stock')
        self.assertIsNone(result['advertised_cents'])
        self.assertIn('拒绝使用主价区外', result['conditions'])
        row = dict(title=result['title'], url='https://product.suning.com/0000000000/12448937063.html',
                   snippet='', status='pending', published_at=None, source_parser='suning',
                   last_seen_at=ar.stamp(datetime(2026, 10, 4, 8)),
                   last_success=ar.stamp(datetime(2026, 10, 4, 8)), enabled=1,
                   source_status='healthy', interval_minutes=60,
                   detail_checked_at=ar.stamp(datetime(2026, 10, 4, 8)),
                   detail_json=json.dumps(result, ensure_ascii=False))
        classified = ar.classify(row, datetime(2026, 10, 4, 8))
        self.assertEqual(classified['state'], 'missing_price')
        self.assertIn('明确标记已下架', classified['reason'])

    def _case_xiaomi_modern_product_urls_keep_product_identity_and_dedupe_tracking(self):
        from scanner import extract_html, _allowed
        first = 'https://www.mi.com/shop/buy/detail?cfrom=web&product_id=10050236'
        second = 'https://www.mi.com/shop/buy?product_id=10050236&cfrom=home'
        self.assertTrue(supports_url(first))
        self.assertTrue(supports_url(second))
        self.assertFalse(supports_url(first + '&product_id=2'))
        self.assertFalse(_allowed('mi','https://www.mi.com:99999/shop/buy?product_id=10050236'))
        html = f'''<ul><li><a href="{first}">小米电视S Pro RGB-Mini LED 2027系列</a>
          <a href="{second}">查看商品</a><span>9299元起</span></li></ul>'''
        rows = extract_html('mi', 'https://www.mi.com/shop', html)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], 'https://www.mi.com/shop/buy/detail?product_id=10050236')
        self.assertIn('9299元起', rows[0][2])
        brief = ar.offer_summary(rows[0][0], rows[0][1], rows[0][2])
        self.assertEqual(brief['price_status']['state'], 'starting_price_unbound')
        self.assertEqual(brief['price_status']['headline_amount_cents'], 929900)
        self.assertIsNone(listing_quote({'catalog_listing': True, 'auto_state': 'missing_price'}, brief))

    def _case_marketplace_product_ids_and_structured_prices_are_bound_to_exact_pages(self):
        cases = (
            ('https://item.jd.com/123456.html', 'jd:123456'),
            ('https://item.taobao.com/item.htm?id=987654', 'taobao:987654'),
            ('https://detail.tmall.com/item.htm?id=987654', 'taobao:987654'),
            ('https://mobile.yangkeduo.com/goods.html?goods_id=456789', 'pdd:456789'),
            ('https://detail.vip.com/detail-123-456.html', 'vip:123:456'),
        )
        for url, identity in cases:
            with self.subTest(url=url):
                self.assertTrue(supports_url(url))
                self.assertFalse(supports_url(url + ('&tracking=1' if '?' in url else '?tracking=1')))
                product_url = url
                if '?' in product_url:
                    product_url += '&tracking=ignored'
                html = f'''<h1>当前商品</h1><script type="application/ld+json">{{
                  "@type":"Product","url":"{product_url}","name":"当前商品","sku":"seller-sku",
                  "offers":{{"@type":"Offer","price":"12.34","priceCurrency":"CNY",
                  "availability":"https://schema.org/InStock"}}}}</script>'''
                result = parse_product_page(url, html)
                self.assertEqual(result['advertised_cents'], 1234)
                self.assertEqual(result['product_identity'], identity)
                self.assertEqual(result['price_evidence'], 'schema.org Product/Offer')
        meta_url='https://mobile.yangkeduo.com/goods.html?goods_id=42'
        meta_page='''<head><meta property="og:url" content="https://mobile.yangkeduo.com/goods.html?goods_id=42">
          <meta property="og:title" content="纸巾 4包"><meta property="product:price:amount" content="8.80">
          <meta property="product:price:currency" content="CNY"></head>'''
        meta_result=parse_product_page(meta_url,meta_page)
        self.assertEqual(meta_result['advertised_cents'],880)
        self.assertEqual(meta_result['product_identity'],'pdd:42')

    def _case_dynamic_marketplace_shells_and_other_skus_never_become_prices(self):
        self.assertTrue(supports_url('https://item.jd.com/10213527436472.html'))
        self.assertTrue(supports_url('https://mobile.yangkeduo.com/goods.html?goods_id=638297466969'))
        self.assertFalse(supports_url('https://mobile.yangkeduo.com/goods.html?goods_id=42&tracking=1'))
        for url,html in (
            ('https://item.jd.com/10213527436472.html',
             '<title>京东(JD.COM)-正品低价、品质保障</title><main id="app"></main>'),
            ('https://mobile.yangkeduo.com/goods.html?goods_id=42',
             '<title>拼多多</title><main id="app"></main>'),
        ):
            with self.subTest(url=url),self.assertRaisesRegex(ValueError,'动态壳'):
                parse_product_page(url,html)
        wrong_product='''<h1>当前商品</h1><script type="application/ld+json">{
          "@type":"Product","url":"https://mobile.yangkeduo.com/goods.html?goods_id=41",
          "name":"推荐商品","offers":{"@type":"Offer","price":"0.01","priceCurrency":"CNY"}}</script>'''
        parsed=parse_product_page('https://mobile.yangkeduo.com/goods.html?goods_id=42',wrong_product)
        self.assertIsNone(parsed['advertised_cents'])


if __name__ == '__main__':
    unittest.main()
