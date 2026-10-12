"""Tests for the data-driven discount calibration (replaces hard-coded filters).

The old business thresholds (85% strikethrough drop, 20x markup, 50-yuan
absolute guard, per-topic price ceilings) are now estimated from the corpus:
- price ceiling        = median(log10 promotion) + 6*MAD(log10 promotion)
- per-bin ratio cap    = P97 of drop ratio in a promotion-price bin (clean tail)
- per-bin drop cap     = P97 of saved amount in a promotion-price bin (clean tail)

These tests verify the estimator, the seed fallback (same semantics as the
legacy constants), and that real fake-deal shapes (KUB/Chaihuo/YuanGu tissue,
rice, absurd-price detergent/machine) are caught while genuine premium goods
(low-deuterium water, camellia-oil gift set) are kept.
"""
import json
import math
import sqlite3
import tempfile
import unittest
from pathlib import Path

from services import discount_calibration as dc


def _memory_db():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(dc.CALIBRATION_SCHEMA)
    return connection


class CalibrationEstimatorTests(unittest.TestCase):
    def test_quantile_and_bin_mapping(self):
        values = list(range(100))
        self.assertEqual(dc._quantile(values, 0.5), 50)    # 上分位取法（含 50）
        self.assertEqual(dc._quantile(values, 0.97), 97)
        self.assertEqual(dc._bin_of(math.log10(5000)), (3.5, 4.0))   # ¥50
        self.assertEqual(dc._bin_of(math.log10(10000)), (4.0, 4.5))  # ¥100
        self.assertEqual(dc._bin_of(math.log10(100)), (2.0, 2.5))    # ¥1

    def test_calibrate_estimates_ceiling_and_caps(self):
        connection = _memory_db()
        connection.executescript("""
            CREATE TABLE sources(id INTEGER PRIMARY KEY, name TEXT, method TEXT);
            CREATE TABLE events(id INTEGER PRIMARY KEY, source_id INTEGER, metadata_json TEXT);
            CREATE TABLE opportunities(id INTEGER PRIMARY KEY, event_id INTEGER, source_id INTEGER);
        """)
        connection.execute("INSERT INTO sources VALUES(1,'淘宝店抽纸优惠搜索','taobao_alliance')")
        # 官方来源命名（纸品）也必须纳入同一主题校准
        connection.execute("INSERT INTO sources VALUES(2,'淘宝生活品·纸品优惠搜索','taobao_alliance')")
        # 200 条正常促销（promo 3-30 元，降幅 20%-60%）+ 4 条明显假货（>90%）
        rows = []
        for i in range(200):
            promo = 300 + (i % 30) * 100
            reserve = promo * 2 + (i % 5) * 100
            rows.append((1, json.dumps({"taobao_alliance_offer": {
                "promotion_price_cents": promo, "reserve_price_cents": reserve}})))
        for promo, reserve in ((299, 19900), (690, 69900), (7300, 69900), (18830, 101600)):
            rows.append((1, json.dumps({"taobao_alliance_offer": {
                "promotion_price_cents": promo, "reserve_price_cents": reserve}})))
        for i in range(50):
            promo = 500 + i * 100
            rows.append((2, json.dumps({"taobao_alliance_offer": {
                "promotion_price_cents": promo, "reserve_price_cents": promo * 2}})))
        connection.executemany("INSERT INTO events(source_id,metadata_json) VALUES(?,?)", rows)
        for i in range(1, len(rows) + 1):
            connection.execute("INSERT INTO opportunities(event_id,source_id) VALUES(?,?)", (i, 1 if i <= 204 else 2))
        calibration = dc.calibrate(connection)
        self.assertIn("paper", calibration)
        payload = calibration["paper"]
        self.assertGreater(payload["price_ceiling_cents"], 10000)      # 高于正常样本价（3-30 元）
        self.assertIsNotNone(payload["global_ratio_cap"])
        self.assertLess(payload["global_ratio_cap"], 0.92)             # 未被假货污染
        self.assertGreater(payload["sample_count"], 200)

    def test_judge_drop_uses_calibrated_bin_caps(self):
        connection = _memory_db()
        # 构造确定性的校准：paper 箱界 ratio 0.85 / drop ¥60，价格上界 ¥300
        dc.set_calibration({"paper": {
            "price_ceiling_cents": 30000,
            "bins": [(2.0, 2.5, 0.85, 30000), (2.5, 3.0, 0.85, 30000),
                     (3.0, 3.5, 0.85, 30000), (3.5, 4.0, 0.85, 30000)],
            "global_ratio_cap": 0.85, "global_drop_cap": 30000,
            "sample_count": 100, "calibrated_at": "2026-01-01T00:00:00+00:00"}})
        try:
            plausible, reason = dc.judge_drop("paper", 69900, 6990)   # KUB 90%
            self.assertFalse(plausible)
            self.assertIn("校准可信上界", reason)
            self.assertTrue(dc.judge_drop("paper", 10000, 1600)[0])   # 84% < 85% 界，放行
            self.assertTrue(dc.judge_drop("paper", 10000, 5000)[0])   # 5 折
            self.assertTrue(dc.judge_drop("paper", 4000, 1000)[0])    # drop 30 < 300，放行
            self.assertFalse(dc.judge_drop("paper", 50000, 10000)[0])  # drop 400 > 300，优惠额超界
            # 荒谬高价：促销价 > 校准上界
            self.assertFalse(dc.judge_drop("laundry_detergent", 299900, 299700)[0])
        finally:
            dc.set_calibration(None)

    def test_premium_bins_are_exempt_from_ratio_and_drop_caps(self):
        # 高价真品（>= 主题 P97 所在箱上界）不受降幅/优惠额界约束，只受荒谬高价上界约束
        dc.set_calibration({"water": {
            "price_ceiling_cents": 45700,
            "high_log": 4.5,   # ¥316 以上豁免
            "bins": [(3.5, 4.0, 0.78, 18800), (4.0, 4.5, 0.64, 27300)],
            "global_ratio_cap": 0.81, "global_drop_cap": 18000,
            "sample_count": 100, "calibrated_at": "2026-01-01T00:00:00+00:00"}})
        try:
            # 358 元低氘水 61% off：高于豁免线，放行
            self.assertTrue(dc.judge_drop("water", 108000, 35800)[0])
            # 37.1 元百岁山 87.6% off：低于豁免线，严格箱界拦
            self.assertFalse(dc.judge_drop("water", 30000, 3710)[0])
            # 500 元高端水 90% off：豁免线之上但荒谬高价上界 457 拦
            self.assertFalse(dc.judge_drop("water", 500000, 50000)[0])
            # 豁免区内仍受主题全局降幅界约束：400 元水 92% off（超出全局 0.81）拦
            self.assertFalse(dc.judge_drop("water", 500000, 40000)[0])
            # 豁免区内 70% off 正常放行
            self.assertTrue(dc.judge_drop("water", 133333, 40000)[0])
        finally:
            dc.set_calibration(None)

    def test_category_matches_blocks_look_alike_products(self):
        # 大米主题误命中的洗护/服饰商品：不得产生参考价差优惠额
        self.assertFalse(dc.category_matches("rice", "欧洲直邮Kiehl's科颜氏大米小麦丰盈洗发露5"))
        self.assertFalse(dc.category_matches("rice", "美国直邮reiss大米Fernando条纹罗纹上衣"))
        self.assertFalse(dc.category_matches("water", "雅漾活泉水爽肤水喷雾化妆水"))
        self.assertFalse(dc.category_matches("flour", "补水面膜粉底散粉爽身粉"))
        self.assertFalse(dc.category_matches("cooking_oil", "发动机机油润滑油5W-30"))
        # 真实品类商品全部放行
        self.assertTrue(dc.category_matches("rice", "2025新米东北五常大米10斤装粳米"))
        self.assertTrue(dc.category_matches("paper", "KUB可优比婴儿云柔巾乳霜保湿超柔抽纸80抽18包"))
        self.assertTrue(dc.category_matches("water", "康泽泉低氘小分子水350ml×12瓶"))
        self.assertTrue(dc.category_matches("cooking_oil", "赣江山茶油食用油2L礼盒纯正茶油"))
        self.assertTrue(dc.category_matches("laundry_detergent", "蓝月亮洗衣液3kg薰衣草香深层洁净"))
        # 未知主题（自定义来源）不设品类门槛
        self.assertTrue(dc.category_matches(None, "任意商品标题"))

    def test_seed_fallback_matches_legacy_rules(self):
        dc.set_calibration(None)
        try:
            from services.taobao_alliance import reference_drop_plausibility as plausible
            self.assertFalse(plausible(19900, 290)[0])       # 98.5% 降幅
            self.assertFalse(plausible(6490, 990)[0])        # <10 元且优惠>50 元
            self.assertFalse(plausible(10000, 300)[0])       # 33 倍参考价
            self.assertFalse(plausible(10000, 1500)[0])      # 恰 85%
            self.assertTrue(plausible(10000, 5000)[0])       # 5 折
            self.assertTrue(plausible(10000, 1600)[0])       # 84%
        finally:
            dc.set_calibration(None)

    def test_price_ceiling_seed_when_uncalibrated(self):
        dc.set_calibration(None)
        try:
            self.assertEqual(dc.price_ceiling_cents("paper"), 30000)
            self.assertIsNone(dc.price_ceiling_cents("custom-9"))
        finally:
            dc.set_calibration(None)

    def test_refresh_if_stale_recomputes(self):
        connection = _memory_db()
        connection.executescript("""
            CREATE TABLE sources(id INTEGER PRIMARY KEY, name TEXT, method TEXT);
            CREATE TABLE events(id INTEGER PRIMARY KEY, source_id INTEGER, metadata_json TEXT);
            CREATE TABLE opportunities(id INTEGER PRIMARY KEY, event_id INTEGER, source_id INTEGER);
        """)
        connection.execute("INSERT INTO sources VALUES(1,'淘宝店抽纸优惠搜索','taobao_alliance')")
        rows = [(1, json.dumps({"taobao_alliance_offer": {
            "promotion_price_cents": promo, "reserve_price_cents": reserve}}))
            for promo, reserve in ((5000, 10000), (3000, 10000), (7000, 10000),
                                   (8000, 10000), (9000, 10000))] * 8
        connection.executemany("INSERT INTO events(source_id,metadata_json) VALUES(?,?)", rows)
        for i in range(1, len(rows) + 1):
            connection.execute("INSERT INTO opportunities(event_id,source_id) VALUES(?,1)", (i,))
        dc.set_calibration(None)
        try:
            result = dc.refresh_if_stale(connection, max_age_seconds=0)
            self.assertIn("paper", result)
            self.assertIsNotNone(result["paper"]["global_ratio_cap"])
        finally:
            dc.set_calibration(None)


class CalibrationTopicKeyTests(unittest.TestCase):
    def test_topic_key_maps_managed_source_names(self):
        self.assertEqual(dc.topic_key("淘宝店抽纸优惠搜索"), "paper")
        self.assertEqual(dc.topic_key("淘宝生活品·纸品优惠搜索"), "paper")
        self.assertEqual(dc.topic_key("淘宝生活品·食用油优惠搜索"), "cooking_oil")
        self.assertEqual(dc.topic_key("淘宝生活品·洗衣液优惠搜索"), "laundry_detergent")
        self.assertIsNone(dc.topic_key("自定义生活品·员工福利"))
        self.assertIsNone(dc.topic_key(""))


if __name__ == "__main__":
    unittest.main()
