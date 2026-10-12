import unittest
from unittest.mock import patch

from services import search_result_cache


class SearchResultCacheTests(unittest.TestCase):
    def setUp(self):
        search_result_cache.clear()

    def tearDown(self):
        search_result_cache.clear()

    def test_returns_result_inside_short_ttl(self):
        value = {'all_rows': ['prepared-once']}
        with patch('services.search_result_cache.monotonic', side_effect=(100.0, 105.0)):
            search_result_cache.put(('same-filter',), value)
            self.assertIs(search_result_cache.get(('same-filter',)), value)

    def test_expired_result_is_not_reused(self):
        with patch('services.search_result_cache.monotonic', side_effect=(100.0, 110.0)):
            search_result_cache.put(('same-filter',), {'all_rows': []})
            self.assertIsNone(search_result_cache.get(('same-filter',)))

    def test_keeps_only_bounded_recent_filters(self):
        with patch('services.search_result_cache.monotonic', return_value=100.0):
            for index in range(search_result_cache.MAX_ENTRIES + 1):
                search_result_cache.put((index,), {'index': index})
            self.assertIsNone(search_result_cache.get((0,)))
            self.assertEqual(search_result_cache.get((search_result_cache.MAX_ENTRIES,)), {
                'index': search_result_cache.MAX_ENTRIES,
            })


if __name__ == '__main__':
    unittest.main()
