import json
import unittest
from datetime import datetime, timezone

import autoreview
from comparison import merchant_identity
from scanner import extract_html
from services.apple_catalog import parse_listing_card, parse_product_detail, supports_product_url

from scenario_groups import grouped_scenarios

@grouped_scenarios({
    'test_product_page_price_and_observation_evidence': (
        'exact_public_rmb_price_excludes_monthly_financing',
        'catalog_review_uses_source_observation_time_and_exact_price',
    ),
    'test_product_url_allowlist_and_identity': (
        'requires_exact_allowlisted_product_page',
        'apple_catalog_url_is_exact_product_identity',
    ),
    'test_listing_card_is_product_bound_and_fails_closed': (
        'listing_card_binds_one_public_price_to_its_exact_product_link',
        'listing_card_fails_closed_on_ambiguous_prices_or_sibling_products',
    ),
})
class AppleCatalogTests(unittest.TestCase):
    url = 'https://www.apple.com.cn/shop/product/fhfa4ch/a'
    page = '''<html><h1>翻新 MacBook Neo (Apple A18 Pro 芯片) - 银色</h1>
      <div class="rf-pdp-price"><div class="rf-pdp-currentprice">RMB 4,699</div>
      <div class="rc-installments">RMB 196/月 (24 期)</div></div>
      <script>{"partNumber":"FHFA4CH/A"}</script></html>'''

    def _case_exact_public_rmb_price_excludes_monthly_financing(self):
        detail = parse_product_detail(self.url, self.page)
        self.assertEqual(detail['advertised_cents'], 469900)
        self.assertEqual(detail['currency'], 'CNY')
        self.assertEqual(detail['specification'], 'Apple 商品编号 FHFA4CH/A')
        self.assertIsNone(detail['published_at'])
        self.assertIn('不是账号结算价', detail['evidence'])

    def _case_requires_exact_allowlisted_product_page(self):
        self.assertTrue(supports_product_url(self.url))
        for url in (
            'https://apple.com.cn/shop/product/fhfa4ch/a',
            'https://www.apple.com.cn.evil.example/shop/product/fhfa4ch/a',
            'https://www.apple.com.cn/shop/product/fhfa4ch/a?redirect=1',
            'http://www.apple.com.cn/shop/product/fhfa4ch/a',
        ):
            self.assertFalse(supports_product_url(url), url)
        self.assertFalse(autoreview.supported('https://www.apple.com.cn/shop/product/fhfa4ch/a?redirect=1'))
        self.assertTrue(autoreview.supported(self.url))

    def test_fails_closed_on_missing_ambiguous_or_wrong_sku_price(self):
        cases = (
            self.page.replace('RMB 4,699', '¥4,699'),
            self.page.replace('FHFA4CH/A', 'OTHER1CH/A'),
            self.page.replace('RMB 4,699', 'RMB 4,699/月'),
            self.page.replace('RMB 4,699', 'RMB 4,699</div><div class="rf-pdp-currentprice">RMB 4,599'),
        )
        for page in cases:
            with self.subTest(page=page[-80:]), self.assertRaises(ValueError):
                parse_product_detail(self.url, page)

    def _case_listing_card_binds_one_public_price_to_its_exact_product_link(self):
        listing_url = 'https://www.apple.com.cn/shop/refurbished/mac'
        title = '翻新 MacBook Neo (Apple A18 Pro 芯片) - 银色'
        html = f'''<ul><li><h3><a href="/shop/product/fhfa4ch/a?fnode=x">{title}</a></h3>
          <span>RMB 4,699</span></li></ul>'''
        rows = extract_html('apple', listing_url, html)
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]), 5)
        self.assertEqual(rows[0][1], self.url)
        self.assertEqual(rows[0][2], title + ' RMB 4,699')
        observation = rows[0][4]['catalog_observation']
        self.assertEqual(observation['advertised_cents'], 469900)
        self.assertEqual(observation['product_identity'], 'apple:FHFA4CH/A')
        self.assertEqual(observation['evidence_kind'], 'apple_catalog_card')

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        stamp = autoreview.stamp(now)
        row = dict(title=title, url=self.url, snippet=rows[0][2], status='pending',
                   source_parser='apple', metadata_json=json.dumps(rows[0][4]),
                   published_at=None, last_seen_at=stamp, last_success=stamp, enabled=1,
                   source_status='healthy', interval_minutes=60, detail_checked_at=None,
                   detail_json=json.dumps(observation, ensure_ascii=False))
        review = autoreview.classify(row, now)
        self.assertEqual(review['state'], 'observed')
        self.assertEqual(review['advertised_cents'], 469900)
        self.assertNotIn('排队', review['reason'])

    def _case_listing_card_fails_closed_on_ambiguous_prices_or_sibling_products(self):
        title = '翻新 MacBook Neo (Apple A18 Pro 芯片) - 银色'
        card_url = self.url
        for card in (
            title + ' RMB 4,699 RMB 5,399',
            '另一个商品 RMB 4,699',
            title + ' ¥4,699',
        ):
            with self.subTest(card=card), self.assertRaises(ValueError):
                parse_listing_card(card_url, title, card,
                                   'https://www.apple.com.cn/shop/refurbished/mac')

    def _case_catalog_review_uses_source_observation_time_and_exact_price(self):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        detail = parse_product_detail(self.url, self.page)
        stamp = autoreview.stamp(now)
        row = dict(title=detail['title'], url=self.url, snippet='', status='pending',
                   source_parser='apple', published_at=None, last_seen_at=stamp,
                   last_success=stamp, enabled=1, source_status='healthy',
                   interval_minutes=60, detail_checked_at=stamp,
                   detail_json=json.dumps(detail, ensure_ascii=False))
        review = autoreview.classify(row, now)
        self.assertEqual(review['state'], 'observed')
        self.assertEqual(review['advertised_cents'], 469900)
        self.assertEqual(review['specification'], 'Apple 商品编号 FHFA4CH/A')

    def _case_apple_catalog_url_is_exact_product_identity(self):
        row = dict(id=1, title='翻新 MacBook Neo', url=self.url, metadata_json='{}', detail_json='{}')
        identity = merchant_identity(row)
        self.assertEqual(identity['key'], 'apple:FHFA4CH/A')
        self.assertIn('Apple 中国商品ID', identity['label'])

if __name__ == '__main__':
    unittest.main()
