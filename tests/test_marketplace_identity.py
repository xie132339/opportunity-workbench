import unittest

from services.marketplace_identity import (
    attach_product_marketplace,
    marketplace_matches_filter,
    marketplace_from_product_url,
)


class MarketplaceIdentityTests(unittest.TestCase):
    def test_only_exact_canonical_product_domains_identify_marketplace(self):
        self.assertEqual(
            marketplace_from_product_url("https://item.taobao.com/item.htm?id=123456789"),
            "taobao",
        )
        self.assertEqual(
            marketplace_from_product_url("https://h5.m.taobao.com/awp/core/detail.htm?id=123456789"),
            "taobao",
        )
        self.assertEqual(
            marketplace_from_product_url("https://m.taobao.com/awp/core/detail.htm?id=123456789"),
            "taobao",
        )
        self.assertEqual(
            marketplace_from_product_url("https://detail.tmall.com/item.htm?id=123456789"),
            "tmall",
        )
        self.assertIsNone(marketplace_from_product_url("https://item.taobao.com.evil.test/item.htm?id=123456789"))
        self.assertIsNone(marketplace_from_product_url("https://s.click.taobao.com/t?e=opaque"))
        self.assertIsNone(marketplace_from_product_url("http://item.taobao.com/item.htm?id=123456789"))
        self.assertIsNone(marketplace_from_product_url("https://item.taobao.com/item.htm?id=not-a-number"))

    def test_text_claims_and_mismatched_product_titles_do_not_identify_taobao(self):
        text_only = attach_product_marketplace({
            "title": "淘宝纸巾活动价 8 元",
            "product_links": [],
        })
        mismatched = attach_product_marketplace({
            "title": "淘宝纸巾活动价 8 元",
            "product_links": [{
                "target_url": "https://item.taobao.com/item.htm?id=123456789",
                "title_alignment": "different",
            }],
        })
        self.assertEqual(text_only["marketplace_key"], "unknown")
        self.assertEqual(mismatched["marketplace_key"], "unknown")
        self.assertIn("标题/正文提及", text_only["marketplace_evidence"])

    def test_multiple_merchant_targets_are_not_forced_into_one_platform(self):
        row = attach_product_marketplace({
            "product_links": [
                {"target_url": "https://item.taobao.com/item.htm?id=123456789"},
                {"target_url": "https://item.jd.com/123456.html"},
            ],
        })
        self.assertEqual(row["marketplace_key"], "multiple")

    def test_default_taobao_filter_includes_tmall_but_tmall_filter_stays_exact(self):
        self.assertTrue(marketplace_matches_filter("taobao", "taobao"))
        self.assertTrue(marketplace_matches_filter("tmall", "taobao"))
        self.assertFalse(marketplace_matches_filter("jd", "taobao"))
        self.assertTrue(marketplace_matches_filter("tmall", "tmall"))
        self.assertFalse(marketplace_matches_filter("taobao", "tmall"))


if __name__ == "__main__":
    unittest.main()
