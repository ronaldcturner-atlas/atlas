from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from . import optimizer
from .management.commands.benchmark_optimizer_generalization import ScreeningRunner
from .management.commands.benchmark_optimizer_scale import SCALE_PROFILES


class ReferenceNightWindowIndex:
    def __init__(self, instances, state):
        self.instances = {i.id: i for i in instances}
        self.state = state

    def count(self, physician_id, start, end):
        return optimizer._night_count_for_window(self.instances, self.state, physician_id, start, end)


class NightWindowIndexTests(SimpleTestCase):
    def test_counts_match_reference_including_duplicates_and_boundaries(self):
        instances = [SimpleNamespace(id=i, date=date(2032, 1, 1) + timedelta(days=i // 2),
            shift_template=SimpleNamespace(night_shift=i % 3 != 0)) for i in range(732)]
        state = {i.id: [i.id % 5, (i.id + 1) % 5, i.id % 5] for i in instances}
        state[9999] = [0]  # Unknown shifts are ignored by both implementations.
        index = optimizer._NightWindowIndex(instances, state)
        reference = ReferenceNightWindowIndex(instances, state)
        for physician in range(7):
            for start, end in ((date(2031, 12, 25), date(2032, 1, 1)),
                               (date(2032, 2, 29), date(2032, 2, 29)),
                               (date(2032, 1, 1), date(2032, 12, 31)),
                               (date(2032, 3, 1), date(2032, 2, 1))):
                self.assertEqual(index.count(physician, start, end), reference.count(physician, start, end))


class NightWindowReportTests(TestCase):
    def test_full_reports_equal_and_rebuild_after_assignment_changes(self):
        runner = ScreeningRunner(starting_point='partial', scenario='mixed')
        version = runner._build_fixture(SCALE_PROFILES[1], shifts_per_day=2,
            staffing_per_shift=2, scenario='mixed')
        for changed in (False, True):
            if changed:
                runner.source.assignments.filter(shift_instance__shift_template__night_shift=True).delete()
            actual = optimizer.build_violation_report(version, runner.source)
            with patch.object(optimizer, '_NightWindowIndex', ReferenceNightWindowIndex):
                expected = optimizer.build_violation_report(version, runner.source)
            self.assertEqual(actual, expected)
