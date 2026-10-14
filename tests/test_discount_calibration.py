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
            # 84% 中低价且优惠额超售价（drop 8400 > 1600×1.05）→ 引流划线，硬规则拦（新语义）
            plausible, reason = dc.judge_drop("paper", 10000, 1600)
            self.assertFalse(plausible)
            self.assertIn("优惠额显著超过推广价", reason)
            self.assertTrue(dc.judge_drop("paper", 10000, 5000)[0])   # 5 折
            self.assertTrue(dc.judge_drop("paper", 2000, 1000)[0])    # drop 10 < 30，放行
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

    def test_trial_gating_marker_blocks_traffic_skus(self):
        # 尝鲜/首单/新客/试用等引流小规格 SKU：不得产生优惠额
        self.assertTrue(dc.has_trial_gating_marker("【尝鲜】百岁山天然矿泉水348ml2瓶便携小瓶装"))
        self.assertTrue(dc.has_trial_gating_marker("【新客专享】1大包抽纸整箱批发家用餐巾纸"))
        self.assertTrue(dc.has_trial_gating_marker("2.01元购400张大包抽纸擦手纸整箱批"))
        self.assertTrue(dc.has_trial_gating_marker("【u先试用】无染原木纯品抽纸家用整箱"))
        self.assertTrue(dc.has_trial_gating_marker("蓝月亮内衣专用洗衣液小样80g去渍除菌"))
        # 扩展引流表达：首购/试吃/随身装/便携/迷你/免费领/1分钱
        self.assertTrue(dc.has_trial_gating_marker("首购礼金 洗衣凝珠试用装3颗"))
        self.assertTrue(dc.has_trial_gating_marker("食用油500ml试吃装特级初榨"))
        self.assertTrue(dc.has_trial_gating_marker("矿泉水随身装便携小瓶350ml"))
        self.assertTrue(dc.has_trial_gating_marker("大米迷你装真空小袋300g"))
        self.assertTrue(dc.has_trial_gating_marker("免费领取 抽纸2包体验"))
        # 真实商品（即使带小规格/整箱）放行；数字子串（如 10.01 标题）不误伤
        self.assertFalse(dc.has_trial_gating_marker("山茶油5L茶油茶籽油低温冷榨食用油植物油"))
        self.assertFalse(dc.has_trial_gating_marker("2026新米柴火大院绿色特等五常大米5kg原粮稻花香2号"))
        self.assertFalse(dc.has_trial_gating_marker("百岁山天然矿泉水348ml*24瓶整箱批发价10.01元"))
        self.assertFalse(dc.has_trial_gating_marker(""))
        self.assertFalse(dc.has_trial_gating_marker(None))

    def test_market_band_fake_discount_blocks_deep_markup_at_market_price(self):
        # 深降幅划线 + 最终价落在市场正常价带（中位±MAD）= 划线虚高造优惠
        dc.set_calibration({"paper": {
            "price_ceiling_cents": 35600, "high_log": 3.0,
            "bins": [], "global_ratio_cap": 0.90, "global_drop_cap": 10000,
            "gated_prices": [], "low_price_floor": 400,
            "market_band_low": 401, "market_band_high": 1179,
            "deep_drop_threshold": 0.916, "sample_count": 100,
            "calibrated_at": "2026-01-01T00:00:00+00:00"}})
        try:
            # 深降幅 + 市场价 → 假优惠
            self.assertTrue(dc.is_market_band_fake_discount("paper", 850, 0.95))
            # 深降幅但价格显著低于市场带（真捡漏）→ 放行
            self.assertFalse(dc.is_market_band_fake_discount("paper", 200, 0.95))
            # 市场带内但降幅不深（量大正常折扣）→ 放行
            self.assertFalse(dc.is_market_band_fake_discount("paper", 790, 0.792))
            # 降幅不足 / 未知主题 / 非法入参
            self.assertFalse(dc.is_market_band_fake_discount("paper", 900, 0.5))
            self.assertFalse(dc.is_market_band_fake_discount(None, 850, 0.95))
            self.assertFalse(dc.is_market_band_fake_discount("paper", 0, 0.95))
        finally:
            dc.set_calibration(None)

    def test_gated_promo_prices_detects_unified_low_band_subsidies(self):
        # 低价带内精确同价（分）聚集 >= 5 条 → 引流统一价；中位价以上或低聚集不拦
        values = [201] * 8 + [501] * 4 + [1000] * 3 + [2000] * 2 + [3000, 4000]
        gated = dc._gated_promo_prices(values)
        self.assertIn(201, gated)
        self.assertNotIn(501, gated)   # 不足 5 条
        self.assertNotIn(2000, gated)  # 无聚集
        self.assertEqual(dc._gated_promo_prices([]), [])
        # 中位数以上不参与：全是同价 100 元也不 gated
        self.assertEqual(dc._gated_promo_prices([10000] * 12), [])

    def test_judge_drop_blocks_gated_subsidy_price(self):
        dc.set_calibration({"paper": {
            "price_ceiling_cents": 35600, "high_log": 3.0,
            "bins": [], "global_ratio_cap": 0.90, "global_drop_cap": 10000,
            "gated_prices": [201], "sample_count": 100,
            "calibrated_at": "2026-01-01T00:00:00+00:00"}})
        try:
            ok, reason = dc.judge_drop("paper", 990, 201)
            self.assertFalse(ok)
            self.assertIn("引流", reason)
            # 非 gated 价不受影响
            self.assertTrue(dc.judge_drop("paper", 990, 600)[0])   # drop 390 < 600×1.05，放行
        finally:
            dc.set_calibration(None)

    def test_low_value_small_item_floor_is_data_driven_and_drops_only_discount(self):
        # P15 推广价作为低价小件线：几块钱单包/单瓶不算"优惠力度大"
        dc.set_calibration({"paper": {
            "price_ceiling_cents": 35600, "high_log": 3.0,
            "bins": [], "global_ratio_cap": 0.90, "global_drop_cap": 10000,
            "gated_prices": [], "low_price_floor": 400, "sample_count": 100,
            "calibrated_at": "2026-01-01T00:00:00+00:00"}})
        try:
            self.assertTrue(dc.is_low_value_small_item("paper", 201))
            self.assertTrue(dc.is_low_value_small_item("paper", 399))
            self.assertFalse(dc.is_low_value_small_item("paper", 400))  # 恰在线上不拦
            self.assertFalse(dc.is_low_value_small_item("paper", 790))
            # 未知主题/无校准不拦
            self.assertFalse(dc.is_low_value_small_item(None, 201))
            self.assertFalse(dc.is_low_value_small_item("other", 201))
            self.assertFalse(dc.is_low_value_small_item("paper", 0))
        finally:
            dc.set_calibration(None)

    def test_seed_fallback_matches_legacy_rules(self):
        dc.set_calibration(None)
        try:
            from services.taobao_alliance import reference_drop_plausibility as plausible
            self.assertFalse(plausible(19900, 290)[0])       # 98.5% 降幅
            self.assertFalse(plausible(6490, 990)[0])        # <10 元且优惠>50 元
            self.assertFalse(plausible(10000, 300)[0])       # 33 倍参考价
            self.assertFalse(plausible(10000, 1500)[0])      # 恰 85%
            self.assertTrue(plausible(10000, 5000)[0])       # 5 折
            # 84% 中低价且优惠额超售价（drop 8400 > 1600×1.05）→ 引流划线，硬规则拦（新语义）
            self.assertFalse(plausible(10000, 1600)[0])
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


class MachineryEquipmentTests(unittest.TestCase):
    """工业/农用加工设备（B端大件）拦截：不进入默认优惠列表，家用电器豁免。"""

    def test_user_screenshot_machines_all_gated(self):
        # 用户截图的 8 条机器：万元设备 + 100元券，全部应拦
        machines = [
            ("新款多功能玉米食品蓬化机商用大米花空心棒江米机膨化五谷杂粮机", 377000),
            ("组合小型小麦大豆碾米机砂轮脱皮大米磨碎机农用五谷杂粮磨面机", 1122000),
            ("厂家供应芝麻清洗机小型多用芝麻大米小麦花生水洗机芝麻淘洗机", 1161600),
            ("本月特价供应大米淘洗机芝麻水洗谷物清洗机去石去沙", 1161600),
            ("304不锈钢齿盘粉碎机青稞大米打粉机香辛料孜然化工物料破碎机", 1161600),
            ("长期供应大米清洗机全自动大米水洗机五谷杂粮淘洗机紫苏籽清洗", 1188000),
            ("柴油机带动70商用600kg/时产能碾米机两用稻谷剥谷机大米碾米机", 1314700),
            ("粽子馅料拌馅机大米酱油肉块混合拌料", 1306800),
        ]
        for title, cents in machines:
            self.assertTrue(
                dc.is_non_consumer_category(title, cents),
                "应拦截: %s (%d)" % (title[:30], cents))

    def test_genuine_deals_not_harmed(self):
        # 真优惠不误伤：家用电器/消费电子豁免、低价家用小机器不拦、无设备词不拦
        self.assertFalse(dc.is_non_consumer_category("海尔洗衣机全自动10公斤家用大容量", 199900))
        self.assertFalse(dc.is_non_consumer_category("破壁机家用多功能豆浆料理机加热款", 89900))
        self.assertFalse(dc.is_non_consumer_category("榨汁机便携家用小型水果原汁机", 29900))
        self.assertFalse(dc.is_non_consumer_category("小米手机14Pro 5G手机", 599900))
        self.assertFalse(dc.is_non_consumer_category("电饭煲家用智能预约4L大容量", 39900))
        self.assertFalse(dc.is_non_consumer_category("血压计家用电子血压仪上臂式语音播报", 12900))
        self.assertFalse(dc.is_non_consumer_category("家用打印机无线彩色喷墨一体机", 49900))
        # 低价家用小机器（<500元）不拦
        self.assertFalse(dc.is_non_consumer_category("家用小型粉碎机五谷杂粮研磨机", 8900))
        self.assertFalse(dc.is_non_consumer_category("家用迷你爆米花机微波炉专用", 2900))
        self.assertFalse(dc.is_non_consumer_category("家用小型豆芽机全自动发豆芽器", 6900))
        # 含"机"但非设备的正常商品不拦（有机/手机壳/玩具）
        self.assertFalse(dc.is_non_consumer_category("有机五常大米东北稻花香5kg", 8990))
        self.assertFalse(dc.is_non_consumer_category("手机壳苹果全包边透明保护套", 2900))
        self.assertFalse(dc.is_non_consumer_category("螺蛳粉柳州正宗螺丝粉速食米粉", 3990))
        self.assertFalse(dc.is_non_consumer_category("地板袜防滑保暖袜冬季加厚", 1990))
        self.assertFalse(dc.is_non_consumer_category("无防腐剂无添加宝宝辅食肉松", 6990))
        self.assertFalse(dc.is_non_consumer_category("树脂摆件手办桌面装饰工艺品", 8900))
        self.assertFalse(dc.is_non_consumer_category("食品原料天然海藻糖烘焙用", 2590))
        # 生产全量验证发现的误伤点：菜籽油/大米品种/否定卖点/日化成分 全部不拦
        self.assertFalse(dc.is_non_consumer_category("金龙鱼菜籽油花生油调和油5L家用", 59900))
        self.assertFalse(dc.is_non_consumer_category("鲁花低芥酸菜籽油特香5L物理压榨家用", 89900))
        self.assertFalse(dc.is_non_consumer_category("五常大米原粮稻种稻香米5kg东北大米", 8990))
        self.assertFalse(dc.is_non_consumer_category("新疆木垒旱田小麦面粉含硒自留麦种", 2590))
        self.assertFalse(dc.is_non_consumer_category("吉内得大米新米3斤富硒丝苗米老种子", 1990))
        self.assertFalse(dc.is_non_consumer_category("农家自种面粉不用化肥无添加包子面条粉", 1590))
        self.assertFalse(dc.is_non_consumer_category("鑫乐河套有机面粉5斤无农药无化肥中高筋", 2990))
        self.assertFalse(dc.is_non_consumer_category("氨基酸表面活性剂洗发水控油蓬松", 3990))
        # 非整数/缺失金额不拦（不误判审计记录）
        self.assertFalse(dc.is_non_consumer_category("碾米机大型商用", None))
        self.assertFalse(dc.is_non_consumer_category("", 1000000))

    def test_business_marker_plus_equipment_core_fallback(self):
        # B端场景词 + 设备核心词兜底（未枚举设备名）
        self.assertTrue(dc.is_non_consumer_category("商用大型滚筒式烘干机食品加工设备", 1280000))
        self.assertTrue(dc.is_non_consumer_category("工厂直供全自动包装流水线设备", 2600000))
        # 只有 B端词无设备核心词 → 不拦（商用大米礼盒）
        self.assertFalse(dc.is_non_consumer_category("商用大米礼盒装5kg*2", 15800))
        # 只有设备核心词无 B端词、无精确设备名 → 不拦
        self.assertFalse(dc.is_non_consumer_category("家用机械表男士防水石英表", 19900))

    def test_non_consumer_categories_all_gated(self):
        # 全库扫描发现的其他非个人消费品类：五金/建材/汽配/农资/化工/医疗/办公
        self.assertTrue(dc.is_non_consumer_category("电钻冲击钻家用电动工具套装", 12900))
        self.assertTrue(dc.is_non_consumer_category("瓷砖美缝剂防水防霉填缝剂", 3900))
        self.assertTrue(dc.is_non_consumer_category("汽车轮胎205/55R16静音轮胎", 39900))
        self.assertTrue(dc.is_non_consumer_category("复合肥尿素化肥农用氮肥", 2590))
        self.assertTrue(dc.is_non_consumer_category("化工原料增稠剂工业级", 1990))
        self.assertTrue(dc.is_non_consumer_category("医疗器械一次性医用耗材", 5900))
        self.assertTrue(dc.is_non_consumer_category("收银机一体机餐饮点餐系统", 89900))
        self.assertTrue(dc.is_non_consumer_category("除草剂农用非选择性灭草剂", 990))
        self.assertTrue(dc.is_non_consumer_category("全自动包装机灌装机生产线设备", 1580000))


if __name__ == "__main__":
    unittest.main()


class SmallSpecLimitsTests(unittest.TestCase):
    def test_small_spec_limits_distinguish_portable_from_bulk(self):
        from services import discount_calibration as dc
        # 便携小件：所有量纲均小
        self.assertTrue(dc.small_spec_limits('益初乳霜纸婴儿云柔巾保湿纸巾擦鼻30抽宝宝用抽纸巾'))
        self.assertTrue(dc.small_spec_limits('【尝鲜】百岁山348ml*2瓶'))
        # 量大真品：任一量纲大 → 不拦
        self.assertFalse(dc.small_spec_limits('KUB可优比婴儿云柔巾80抽18包'))
        self.assertFalse(dc.small_spec_limits('聪妈抽纸悬挂式1000张加大加厚家用实惠装'))
        self.assertFalse(dc.small_spec_limits('百岁山570ml*24瓶整箱'))
        self.assertFalse(dc.small_spec_limits('漫花400纸巾抽纸可选30包整箱家用'))
        # 无规格或空标题 → 不按规格拦
        self.assertFalse(dc.small_spec_limits(''))
        self.assertFalse(dc.small_spec_limits('大包原木抽纸实惠装'))

    def test_spec_route_uses_median_price_and_preserves_bulk(self):
        from services import discount_calibration as dc
        payload = {'low_price_floor': 400, 'price_p50_cents': 790, 'sample_count': 10,
                   'calibrated_at': '2026-01-01T00:00:00+00:00'}
        dc.set_calibration({'paper': payload})
        try:
            # 小规格 + 低于主题中位价 → 小件
            self.assertTrue(dc.is_low_value_small_item('paper', 440, '益初乳霜纸30抽宝宝用'))
            # 小规格但价高于中位 → 不拦（可能真是高端小件）
            self.assertFalse(dc.is_low_value_small_item('paper', 1000, '乳霜纸30抽宝宝用'))
            # 量大（1000张）→ 不拦，即使价格低
            self.assertFalse(dc.is_low_value_small_item('paper', 619, '聪妈抽纸悬挂式1000张家用'))
            # 无规格 → 走 P15 线
            self.assertTrue(dc.is_low_value_small_item('paper', 201))
            self.assertFalse(dc.is_low_value_small_item('paper', 790))
        finally:
            dc.set_calibration(None)


class BrandMarketReferenceTests(unittest.TestCase):
    """品牌标准市场价判据：优惠价 ≤ 品牌单品标准市场价才为真优惠。"""

    def test_spec_unit_conversion_handles_multipliers_and_priority(self):
        from services import discount_calibration as dc
        self.assertEqual(dc.spec_unit_price_cents('百岁山348ml*2瓶', 1001), ('bottle500', 719))
        self.assertEqual(dc.spec_unit_price_cents('百岁山570ml*24瓶', 6390), ('bottle500', 233))
        self.assertEqual(dc.spec_unit_price_cents('百岁山348/570ml*24瓶*2箱', 9252), ('bottle500', 169))
        self.assertEqual(dc.spec_unit_price_cents('柴火大米5kg*2袋', 40000), ('kg', 4000))
        self.assertEqual(dc.spec_unit_price_cents('十月稻田10kg*5袋', 100000), ('kg', 2000))
        self.assertEqual(dc.spec_unit_price_cents('蓝月亮3kg*2瓶', 5990), ('kg', 998))
        self.assertEqual(dc.spec_unit_price_cents('金龙鱼5L', 7990), ('liter', 1598))
        self.assertEqual(dc.spec_unit_price_cents('维达100抽*24包', 1890), ('pack100', 78))
        self.assertEqual(dc.spec_unit_price_cents('鲁花5S花生油5L', 11990), ('liter', 2398))
        # 无规格/纯单瓶描述不折算
        self.assertIsNone(dc.spec_unit_price_cents('百岁山570ml/瓶', 7800))
        self.assertIsNone(dc.spec_unit_price_cents('漫花6大提悬挂式抽纸', 790))

    def test_brand_market_overpriced_blocks_small_spec_inflated_units(self):
        from services import discount_calibration as dc
        # 尝鲜 348ml×2 ¥10.01 → 500ml 折算 ¥7.19 >> 百岁山标准 ¥2.80×1.2
        self.assertTrue(dc.is_brand_market_overpriced('water', '百岁山天然矿泉水348ml*2瓶装', 1001))
        # 整箱真品：单瓶折算 ≤ 标准市场价 → 放
        self.assertFalse(dc.is_brand_market_overpriced('water', '百岁山570ml*24瓶整箱', 2359))
        self.assertFalse(dc.is_brand_market_overpriced('water', '百岁山570ml*24瓶整箱', 6390))
        self.assertFalse(dc.is_brand_market_overpriced('water', '百岁山570ml*24瓶*2箱', 9252))
        # 维达整箱单包 ¥0.79 << ¥3.00
        self.assertFalse(dc.is_brand_market_overpriced('paper', '维达100抽*24包整箱', 1890))
        # KUB 高端婴童纸：单包 ¥13.11 ≈ 可优比标准 ¥12×1.2 → 放（真品）
        self.assertFalse(dc.is_brand_market_overpriced('paper', 'KUB可优比婴儿云柔巾80抽18包', 18890))
        # 五常米：柴火 ¥40/kg ≤ ¥40×1.2 → 放
        self.assertFalse(dc.is_brand_market_overpriced('rice', '柴火大院五常大米5kg*2袋', 40000))
        self.assertFalse(dc.is_brand_market_overpriced('rice', '十月稻田五常大米10kg*5袋', 100000))
        # 未知品牌/无规格/无标准价 → 永不拦
        self.assertFalse(dc.is_brand_market_overpriced('water', '外星人电解质水24瓶整箱', 9999))
        self.assertFalse(dc.is_brand_market_overpriced('water', '百岁山570ml/瓶', 7800))
        self.assertFalse(dc.is_brand_market_overpriced('paper', '漫花6大提', 790))
