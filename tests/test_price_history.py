import unittest
from datetime import datetime, timedelta, timezone

from price_history import assess


class PublicPriceHistoryTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)

    def current(self, cents=400):
        return dict(state='public_price_observed', price_cents=cents, currency='CNY',
                    availability='https://schema.org/InStock',
                    checked_at=self.now - timedelta(minutes=1),
                    next_check_at=self.now + timedelta(minutes=4))

    def history(self, cents_by_day):
        return [dict(price_cents=cents, currency='CNY',
                     availability='InStock',
                     checked_at=self.now - timedelta(days=days, hours=1))
                for days, cents in cents_by_day]

    def test_no_page_price_is_not_replaced_by_source_claim(self):
        result = assess(None, self.history([(days, 1000) for days in range(1, 31)]), self.now)
        self.assertEqual(result['state'], 'no_current_price')
        self.assertIsNone(result['reference_delta_cents'])

    def test_requires_thirty_distinct_observation_days(self):
        history = self.history([(days, 1000) for days in range(1, 30)])
        history += self.history([(1, 800)] * 20)  # Poll volume on one day cannot fake 30 days.
        result = assess(self.current(), history, self.now)
        self.assertEqual(result['state'], 'collecting_history')
        self.assertEqual(result['sample_days'], 29)

    def test_p10_and_median_rule_emits_only_historical_low_candidate(self):
        history = self.history([(days, 500 if days <= 3 else 1000) for days in range(1, 31)])
        result = assess(self.current(400), history, self.now)
        self.assertEqual(result['state'], 'historical_low_candidate')
        self.assertEqual(result['p10_cents'], 500)
        self.assertEqual(result['median_cents'], 1000)
        self.assertEqual(result['reference_delta_cents'], 600)

        result = assess(self.current(600), history, self.now)
        self.assertEqual(result['state'], 'not_historically_low')

    def test_expired_or_non_cny_public_price_cannot_be_a_candidate(self):
        expired = self.current()
        expired['next_check_at'] = self.now - timedelta(seconds=1)
        history = self.history([(days, 1000) for days in range(1, 31)])
        self.assertEqual(assess(expired, history, self.now)['state'], 'stale_current_price')
        wrong_currency = self.current()
        wrong_currency['currency'] = 'USD'
        self.assertEqual(assess(wrong_currency, history, self.now)['state'], 'stale_current_price')

    def test_unknown_or_out_of_stock_status_cannot_be_a_candidate(self):
        history = self.history([(days, 1000) for days in range(1, 31)])
        current = self.current()
        current['availability'] = ''
        self.assertEqual(assess(current, history, self.now)['state'], 'availability_unconfirmed')
        current['availability'] = 'https://schema.org/OutOfStock'
        self.assertEqual(assess(current, history, self.now)['state'], 'availability_unconfirmed')


if __name__ == '__main__':
    unittest.main()
