import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import db
from app import app
from services.double11_sync import refresh, snapshot


RULE_HTML = '''<html><head><title>26年双11超级红包消费者规则</title></head><body>
<p>2026年10月15日10:00:00至2026年11月13日23:59:50 手机号 实名认证</p>
<p>二、天天开红包规则 每天可参与1次</p>
<p>三、攒能量值领红包规则 全周期最多领取3个超级红包</p>
<p>四、逛会场领淘礼金规则 领取后24小时内可使用</p>
<p>五、限时加码红包规则 场次安排不固定</p>
<p>四、粉丝专享金币加抵规则 粉丝抵</p>
<p>五、邀好友抢最高100元红包 红包需要主动领取</p>
<p>六、惊喜红包规则 数量有限</p>
<p>七、红包猜猜猜规则 猜幸运数字</p>
<p>优惠券、购物津贴、红包 二阶段红包使用待后续更新</p>
</body></html>'''


class Double11SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old = db.DB_PATH
        db.DB_PATH = Path(self.temp.name) / 'test.sqlite3'
        db.initialize()
        self.addCleanup(self.restore)

    def restore(self):
        db.DB_PATH = self.old
        self.temp.cleanup()

    def test_sync_rules_and_current_year_name_still_requires_attribution(self):
        old = {'material_id':'98168','material_name':'双11多年通用物料',
               'start_time':'2024-10-01 00:00:00','end_time':'2029-11-01 00:00:00'}
        current = {'material_id':'123456','material_name':'2026双11官方商品货盘',
                   'start_time':'2026-10-12 00:00:00','end_time':'2026-11-13 23:59:50'}
        response = Mock(text=RULE_HTML, content=RULE_HTML.encode())
        response.raise_for_status.return_value = None
        now = datetime(2026, 10, 12, tzinfo=timezone.utc)
        result = refresh(now=now, get=lambda *a, **k: response, fetch=lambda material_type=1:[old,current] if material_type == 1 else [])
        self.assertEqual(result['current_year_materials'], 1)
        with db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM festival_material_feeds').fetchone()[0], 0)
            self.assertEqual(snapshot(conn)['rules']['activities'][0]['name'], '天天开红包')
        self.assertIsNone(refresh(due_only=True, now=now+timedelta(minutes=10),
                                  get=lambda *a, **k: self.fail('unexpected fetch'),
                                  fetch=lambda **kwargs: self.fail('unexpected fetch')))
        refresh(now=now+timedelta(hours=1), get=lambda *a, **k: response, fetch=lambda material_type=1:[old,current] if material_type == 1 else [])
        with db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM festival_material_feeds').fetchone()[0], 0)
        html = app.test_client().get('/festival-promotions').get_data(as_text=True)
        self.assertIn('2026 双十一 · 官方活动与参加办法', html)
        self.assertIn('2026双11官方商品货盘', html)
        self.assertIn('未标明2026归属', html)
        self.assertIn('归属仍待核实', html)

    def test_bad_rules_keep_previous_success(self):
        response = Mock(text=RULE_HTML, content=RULE_HTML.encode())
        response.raise_for_status.return_value = None
        refresh(get=lambda *a, **k: response, fetch=lambda **kwargs:[])
        broken = Mock(text='<title>未知规则</title>', content=b'broken')
        broken.raise_for_status.return_value = None
        result = refresh(get=lambda *a, **k: broken, fetch=lambda **kwargs:[])
        self.assertIn('rules', result['errors'])
        with db.connect() as conn:
            saved = snapshot(conn)
            self.assertEqual(saved['rules']['title'], '26年双11超级红包消费者规则')
            self.assertTrue(saved['rules_error'])


if __name__ == '__main__':
    unittest.main()
