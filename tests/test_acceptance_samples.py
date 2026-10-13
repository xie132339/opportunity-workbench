"""用户验收样本防回归套件。

把用户多轮明确认可的"必须放行"与"必须拦截/不标"的真实商品样本固化为自动化
回归，防止后续改动再次引入误伤（品牌判据 4183 条误伤事件）或漏拦（假优惠霸榜）。
覆盖：品牌贵过市场价判据、引流形态词、细分品类豁免、品牌捡漏标签、混品守卫、
主题映射、配置完整性。数据驱动层（统一价聚集/低价小件/市场价带）依赖校准表，
由 tests/test_discount_calibration.py 与 tests/test_price_admission.py 专项覆盖。
"""
import unittest

import services.discount_calibration as dc
from services.discount_calibration import (
    assess_brand_baseline, brand_from_title, has_trial_gating_marker,
    is_brand_market_overpriced, topic_key, BRAND_REFERENCE_CONFIG,
)


class StrikeThroughSamplesTests(unittest.TestCase):
    """用户截图实证（2026-10-13）：联盟划线价虚高，页面却按划线价差展示
    “优惠¥510/约73%”类假优惠。必须被 judge_drop 判为划线价不可信；同时
    高价真品深折扣（低氘水 61% off）不得误伤。"""

    PAPER_CAL = {"paper": {
        "price_ceiling_cents": 69972,
        "high_log": 1.9,               # ¥79 以上为高价段（P97 箱上沿）
        "high_reserve_ratio_cap": 3.2,  # 高价段划线价≤3.2倍推广价
        "bins": [(2.0, 2.5, 0.85, 5000), (2.5, 3.0, 0.85, 5000),
                 (3.0, 3.5, 0.85, 16210), (3.5, 4.0, 0.85, 16210)],
        "global_ratio_cap": 0.8995, "global_drop_cap": 16210,
        "sample_count": 2625, "calibrated_at": "2026-10-13T00:00:00+00:00"}}
    WATER_CAL = {"water": {
        "price_ceiling_cents": 45700,
        "high_log": 4.5,               # ¥316 以上为高价段
        "high_reserve_ratio_cap": 3.5,
        "bins": [(3.5, 4.0, 0.78, 18800), (4.0, 4.5, 0.64, 27300)],
        "global_ratio_cap": 0.81, "global_drop_cap": 18000,
        "sample_count": 2625, "calibrated_at": "2026-10-13T00:00:00+00:00"}}

    def setUp(self):
        dc.set_calibration(self.PAPER_CAL | self.WATER_CAL)

    def tearDown(self):
        dc.set_calibration(None)

    def test_心相印90抽整箱划线虚高必须拦截(self):
        # 天猫实际原价 79.9（用户截图铁证），联盟划线价 319.8（4倍）→ 高价段比值超限
        ok, reason = dc.judge_drop('paper', 31980, 7990)
        self.assertFalse(ok)
        self.assertIn('划线价', reason)

    def test_KUB云柔巾80抽18包划线虚高必须拦截(self):
        # 推广价 188.9、划线价 699（3.7倍），页面曾显示“优惠¥510.10/约73%”
        ok, _reason = dc.judge_drop('paper', 69900, 18890)
        self.assertFalse(ok)

    def test_维达整箱划线虚高必须拦截(self):
        ok, _reason = dc.judge_drop('paper', 19900, 3517)
        self.assertFalse(ok)

    def test_德佑乳霜纸划线虚高必须拦截(self):
        ok, _reason = dc.judge_drop('paper', 19990, 3480)
        self.assertFalse(ok)

    def test_心相印悬挂式划线虚高必须拦截(self):
        ok, _reason = dc.judge_drop('paper', 24000, 3990)
        self.assertFalse(ok)

    def test_低价段深划线仍受全局界约束(self):
        # 39.9 元悬挂纸 drop 200 元（83.4%）：中低价段走分箱/全局界
        ok, _reason = dc.judge_drop('paper', 24000, 3990)
        self.assertFalse(ok)

    def test_高价真品深折扣不误伤(self):
        # 358 元低氘水 61% off（原价 1080）：reserve/promo=3.0 < 3.5 → 放行
        ok, reason = dc.judge_drop('water', 108000, 35800)
        self.assertTrue(ok, reason)

    def test_真实立减24元仍可作优惠证据(self):
        # 心相印官方“限时立减24元”：reserve=103.9 仅 1.3 倍推广价 → 放行
        ok, reason = dc.judge_drop('paper', 10390, 7990)
        self.assertTrue(ok, reason)

    def test_babycare与KUB别名命中品牌判据(self):
        self.assertEqual(brand_from_title('babycare云柔巾婴儿抽纸', 'paper'), 'babycare')
        self.assertEqual(brand_from_title('KUB可优比婴儿云柔巾', 'paper'), '可优比')
        self.assertEqual(brand_from_title('德佑乳霜纸', 'paper'), '德佑')

    def test_无品牌无规格高价纸品降级(self):
        # 用户截图：AA桃花郎 300张 ¥199.10（优惠¥400.80/66.8%）——高价无参照，不出优惠
        self.assertTrue(dc.is_high_tier_unverifiable('paper', 'AA桃花郎抽纸原生木浆5层共300张整箱卫生纸纸巾', 19910))
        # 桃花郎 300张 ¥599（优惠¥572.20/95.5%）
        self.assertTrue(dc.is_high_tier_unverifiable('paper', '桃花郎300张纸巾抽纸整箱家用实惠餐巾纸卫生纸厕纸', 59900))
        # 纸巾定制 ¥120（优惠¥179）
        self.assertTrue(dc.is_high_tier_unverifiable('paper', '纸巾定制可印logo饭店餐巾纸商用餐厅方巾纸订制订做专用抽纸散装', 12000))
        # 大包悬挂式 ¥199.9（优惠¥160）
        self.assertTrue(dc.is_high_tier_unverifiable('paper', '大包悬挂式底部抽纸整箱家用实惠装厕所卫生间专用擦手纸', 19990))

    def test_可折算品牌高价品不被误伤(self):
        # KUB（可折算 pack100=1311 分，品牌参考 1200×1.2 内）→ 高价段可验证，不降级
        self.assertFalse(dc.is_high_tier_unverifiable('paper', 'KUB可优比婴儿云柔巾乳霜保湿超柔抽纸保湿擦鼻涕柔纸巾80抽18包', 18890))
        # 心相印茶语 110抽24包 ¥164.7（品牌可折算）
        self.assertFalse(dc.is_high_tier_unverifiable('paper', '【下拉详情享优惠】心相印茶语抽纸3层110抽24包', 16470))
        # 低价悬挂纸（< 高价段门槛）不受影响
        self.assertFalse(dc.is_high_tier_unverifiable('paper', '心相印悬挂式抽纸整箱卫生纸实惠装家用壁挂式纸巾M码320抽挂抽', 3990))

    def test_优惠额超售价硬规则(self):
        # 主动审计发现：大量漏网假优惠共性 = 优惠额 > 促销价（蓝漂 ¥5.89 纸优惠 ¥10.01、
        # 洞庭山 ¥92.93 水优惠 ¥106.07、道道全 ¥54.8 油优惠 ¥104.2）——优惠不可能大于售价
        dc.set_calibration(None)  # seed 态（生产校准下同样生效：这些条目均为中低价段）
        try:
            ok, _ = dc.judge_drop('paper', 1590, 589)      # 蓝漂：drop 1001 > 589×1.05 → 拦
            self.assertFalse(ok)
            ok, _ = dc.judge_drop('water', 19900, 9293)    # 洞庭山：drop 10607 > 9293×1.05 → 拦
            self.assertFalse(ok)
            ok, _ = dc.judge_drop('cooking_oil', 15900, 5480)  # 道道全：drop 10420 > 5480×1.05 → 拦
            self.assertFalse(ok)
            # 真实优惠不误伤：半价（drop≈promo）与低折扣
            ok, _ = dc.judge_drop('paper', 6000, 3000)     # 恰半价 → 放行
            self.assertTrue(ok)
            ok, _ = dc.judge_drop('paper', 5990, 2990)     # 半价划线误差内 → 放行
            self.assertTrue(ok)
            ok, _ = dc.judge_drop('water', 10000, 7000)    # 七折 → 放行
            self.assertTrue(ok)
        finally:
            dc.set_calibration(None)

    def test_water品牌多规格基准(self):
        # 用户：怡宝/农夫山泉/娃哈哈/百岁山等主流品牌要详细到"品牌+产品单价"参考。
        # 瓶装用 bottle500 基准；大瓶/桶装折算 liter 用 extra_units 每升基准；
        # 整箱为常规规格（factor=1.0），小规格保留 1.4 溢价保护。
        dc.set_calibration(None)
        try:
            # 怡宝 4.5L×4桶×10箱 ¥348.8 → 1.94 元/L < 3.6 元/L → 真便宜不误伤
            self.assertFalse(dc.is_brand_market_overpriced('water', '怡宝纯净水4.5L*4桶*10箱整箱大桶装家庭饮用水', 34880))
            # 怡宝 4.5L×4桶单箱 ¥348.8 → 19.4 元/L 贵 → 拦
            self.assertTrue(dc.is_brand_market_overpriced('water', '怡宝纯净水4.5L*4桶家庭大桶装饮用水', 34880))
            # 农夫山泉 550ml×24 整箱 ¥72 → 3 元/瓶 贵 → 拦；¥30 → 1.13 元/瓶 便宜 → 放行
            self.assertTrue(dc.is_brand_market_overpriced('water', '农夫山泉饮用天然水550ml*24瓶整箱', 7200))
            self.assertFalse(dc.is_brand_market_overpriced('water', '农夫山泉饮用天然水550ml*24瓶整箱', 3000))
            # 娃哈哈 596ml×24 ¥48 → 2 元/瓶 正常 → 放行
            self.assertFalse(dc.is_brand_market_overpriced('water', '娃哈哈纯净水596ml*24瓶整箱', 4800))
            # 百岁山 570ml×24 ¥110 → 4.58 元/瓶 贵 → 拦
            self.assertTrue(dc.is_brand_market_overpriced('water', '百岁山饮用天然矿泉水570ml*24瓶整箱', 11000))
            # 依云整箱 ¥120 → 15 元/L < 30.3 → 放行；小规格 6 瓶 → 1.4 因子保护放行
            self.assertFalse(dc.is_brand_market_overpriced('water', '依云天然矿泉水330ml*24瓶整箱装', 12000))
            self.assertFalse(dc.is_brand_market_overpriced('water', '依云天然矿泉水330ml*6瓶装', 6000))
        finally:
            dc.set_calibration(None)

    def test_品牌库广覆盖(self):
        # 用户：品牌为什么这么少，要非常多覆盖非常广。六主题 176 品牌，
        # 每主题抽查新品牌判据：贵拦、正常/便宜放行、豁免细分品类不误伤。
        dc.set_calibration(None)
        try:
            # paper：低价纸基准=200（100抽正常零售2元）；3元/包拦、2元/包放行
            self.assertTrue(dc.is_brand_market_overpriced('paper', '蓝漂抽纸整箱批发10包装家用实惠装纸巾', 3000))
            self.assertFalse(dc.is_brand_market_overpriced('paper', '蓝漂抽纸整箱批发10包装家用实惠装纸巾', 2000))
            self.assertFalse(dc.is_brand_market_overpriced('paper', '丝飘抽纸10包装家用纸巾', 2000))
            # 无抽数 pack 折算 pack100：130抽装 3 元/包不误伤
            self.assertFalse(dc.is_brand_market_overpriced('paper', '心相印茶语经典抽纸3层130抽10包', 3000))
            # 悬挂纸豁免
            self.assertFalse(dc.is_brand_market_overpriced('paper', '心相印悬挂式抽纸320抽大包壁挂纸巾', 3000))
            # water：今麦郎/凉白开/润田整箱贵拦、便宜放行；椰树椰汁（非矿泉水词）不误伤
            self.assertTrue(dc.is_brand_market_overpriced('water', '今麦郎纯净水550ml*24瓶整箱装', 5000))
            self.assertFalse(dc.is_brand_market_overpriced('water', '今麦郎纯净水550ml*24瓶整箱装', 3000))
            self.assertTrue(dc.is_brand_market_overpriced('water', '今麦郎凉白开熟水500ml*24瓶整箱', 7200))
            self.assertFalse(dc.is_brand_market_overpriced('water', '润田翠饮用纯净水550ml*24瓶整箱', 2800))
            self.assertFalse(dc.is_brand_market_overpriced('water', '椰树牌正宗椰汁245ml*24罐装', 5000))
            # rice：五常/稻花香豁免保护高端真品；普通五芳斋放行
            self.assertFalse(dc.is_brand_market_overpriced('rice', '稻花香五常大米10斤装当季新米', 8000))
            self.assertFalse(dc.is_brand_market_overpriced('rice', '五芳斋东北大米10斤装', 4500))
            # flour：陈克明 7.6 元/kg 拦、6 元/kg 放行
            self.assertTrue(dc.is_brand_market_overpriced('flour', '陈克明小麦面粉5kg家用装', 3800))
            self.assertFalse(dc.is_brand_market_overpriced('flour', '陈克明小麦面粉5kg家用装', 3000))
            # oil：道道全菜籽油（细分豁免）不误伤；香满园调和油 100 元/5L 拦、75 元放行
            self.assertFalse(dc.is_brand_market_overpriced('cooking_oil', '道道全纯正菜籽油5L桶装', 8000))
            self.assertTrue(dc.is_brand_market_overpriced('cooking_oil', '香满园食用调和油5L桶装', 10000))
            self.assertFalse(dc.is_brand_market_overpriced('cooking_oil', '香满园食用调和油5L桶装', 7500))
            # laundry：金纺柔顺剂命中"柔顺"豁免（柔顺剂品类价格透明不误伤）；
            # 网易严选放行；超能高端线豁免
            self.assertFalse(dc.is_brand_market_overpriced('laundry_detergent', '金纺衣物柔顺剂3L家庭装', 6000))
            self.assertFalse(dc.is_brand_market_overpriced('laundry_detergent', '金纺衣物柔顺剂3L家庭装', 4500))
            self.assertFalse(dc.is_brand_market_overpriced('laundry_detergent', '网易严选洗衣液3kg实惠装', 2500))
        finally:
            dc.set_calibration(None)

    def test_品牌判据防误伤(self):
        # 全量复验发现 235 条拦截中的误伤（小瓶/联名限定/高端线/加号组合规格），
        # 修复后：真贵货仍拦、正常价全部放行（用户：一定不要误伤真实捡漏）。
        dc.set_calibration(None)
        try:
            # 加号组合量贩装："18+6包"=24包 → 2.5元/包 130抽 不误伤
            self.assertFalse(dc.is_brand_market_overpriced('paper', '维达细韧抽纸3层130抽18+6包XS码纸巾', 5990))
            # 联名/限定/迷你/四层豁免（得宝/心相印联名款、迷你抽纸、四层正厚）
            self.assertFalse(dc.is_brand_market_overpriced('paper', '【万圣节限定款】tempo得宝南瓜香迷你抽纸80抽4包h', 1990))
            self.assertFalse(dc.is_brand_market_overpriced('paper', '【新品上市】tempo得宝秋季限定桂花香印花抽纸80抽15包', 10090))
            self.assertFalse(dc.is_brand_market_overpriced('paper', '【积分】会员精选Tempo得宝无香软抽四层正厚M码抽纸90抽15包/箱', 11000))
            # water 小瓶整箱保留 1.4 溢价保护（380ml）；无乘号桶数 5L4桶=20L；婴儿水豁免
            self.assertFalse(dc.is_brand_market_overpriced('water', '农夫山泉饮用水非纯净水非矿泉水天然水红盖380ml*24瓶 整箱装1箱', 4112))
            self.assertFalse(dc.is_brand_market_overpriced('water', '农夫山泉饮用天然水5L4桶5升整箱非矿泉水家庭办公泡茶做饭饮用水', 4612))
            self.assertFalse(dc.is_brand_market_overpriced('water', '屈臣氏饮用水汪汪队联名200ml*24瓶非矿泉蒸馏水整箱便携小瓶', 6490))
            self.assertFalse(dc.is_brand_market_overpriced('water', '农夫山泉婴儿水天然水母婴水适合宝宝婴幼儿饮用水1L低钠整箱', 4000))
            # rice：稻花香基准 2000（五常稻花香 10元/斤不误伤）；鲜米/6步鲜豁免；小包装<2.5kg 跳过
            self.assertFalse(dc.is_brand_market_overpriced('rice', '响水大米千年御贡东北稻花香2号新米5kg石板大米2025新米', 8900))
            self.assertFalse(dc.is_brand_market_overpriced('rice', '【下拉领补贴】金龙鱼稻谷鲜生6步鲜稻花香大米5kg东北寒地大米', 7990))
            self.assertFalse(dc.is_brand_market_overpriced('rice', '【金龙鱼稻花香】6步鲜大米500g稻谷先生1斤粳米粥米宿舍福利正品', 1090))
            # oil：加号组合 5.68L+5L=10.68L；葵仁油豁免
            self.assertFalse(dc.is_brand_market_overpriced('cooking_oil', '金龙鱼黄金比例植物食用调和油5.68L+5L葵调家用炒菜油食用油', 17180))
            self.assertFalse(dc.is_brand_market_overpriced('cooking_oil', '多力浓香葵仁油1.8L/桶家用炒菜营养健康食用油官方正品旗舰店', 6990))
            # laundry：超能高端线（植沐/悦色/柔顺/亲肤/椰油/APG）豁免
            self.assertFalse(dc.is_brand_market_overpriced('laundry_detergent', '超能植沐悦色洗衣液3.38kg*2天然椰子油植物氨基酸绿色风铃草香', 12990))
            self.assertFalse(dc.is_brand_market_overpriced('laundry_detergent', '超能洗衣液APG10斤家用护衣护色抵抗沉积低泡易漂官方旗舰店正品', 13625))
            # 真贵货仍拦
            self.assertTrue(dc.is_brand_market_overpriced('water', 'Ganten景田饮用纯净水560ml*24瓶整箱大瓶装水会议饮用水非矿泉', 5989))
            self.assertTrue(dc.is_brand_market_overpriced('water', 'Ganten百岁山天然矿泉水4.5L10箱大桶泡茶冲奶煮饭整箱饮用水批发', 43750))
            # 真贵货仍拦（得宝普通款 7.3元/包 90抽=815分/100抽 > 450×1.2 拦；四层正厚豁免放行）
            self.assertTrue(dc.is_brand_market_overpriced('paper', 'Tempo得宝软抽纸巾M码90抽15包/箱', 11000))
            self.assertFalse(dc.is_brand_market_overpriced('paper', '【积分】会员精选Tempo得宝无香软抽四层正厚M码抽纸90抽15包/箱', 11000))
            # 细分品类豁免：厨房抽纸/天鹅绒/Mini/羊绒面纸/润田翠/臻品贵族/河套雪
            self.assertFalse(dc.is_brand_market_overpriced('paper', '【下拉享优惠】心相印厨房抽纸2层70抽3包大张厨房专用卷纸吸水纸', 2990))
            self.assertFalse(dc.is_brand_market_overpriced('paper', '【买3加赠Pingu雨伞】Tempo得宝天鹅绒软抽纸巾M码60抽20包亲肤', 9990))
            self.assertFalse(dc.is_brand_market_overpriced('paper', 'Tempo得宝Mini系列抽取式面巾纸ins风抽纸80抽×18包', 7290))
            self.assertFalse(dc.is_brand_market_overpriced('water', '润田翠天然矿泉水饮用水350ml*24瓶井冈山包邮整箱', 3500))
            self.assertFalse(dc.is_brand_market_overpriced('flour', '金沙河 臻品贵族多用途小麦粉 馒头 包子 饺子多用中筋面粉5kg', 5390))
            self.assertFalse(dc.is_brand_market_overpriced('flour', '河套面粉河套雪10kg(2.5kg*4袋)小麦粉多用途粉包子面条馒头面粉', 20000))
            # 真贵货仍拦
            self.assertTrue(dc.is_brand_market_overpriced('water', '娃哈哈饮用纯净水饮用水596ml*12瓶塑封膜装水企业办公开会用水', 6900))
            self.assertTrue(dc.is_brand_market_overpriced('water', '娃哈哈饮用纯净水2.15L装整箱大瓶量贩装饮用水非矿泉水家庭用水', 4900))
            self.assertTrue(dc.is_brand_market_overpriced('flour', '金沙河家庭多用途小麦粉10斤*1袋中筋包子馒头花卷多用面粉', 5520))
        finally:
            dc.set_calibration(None)

    def test_大王与绽家品牌基准(self):
        # 大王奢润乳霜纸 60抽×30包 ¥599 → 折算 33.3 元/100抽 > 大王基准 1200×1.2 → 拦
        self.assertTrue(dc.is_brand_market_overpriced('paper', '大王奢润保湿抽纸60抽*30包整箱抽纸', 59900))
        # 乳霜/婴儿/宝宝豁免词按设计放行（乳霜纸单价高，不误伤高端真品）
        self.assertFalse(dc.is_brand_market_overpriced('paper', '大王奢润保湿乳霜婴儿柔纸巾60抽*30包整箱保湿抽纸宝宝专用', 59900))
        # 绽家香氛洗衣液 1.7kg×8 ¥559.9 → 41 元/kg < 绽家基准 4000×1.2 → 不误伤
        self.assertFalse(dc.is_brand_market_overpriced('laundry_detergent', '【繁花全家桶】绽家四季繁花香氛洗衣液持久留香护色除菌1.7kg*8', 55990))


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
