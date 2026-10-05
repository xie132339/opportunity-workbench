import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import db
import scanner
import xianyu
from app import app


class XianyuBridgeTests(unittest.TestCase):
    def test_account_state_requires_real_goofish_domain(self):
        for content in ('not-json', '{"cookies": []}',
                        '{"cookies": [{"domain": "evilgoofish.com"}]}'):
            with self.subTest(content=content), self.assertRaises(xianyu.XianyuError):
                xianyu.import_account('mine', content)

    def test_valid_account_state_is_sent_only_to_local_account_api(self):
        calls = []

        def fake_api(method, path, payload=None):
            calls.append((method, path, payload))
            return [] if method == 'GET' else {}

        with patch('xianyu.api', side_effect=fake_api):
            xianyu.import_account('mine', '{"cookies": [{"domain": ".goofish.com"}]}')
        self.assertEqual(calls[1][0:2], ('POST', '/api/accounts'))
        self.assertEqual(calls[1][2]['name'], 'mine')

    def test_task_rejects_inverted_price_before_creating(self):
        with patch('xianyu.api', return_value=[{'name': 'mine', 'path': 'state/mine.json'}]) as api:
            with self.assertRaisesRegex(xianyu.XianyuError, '最低价'):
                xianyu.create_task({'task_name': '手机', 'keyword': '手机', 'account': 'mine',
                                    'min_price': '8000', 'max_price': '2000'})
            api.assert_called_once_with('GET', '/api/accounts')

    def test_task_uses_keyword_mode_and_selected_account(self):
        calls = []

        def fake_api(method, path, payload=None):
            calls.append((method, path, payload))
            return [{'name': 'mine', 'path': 'state/mine.json'}] if method == 'GET' else {}

        with patch('xianyu.api', side_effect=fake_api):
            xianyu.create_task({'task_name': '纸品', 'keyword': '抽纸', 'account': 'mine',
                                'max_pages': '2', 'new_publish_option': '1天内'})
        payload = calls[-1][2]
        self.assertEqual(payload['decision_mode'], 'keyword')
        self.assertEqual(payload['account_state_file'], 'state/mine.json')
        self.assertEqual(payload['keyword_rules'], ['抽纸'])
        self.assertEqual(payload['max_pages'], 2)

    def test_update_can_pause_and_rejects_inverted_range(self):
        task = {'decision_mode': 'keyword', 'keyword': '抽纸'}
        with patch('xianyu.api') as api:
            xianyu.update_task(7, task, {'max_pages': '2', 'min_price': '10',
                                         'max_price': '20'})
            args = api.call_args
            self.assertEqual(args.args[:2], ('PATCH', '/api/tasks/7'))
            self.assertFalse(args.args[2]['enabled'])
            api.reset_mock()
            with self.assertRaisesRegex(xianyu.XianyuError, '最低价'):
                xianyu.update_task(7, task, {'max_pages': '2', 'min_price': '80',
                                             'max_price': '20'})
            api.assert_not_called()

    def test_goofish_rows_only_accept_explicit_publication_time(self):
        def record(item_id, published):
            return {'商品信息': {'商品ID': item_id, '商品标题': '一款闲鱼商品',
                               '商品链接': f'https://www.goofish.com/item?id={item_id}',
                               '发布时间': published}}

        response = Mock()
        response.status_code = 200
        response.is_redirect = False
        response.json.return_value = {'items': [record('1', '2026-10-03 12:00'),
                                                record('2', '3小时前')]}
        with patch('scanner.requests.get', return_value=response):
            rows = scanner._goofish_rows('http://127.0.0.1:8000/api/results/phone_full_data.jsonl')
        self.assertEqual(rows[0][4], '2026-10-03 04:00:00')
        self.assertIsNone(rows[1][4])


class XianyuPageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db.DB_PATH = Path(self.tmp.name) / 'workbench.sqlite3'
        db.initialize()
        self.client = app.test_client()

    def tearDown(self):
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    def token(self):
        with self.client.session_transaction() as session:
            return session['csrf']

    def test_unified_page_and_csrf(self):
        with patch('routes.xianyu.xianyu_snapshot', return_value=([], [], [])):
            page = self.client.get('/xianyu')
        self.assertEqual(page.status_code, 200)
        self.assertIn('闲鱼账号与搜索任务'.encode(), page.data)
        self.assertNotIn(b'127.0.0.1:8000/', page.data)
        self.assertEqual(self.client.post('/xianyu/tasks', data={}).status_code, 400)

    def test_populated_page_shows_edit_and_result_sync(self):
        account = {'name': 'mine', 'path': 'state/mine.json'}
        task = {'id': 7, 'task_name': '二手手机', 'keyword': '手机',
                'min_price': '100', 'max_price': '200', 'region': '',
                'new_publish_option': '1天内', 'account_state_file': 'state/mine.json',
                'is_running': False, 'enabled': True, 'decision_mode': 'keyword',
                'max_pages': 3, 'keyword_rules': ['手机'], 'personal_only': True,
                'free_shipping': False}
        with patch('routes.xianyu.xianyu_snapshot', return_value=([account], [task], ['phone_full_data.jsonl'])):
            page = self.client.get('/xianyu')
        self.assertEqual(page.status_code, 200)
        self.assertIn('编辑条件'.encode(), page.data)
        self.assertIn('接入并检查'.encode(), page.data)
        self.assertNotIn(b'127.0.0.1:8000/api/', page.data)

    def test_backend_down_is_visible_without_fake_accounts(self):
        with patch('routes.xianyu.xianyu_snapshot', side_effect=xianyu.XianyuError('闲鱼采集服务未运行')):
            page = self.client.get('/xianyu')
        self.assertEqual(page.status_code, 200)
        self.assertIn('闲鱼采集服务未运行'.encode(), page.data)
        self.assertIn('创建搜索任务'.encode(), page.data)
        self.assertIn(b'disabled', page.data)

    def test_result_sync_requires_real_file_then_registers_once(self):
        with patch('routes.xianyu.xianyu_snapshot', return_value=([], [], [])):
            self.client.get('/xianyu')
        token = self.token()
        with patch('routes.xianyu.xianyu_api', return_value={'files': []}), patch('routes.xianyu.scan_source') as scan:
            self.client.post('/xianyu/results', data={'csrf': token,
                              'filename': 'phone_full_data.jsonl'})
            scan.assert_not_called()
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sources WHERE method='goofish'").fetchone()[0], 0)

        with patch('routes.xianyu.xianyu_api', return_value={'files': ['phone_full_data.jsonl']}), \
             patch('routes.xianyu.scan_source', return_value={'status': 'healthy', 'seen': 2}) as scan:
            for _ in range(2):
                self.client.post('/xianyu/results', data={'csrf': token,
                                  'filename': 'phone_full_data.jsonl'})
            self.assertEqual(scan.call_count, 2)
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sources WHERE method='goofish'").fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
