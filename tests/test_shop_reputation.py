# -*- coding: utf-8 -*-
"""店铺离谱率过滤（用户：很多店铺都是瞎搞的，数据太离谱就不展示，阈值可配置）。

淘宝联盟 API 不返回店铺粉丝数字段，用数据驱动的"店铺离谱率"作为低信誉小店
代理；坏店条目的校准界按 SHOP_BAD_CAP_K 收紧，店内无法形成可验证优惠的条目
不进入默认列表（整条不展示），可验证真优惠保留（不误伤）。
"""
import json
import unittest

from services import discount_calibration as dc
from services import product_search
from services import shop_reputation

_META = dict(
    title='测试商品纸巾整箱',
    item_url='https://item.taobao.com/item.htm?id=123456',
    num_iid='123456',
    user_type='0',
)


def _row(shop, promo, reserve, title='测试商品纸巾整箱', quote_amount=5000):
    fields = dict(_META)
    fields.update(title=title, shop_title=shop,
                  zk_final_price='%.2f' % (promo / 100),
                  reserve_price='%.2f' % (reserve / 100))
    return {
        'source_method': 'taobao_alliance',
        'source_name': '淘宝生活品·测试优惠源',   # 未知主题：校准判据宽松
        'title': title,
        'promotional_candidate': True,
        'display_quote': {'kind': 'official_promotion_observation', 'amount_cents': quote_amount},
        'metadata_json': json.dumps({'taobao_alliance_source_fields': fields}),
    }


class ShopReputationTests(unittest.TestCase):

    def tearDown(self):
        shop_reputation.set_shop_stats(None)

    def _judge(self, reserve, promo, shop_bad=False):
        # 用未知主题走确定性 seed 分支（不依赖本地校准表）
        return dc.judge_drop(None, reserve, promo, shop_bad=shop_bad)

    def test_bad_shop_判定_离谱率阈值与最小条目(self):
        shop_reputation.set_shop_stats({
            '瞎搞小店': [4, 3],       # 3/4 = 75% >= 50%，条目 >= 3 → 坏店
            '正常小店': [4, 1],       # 1/4 = 25% < 50% → 正常
            '条目太少店': [2, 2],      # 100% 离谱但条目 < 3 → 不判坏店（避免误伤）
        })
        self.assertTrue(shop_reputation.is_bad_shop('瞎搞小店'))
        self.assertFalse(shop_reputation.is_bad_shop('正常小店'))
        self.assertFalse(shop_reputation.is_bad_shop('条目太少店'))
        self.assertFalse(shop_reputation.is_bad_shop(''))

    def test_judge_drop_坏店收紧_seed边界(self):
        # seed 态下 ratio 0.65 且优惠额超售价（drop 13000 > 7000×1.05）→ 硬规则拦，
        # 正常店与坏店都不放行（优惠不可能大于售价；该区间已被硬规则覆盖）
        ok_norm, _ = self._judge(20000, 7000)
        self.assertFalse(ok_norm)
        ok_bad, _ = self._judge(20000, 7000, shop_bad=True)
        self.assertFalse(ok_bad)

    def test_judge_drop_坏店不收紧_明显正常数据(self):
        # 真优惠（ratio 0.5，drop 100）坏店也放行 —— 不误伤
        ok_norm, _ = self._judge(20000, 10000)
        ok_bad, _ = self._judge(20000, 10000, shop_bad=True)
        self.assertTrue(ok_norm)
        self.assertTrue(ok_bad)

    def test_judge_drop_坏店收紧_高价段比值(self):
        # 构造高价段（promo >= P97 上沿）：reserve/promo 恰好等于 cap → 正常店放行
        payload = dc.get_calibration().get('paper') or {}
        high_log = payload.get('high_log')
        cap = payload.get('high_reserve_ratio_cap')
        if high_log is None or cap is None:
            self.skipTest('校准表缺高价段参数（测试环境未校准）')
        promo = int(10 ** high_log) + 1
        reserve_ok = int(promo * cap) - 1
        ok_norm, _ = dc.judge_drop('paper', reserve_ok, promo, shop_bad=False)
        self.assertTrue(ok_norm)
        ok_bad, _ = dc.judge_drop('paper', reserve_ok, promo, shop_bad=True)
        self.assertFalse(ok_bad)   # 坏店 cap × 0.7 → 边界超出

    def test_admission_坏店无优惠条目不展示(self):
        shop_reputation.set_shop_stats({'瞎搞小店': [4, 3]})
        row = _row('瞎搞小店', 59900, 1171200)
        row['display_quote'] = {'kind': 'official_promotion_observation', 'amount_cents': 0}
        result = product_search.promotion_admission(row, None)
        self.assertFalse(result.get('eligible'))

    def test_admission_坏店可验证真优惠保留(self):
        shop_reputation.set_shop_stats({'瞎搞小店': [4, 3]})
        row = _row('瞎搞小店', 9900, 19900)
        # 该条能形成可验证优惠（mock）→ 坏店也不误伤
        import unittest.mock as mock
        with mock.patch.object(
                product_search, 'official_promotion_discount',
                return_value={'amount_cents': 5000, 'rate_basis_points': 5000}):
            result = product_search.promotion_admission(row, None)
        self.assertTrue(result.get('eligible'))


if __name__ == '__main__':
    unittest.main()
