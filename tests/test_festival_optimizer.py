import copy
import json
import tempfile
import unittest
from pathlib import Path

import db
from app import app
from services.festival_optimizer import calculate_basket, example_basket, _allocate, import_product


class FestivalOptimizerTests(unittest.TestCase):
    def setUp(self):
        self.data = example_basket()

    def test_2026_store_exclusion_and_packet(self):
        result = calculate_basket(self.data)
        self.assertEqual(result['best']['total_cents'], 17500)
        self.assertEqual([s['id'] for s in result['best']['steps']], ['R2', 'R3'])
        self.data['stack_pairs'] = [['R1', 'R2']]
        self.assertEqual(calculate_basket(self.data)['best']['total_cents'], 17500)

    def test_same_suborder_shop_rules_exclude_even_disjoint_items(self):
        self.data['items'].append(dict(self.data['items'][0], id='B'))
        a = dict(self.data['rules'][0], threshold='0', value='10')
        b = dict(a, id='Bcoupon', items=['B'])
        self.data['rules'] = [a, b]
        self.assertEqual(calculate_basket(self.data)['best']['saving_cents'], 1000)
        self.data['items'][1]['shop'] = 'other'
        self.assertEqual(calculate_basket(self.data)['best']['saving_cents'], 2000)

    def test_unconfirmed_expired_future_and_event_window(self):
        self.data['rules'][1]['confirmed'] = False
        self.data['rules'][0]['end'] = '2026-10-16T12:00'
        self.data['rules'][2]['start'] = '2026-10-17T12:00'
        result = calculate_basket(self.data)
        self.assertEqual(result['best']['total_cents'], 20000)
        self.assertEqual(len(result['ignored']), 3)
        self.data = example_basket()
        self.data['at'] = '2026-10-14T12:00'
        self.data['rules'] = [self.data['rules'][2]]
        self.data['rules'][0]['start'] = '2026-10-01T00:00'
        self.assertEqual(calculate_basket(self.data)['best']['total_cents'], 20000)

    def test_exact_selection_beats_greedy_coupon(self):
        first = self.data['rules'][0]
        first.update(value='60', threshold='0')
        platform = dict(first, id='P', name='满200减100平台券', kind='platform_coupon', value='100', threshold='200', basis='current')
        self.data['rules'] = [first, platform]
        self.data['stack_pairs'] = [['R1', 'P']]
        result = calculate_basket(self.data)
        self.assertEqual(result['best']['total_cents'], 10000)
        self.assertEqual([s['id'] for s in result['best']['steps']], ['P'])

    def test_unknown_stacking_is_not_assumed(self):
        first = self.data['rules'][0]
        platform = dict(first, id='P', name='平台券', kind='platform_coupon', value='20', threshold='0')
        self.data['rules'] = [first, platform]
        self.assertEqual(calculate_basket(self.data)['best']['total_cents'], 18000)
        self.data['stack_pairs'] = [['R1', 'P']]
        self.assertEqual(calculate_basket(self.data)['best']['total_cents'], 17000)

    def test_shipping_is_not_discounted_and_counted_once(self):
        self.data['items'][0]['shipping'] = '6'
        self.data['items'].append(dict(self.data['items'][0], id='B', quantity=1, price='20'))
        self.data['rules'] = [dict(self.data['rules'][2], value='999')]
        result = calculate_basket(self.data)
        self.assertEqual(result['best']['total_cents'], 2600)
        self.assertEqual(result['best']['shipping_cents'], 600)
        self.data['items'][1]['shipping'] = ''
        with self.assertRaises(ValueError):
            calculate_basket(self.data)

    def test_threshold_packets_only_one_and_total_max_ten(self):
        packet = self.data['rules'][2]
        self.data['rules'] = [dict(packet, id=f'R{i}') for i in range(12)]
        result = calculate_basket(self.data)
        self.assertEqual(len(result['best']['steps']), 10)
        self.assertEqual(result['best']['total_cents'], 15000)
        self.data['rules'] = [dict(packet, id='a', threshold='50'), dict(packet, id='b', threshold='100', value='10')]
        self.assertEqual(calculate_basket(self.data)['best']['total_cents'], 19000)

    def test_minimum_quantity_repeat_and_cap(self):
        rule = self.data['rules'][1]
        self.data['rules'] = [rule]
        self.data['items'][0]['quantity'] = 1
        self.assertEqual(calculate_basket(self.data)['best']['total_cents'], 10000)
        self.data['items'][0]['quantity'] = 5
        rule.update(mode='repeat', threshold='100', value='20', cap='60')
        self.assertEqual(calculate_basket(self.data)['best']['total_cents'], 44000)

    def test_official_path_is_indivisible_and_quantity_bound(self):
        path = dict(self.data['rules'][0], kind='official_path', id='path', threshold='0', value='20')
        self.data['rules'] = [path, self.data['rules'][2]]
        self.data['items'][0]['quantity'] = 1
        self.assertEqual(calculate_basket(self.data)['best']['total_cents'], 8000)
        self.data['items'][0]['quantity'] = 2
        self.assertEqual(calculate_basket(self.data)['best']['total_cents'], 19500)

    def test_every_cent_conserved(self):
        balances = [1, 1, 1]
        _allocate(balances, [0, 1, 2], 2)
        self.assertEqual(sum(balances), 1)
        self.assertEqual(balances, [0, 0, 1])

    def test_invalid_input_and_limits(self):
        for key, value in [('items', []), ('rules', self.data['rules'] * 5), ('stack_pairs', [['bad', 'R1']])]:
            bad = copy.deepcopy(self.data); bad[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                calculate_basket(bad)
        for key, value in [('quantity', True), ('price', 'NaN'), ('shipping', ''), ('price', '-1'), ('price', '1.001')]:
            bad = copy.deepcopy(self.data); bad['items'][0][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                calculate_basket(bad)
        self.data['rules'][1]['id'] = 'R1'
        with self.assertRaisesRegex(ValueError, '重复'):
            calculate_basket(self.data)


class FestivalOptimizerRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original_path = db.DB_PATH
        db.DB_PATH = Path(self.temp.name) / 'optimizer.sqlite3'
        db.initialize()
        self.client = app.test_client()
        self.addCleanup(self.restore)

    def restore(self):
        db.DB_PATH = self.original_path
        self.temp.cleanup()

    def test_route_post_csrf_example_and_invalid(self):
        page = self.client.get('/festival-promotions?example=1')
        self.assertEqual(page.status_code, 200)
        self.assertIn('¥175.00', page.get_data(as_text=True))
        with self.client.session_transaction() as session:
            token = session['csrf']
        response = self.client.post('/festival-promotions', data={'csrf': token, 'basket_json': json.dumps(example_basket())})
        self.assertIn('¥175.00', response.get_data(as_text=True))
        self.assertIn('模拟示例', response.get_data(as_text=True))
        response = self.client.post('/festival-promotions', data={'csrf': token, 'basket_json': '{bad'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('role="alert"', response.get_data(as_text=True))
        self.assertEqual(self.client.post('/festival-promotions', data={'basket_json': '{}'}).status_code, 302)

    def test_import_missing_product_and_login_required(self):
        with db.connect() as conn:
            with self.assertRaises(ValueError):
                import_product(conn, 99999)
        page = self.client.get('/festival-promotions?product_id=bad')
        self.assertIn('数字编号', page.get_data(as_text=True))
        client = app.test_client(authenticated=False)
        self.assertEqual(client.get('/festival-promotions').status_code, 302)
