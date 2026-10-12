"""用户验收样本防回归套件。

把用户多轮明确认可的"必须放行"与"必须拦截/不标"的真实商品样本固化为自动化
回归，防止后续改动再次引入误伤（品牌判据 4183 条误伤事件）或漏拦（假优惠霸榜）。
覆盖：品牌贵过市场价判据、引流形态词、细分品类豁免、品牌捡漏标签、混品守卫、
主题映射、配置完整性。数据驱动层（统一价聚集/低价小件/市场价带）依赖校准表，
由 tests/test_discount_calibration.py 与 tests/test_price_admission.py 专项覆盖。
"""
import unittest

from services.discount_calibration import (
    assess_brand_baseline, brand_from_title, has_trial_gating_marker,
    is_brand_market_overpriced, topic_key, BRAND_REFERENCE_CONFIG,
)


class MustGateSamplesTests(unittest.TestCase):
    """用户明确否定的数据：必须被拦出默认列表（品牌判据/形态词任一命中）。"""

    def test_尝鲜百岁山小瓶装必须拦截(self):
        # 用户原话："百岁山 348ml 4块多什么玩意，这种数据肯定不算优惠"
        title = '【尝鲜】百岁山天然矿泉水348ml2瓶便携车载会议健康饮用水小瓶装'
        self.assertTrue(
            has_trial_gating_marker(title)
            or is_brand_market_overpriced('water', title, 1001))

    def test_茶语110抽3包十四元必须拦截(self):
        # 折算 424 分/100抽 > 心相印标准 280×1.2 —— 真贵
        title = '【110抽3包】心相印茶语抽纸家用纸巾面巾纸餐巾纸卫生纸学生实惠'
        self.assertTrue(is_brand_market_overpriced('paper', title, 1401))

    def test_维达单包十元必须拦截(self):
        title = '维达超韧抽纸大包卫生纸巾家用实惠装整箱100抽1包餐巾纸面巾纸'
        self.assertTrue(is_brand_market_overpriced('paper', title, 1000))

    def test_五得利五公斤一百二十三元必须拦截(self):
        # 折算 24.7 元/kg > 五得利标准 800×1.2
        title = '五得利面粉家用小麦粉5kg袋装粉质细腻日期新鲜不易粘手蒸食暄软'
        self.assertTrue(is_brand_market_overpriced('flour', title, 12358))

    def test_五得利十斤袋装必须拦截(self):
        title = '五得利10斤袋面粉家用小麦粉装不易粘手蒸食暄软粉质细腻日期新鲜'
        self.assertTrue(is_brand_market_overpriced('flour', title, 12371))


class MustKeepSamplesTests(unittest.TestCase):
    """用户认可的真捡漏/真品：任何防线都不得误伤。"""

    def test_百岁山整箱23块59必须放行(self):
        title = '百岁山天然矿泉水570ml*24瓶整箱会议车载小瓶饮用水348m'
        self.assertFalse(is_brand_market_overpriced('water', title, 2359))

    def test_百岁山整箱63块9必须放行(self):
        title = '百岁山水中贵族天然矿泉水570ml*24瓶/箱饮用水日常补水含偏'
        self.assertFalse(is_brand_market_overpriced('water', title, 6390))

    def test_KUB乳霜纸整箱必须放行(self):
        # 细分品类豁免：乳霜纸单价系统性高于普通纸，不得按普通标准价误拦
        title = 'KUB可优比婴儿云柔巾乳霜保湿超柔抽纸保湿擦鼻涕柔纸巾80抽18'
        self.assertFalse(is_brand_market_overpriced('paper', title, 18890))

    def test_柴火大院五常米必须放行(self):
        title = '2026新米柴火大院有机五常大米5kg官方旗舰店龙凤山产区10斤'
        self.assertFalse(is_brand_market_overpriced('rice', title, 20000))

    def test_维达整箱必须放行(self):
        title = '维达超韧抽纸整箱100抽24包家用实惠装大包纸巾'
        self.assertFalse(is_brand_market_overpriced('paper', title, 4990))

    def test_悬挂纸七块9必须放行(self):
        title = '丹娜丝悬挂式抽纸卫生纸加大加厚餐巾纸厨房纸巾面巾纸擦手纸'
        self.assertFalse(is_brand_market_overpriced('paper', title, 790))

    def test_多力菜籽油必须放行(self):
        # 菜籽油细分品类豁免（单价高于普通油）
        title = '【官方旗舰店】多力菁选特香菜籽油5.2L营养食用油低芥酸非转基因'
        self.assertFalse(is_brand_market_overpriced('oil', title, 13900))

    def test_新疆面粉必须放行(self):
        title = '新疆天山面粉1966中筋小麦粉家用5kg商用特精制拉面条饺子非雪'
        self.assertFalse(is_brand_market_overpriced('flour', title, 4390))

    def test_蓝月亮整箱必须放行(self):
        title = '蓝月亮洗衣液3kg*2瓶家庭装薰衣草香'
        self.assertFalse(is_brand_market_overpriced('laundry_detergent', title, 5990))


class BrandBaselineLabelTests(unittest.TestCase):
    """品牌捡漏标签（移动端/列表/排序）：正例标注、反例不标、混品不误标。"""

    def test_天山面粉五块九九公斤标可捡漏(self):
        got = assess_brand_baseline('flour', '新疆天山面粉精制粉10kg高20斤袋拉条子拌面天山牌家用中筋冬小麦', 11980)
        self.assertEqual(got['state'], 'maintained_below_typical')
        self.assertEqual(got['label'], '按品牌基准：低于常规')

    def test_鲁花油五折标可捡漏(self):
        got = assess_brand_baseline('cooking_oil', '鲁花压榨葵花仁油5L剥壳去皮压榨桶装食用油植物油葵花籽油', 9990)
        self.assertEqual(got['state'], 'maintained_bargain')
        self.assertEqual(got['label'], '按品牌基准：可捡漏')

    def test_黄小米不得用大米标准价标注(self):
        # 混品守卫：非本品类混品（黄小米≠大米）不得误标可捡漏
        got = assess_brand_baseline('rice', '十月稻田黄小米5斤杂粮小米搭配大米熬粥小黄米香糯米黄易煮官方', 3990)
        self.assertEqual(got['state'], 'no_match')

    def test_茶语真贵标价格偏高(self):
        got = assess_brand_baseline('paper', '【110抽3包】心相印茶语抽纸家用纸巾面巾纸餐巾纸卫生纸学生实惠', 1401)
        self.assertEqual(got['state'], 'maintained_high')

    def test_尝鲜百岁山标价格偏高(self):
        got = assess_brand_baseline('water', '【尝鲜】百岁山天然矿泉水348ml2瓶便携车载会议健康饮用水小瓶装', 1001)
        self.assertEqual(got['state'], 'maintained_high')

    def test_无品牌不出结论(self):
        got = assess_brand_baseline('paper', '抽纸家用整箱实惠装餐巾纸面巾纸', 3000)
        self.assertEqual(got['state'], 'no_match')


class TopicMappingTests(unittest.TestCase):
    """主题映射：品牌基准必须用六主题 key（display_topic 粗分类踩坑防回归）。"""

    def test_六主题来源名映射(self):
        cases = {
            '淘宝店抽纸优惠搜索': 'paper',
            '淘宝生活品·饮用水优惠搜索': 'water',
            '淘宝生活品·大米优惠搜索': 'rice',
            '淘宝生活品·面粉优惠搜索': 'flour',
            '淘宝生活品·食用油优惠搜索': 'cooking_oil',
            '淘宝生活品·洗衣液优惠搜索': 'laundry_detergent',
        }
        for name, want in cases.items():
            self.assertEqual(topic_key(name), want, name)

    def test_未知来源不映射(self):
        self.assertIsNone(topic_key('自定义来源'))


class BrandConfigCompletenessTests(unittest.TestCase):
    """配置完整性（用户：全面的配置数据、详细的配置）：每条品牌配置四字段齐全。"""

    def test_全部品牌配置字段完整且价格为正(self):
        self.assertGreaterEqual(sum(len(v) for v in BRAND_REFERENCE_CONFIG.values()), 95)
        for topic, brands in BRAND_REFERENCE_CONFIG.items():
            self.assertIn(topic, ('paper', 'water', 'rice', 'flour',
                                  'cooking_oil', 'laundry_detergent'))
            for name, spec in brands.items():
                self.assertTrue(isinstance(name, str) and name, topic)
                self.assertTrue(isinstance(spec, dict), f'{topic}/{name}')
                for field in ('terms', 'unit', 'price_cents', 'note'):
                    self.assertIn(field, spec, f'{topic}/{name} 缺 {field}')
                self.assertTrue(isinstance(spec['terms'], tuple) and spec['terms'],
                                f'{topic}/{name} terms 空')
                self.assertTrue(isinstance(spec['price_cents'], int)
                                and spec['price_cents'] > 0, f'{topic}/{name} 价格异常')
                self.assertTrue(spec['note'], f'{topic}/{name} note 空')

    def test_品牌别名可命中(self):
        # 别名（英文）也要命中：维达 vinda
        self.assertEqual(brand_from_title('vinda 超韧抽纸100抽24包', 'paper'), '维达')

    def test_品牌别名同样参与贵过市场价判据(self):
        # 回归：brand_from_title 返回配置 key 后，别名标题不得绕过品牌判据
        self.assertTrue(is_brand_market_overpriced('paper', 'vinda 超韧抽纸100抽1包', 1000))


if __name__ == '__main__':
    unittest.main()
