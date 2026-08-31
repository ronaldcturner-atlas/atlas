from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from . import optimizer


class PeriodWindowCacheTests(SimpleTestCase):
    def tearDown(self):
        optimizer._period_windows_for_dates.cache_clear()

    def test_matches_uncached_windows_across_block_lengths_and_sparse_dates(self):
        for days in (1, 31, 61, 183, 366):
            block = SimpleNamespace(start_date=date(2031, 12, 17),
                end_date=date(2031, 12, 17) + timedelta(days=days - 1))
            dates = [block.start_date + timedelta(days=i) for i in range(0, days, 3)]
            instances = [SimpleNamespace(date=d, schedule_block=block) for d in dates * 2]
            for period in ('WEEK', 'MONTH', 'SCHEDULE_BLOCK'):
                expected = sorted({optimizer._period_contains_date(period,
                    block.start_date, block.end_date, d) for d in dates})
                self.assertEqual(optimizer._period_windows(instances, period), expected)
                self.assertEqual(optimizer._period_windows(list(reversed(instances)), period), expected)

    def test_reuses_work_but_does_not_reuse_mutable_results_or_stale_dates(self):
        optimizer._period_windows_for_dates.cache_clear()
        block = SimpleNamespace(start_date=date(2032, 1, 15), end_date=date(2032, 3, 12))
        instances = [SimpleNamespace(date=date(2032, 1, 16), schedule_block=block)]
        original = optimizer._period_contains_date
        with patch.object(optimizer, '_period_contains_date', wraps=original) as calculate:
            result = optimizer._period_windows(instances, 'MONTH')
            result.clear()
            self.assertEqual(optimizer._period_windows(instances, 'MONTH'),
                             [(date(2032, 1, 15), date(2032, 1, 31))])
            self.assertEqual(calculate.call_count, 1)
            block.start_date = date(2032, 1, 1)
            self.assertEqual(optimizer._period_windows(instances, 'MONTH')[0][0], date(2032, 1, 1))
            instances[0].date = date(2032, 2, 29)
            self.assertEqual(optimizer._period_windows(instances, 'MONTH'),
                             [(date(2032, 2, 1), date(2032, 2, 29))])
            self.assertEqual(calculate.call_count, 3)

    def test_empty_and_no_block_fallback(self):
        self.assertEqual(optimizer._period_windows([], 'MONTH'), [])
        instances = [SimpleNamespace(date=date(2032, 2, 29)), SimpleNamespace(date=date(2032, 3, 2))]
        self.assertEqual(optimizer._period_windows(instances, 'MONTH'),
            [(date(2032, 2, 29), date(2032, 2, 29)), (date(2032, 3, 1), date(2032, 3, 2))])
