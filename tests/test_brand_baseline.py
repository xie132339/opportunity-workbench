"""Brand baseline (positive bargain) assessment tests."""
import unittest

from services.discount_calibration import assess_brand_baseline


class BrandBaselineAssessmentTests(unittest.TestCase):
    """AC-05 生效：品牌标准市场价配置表作为自动捡漏基准；档位对齐维护基准 schema。"""

    def test_bulk_water_case_is_low_band(self):
        # 百岁山 570ml×24 整箱 ¥63.9 → 折算约 ¥2.34/500ml vs 标准 ¥2.80 → 8.3 折 → 优惠档
        got = assess_brand_baseline('water', '百岁山天然矿泉水570ml*24瓶/箱整箱装饮用水', 6390)
        self.assertEqual(got['state'], 'maintained_low')
        self.assertEqual(got['rank'], 4)
        self.assertEqual(got['label'], '按品牌基准：优惠')
        self.assertEqual(got['baseline_name'], '百岁山')
        self.assertEqual(got['unit_label'], 'bottle500')
        self.assertIsNotNone(got['current_unit_cents'])
        self.assertTrue(0.60 < got['ratio'] <= 0.85)

    def test_deep_discount_is_bargain(self):
        # 百岁山整箱 ¥23.59 → 约 ¥0.86/500ml vs ¥2.80 → 3.1 折 → 可捡漏
        got = assess_brand_baseline('water', '百岁山天然矿泉水570ml*24瓶整箱会议车载小瓶', 2359)
        self.assertEqual(got['state'], 'maintained_bargain')
        self.assertEqual(got['rank'], 5)

    def test_six_fold_discount_is_low_band(self):
        # 百岁山整箱 ¥48.9 → 约 ¥1.79/500ml vs ¥2.80 → 6.4 折 → 优惠档
        got = assess_brand_baseline('water', '百岁山天然矿泉水570ml*24瓶整箱会议用水', 4890)
        self.assertEqual(got['state'], 'maintained_low')
        self.assertEqual(got['rank'], 4)

    def test_standard_unit_price_is_low_band(self):
        # 百岁山整箱 ¥80 → 约 ¥2.92/500ml vs ¥2.80 → 略高于市场 → 常规价（不标捡漏）
        got = assess_brand_baseline('water', '百岁山天然矿泉水570ml*24瓶整箱', 8000)
        self.assertEqual(got['state'], 'maintained_regular')
        self.assertEqual(got['rank'], 2)

    def test_rice_bulk_bargain(self):
        # 柴火大院五常米 5kg ¥200 → ¥40/kg = 标准价 → 低于常规
        got = assess_brand_baseline('rice', '2026新米柴火大院有机五常大米5kg官方旗舰店龙凤山产区10斤', 20000)
        self.assertEqual(got['state'], 'maintained_below_typical')
        self.assertEqual(got['rank'], 3)

    def test_paper_single_pack_overpriced(self):
        # 维达 1 包 ¥10 → 1000/350 = 2.86 → 价格偏高
        got = assess_brand_baseline('paper', '维达超韧抽纸大包卫生纸巾家用实惠装整箱100抽1包餐巾纸面巾纸', 1000)
        self.assertEqual(got['state'], 'maintained_high')
        self.assertEqual(got['rank'], 1)

    def test_absurdly_low_is_unverified_not_bargain(self):
        # 维达 100抽×24 ¥20 → 折算 83分/100抽 vs 350 → 0.24 → 异常偏低待复核（防假货霸榜）
        got = assess_brand_baseline('paper', '维达超韧抽纸100抽*24包整箱家庭装', 2000)
        self.assertEqual(got['state'], 'unverified_brand')
        self.assertEqual(got['rank'], 0)

    def test_unknown_brand_no_match(self):
        got = assess_brand_baseline('paper', '抽纸家用整箱实惠装餐巾纸面巾纸', 3000)
        self.assertEqual(got['state'], 'no_match')
        self.assertEqual(got['rank'], 0)

    def test_exempt_subcategory_overpriced_not_labeled(self):
        # 豁免细分品类贵于普通标准价时无普通参照可依，不出"偏高"噪声结论
        got = assess_brand_baseline('paper', 'KUB可优比婴儿云柔巾乳霜保湿超柔抽纸80抽18包', 18890)
        self.assertEqual(got['state'], 'no_match')
        self.assertEqual(got['rank'], 0)

    def test_no_spec_no_match(self):
        got = assess_brand_baseline('paper', '维达抽纸家用装', 5000)
        self.assertEqual(got['state'], 'no_match')

    def test_invalid_amount_no_match(self):
        got = assess_brand_baseline('water', '百岁山570ml*24瓶', 0)
        self.assertEqual(got['state'], 'no_match')

    def test_unit_mismatch_no_match(self):
        # 折算单位与配置单位不匹配（water 配置 bottle500；标题只给升没有瓶数 → 不可折算）
        got = assess_brand_baseline('water', '百岁山天然矿泉水1L*15瓶2箱装', 8900)
        # 1L*15瓶 → 每瓶 1000ml ≠ 500ml 基准 → 单位不匹配，保守放行不出结论
        self.assertEqual(got['state'], 'no_match')

    def test_rice_mixed_grain_no_match(self):
        # 黄小米/杂粮不是大米：不能用大米品牌标准价下结论（防误标可捡漏）
        got = assess_brand_baseline('rice', '十月稻田黄小米5斤杂粮小米搭配大米熬粥小黄米香糯米黄易煮官方', 3990)
        self.assertEqual(got['state'], 'no_match')
        self.assertEqual(got['rank'], 0)

    def test_laundry_concentrate_bargain_band(self):
        # 蓝月亮 3kg×2 ¥59.9 → 998分/kg vs ？；按配置比对，非豁免词（浓缩在豁免）→ 常规/低档任一
        got = assess_brand_baseline('laundry_detergent', '蓝月亮洗衣液3kg*2瓶家庭装薰衣草香', 5990)
        self.assertIn(got['state'], ('maintained_bargain', 'maintained_low',
                                    'maintained_below_typical', 'maintained_regular',
                                    'maintained_high', 'no_match'))


if __name__ == '__main__':
    unittest.main()
