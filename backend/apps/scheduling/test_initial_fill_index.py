from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
import random

from django.test import SimpleTestCase

from .optimizer import (
    _AssignmentIntervalIndex, _can_assign_in_state, _intervals_for_physician,
    _overlaps, _rest_violation,
)


class InitialFillIntervalIndexTests(SimpleTestCase):
    def test_index_matches_linear_checks_with_overlapping_fixed_intervals(self):
        rng = random.Random(8701)
        origin = datetime(2032, 1, 1, tzinfo=timezone.utc)
        index = _AssignmentIntervalIndex()
        intervals = []
        for _ in range(80):
            start = origin + timedelta(hours=rng.randrange(0, 8760))
            interval = (start, start + timedelta(hours=rng.choice([8, 12, 24, 100])))
            intervals.append(interval)
            index.append(interval)
            for _ in range(20):
                start = origin + timedelta(hours=rng.randrange(-24, 8800))
                candidate = SimpleNamespace(
                    start_datetime=start, end_datetime=start + timedelta(hours=12),
                )
                rest = Decimal(rng.choice([0, 8, 10, 12, 24]))
                self.assertEqual(
                    index.conflicts(candidate, rest),
                    _overlaps(candidate, intervals) or _rest_violation(candidate, intervals, rest),
                )

    def test_cached_intervals_match_state_scan_across_rest_and_overlap_boundaries(self):
        origin = datetime(2032, 1, 1, tzinfo=timezone.utc)
        instances = {
            index: SimpleNamespace(
                id=index, facility_id=1,
                start_datetime=origin + timedelta(hours=hours),
                end_datetime=origin + timedelta(hours=hours + duration),
            )
            for index, (hours, duration) in enumerate([
                (0, 12), (36, 8), (72, 12),
            ], start=1)
        }
        state = defaultdict(list, {1: [1], 2: [2], 3: [1]})
        intervals = _intervals_for_physician(state, instances, 1)
        interval_index = _AssignmentIntervalIndex()
        for interval in intervals:
            interval_index.append(interval)
        for hours in range(-24, 109):
            candidate = SimpleNamespace(
                id=99, facility_id=1,
                start_datetime=origin + timedelta(hours=hours),
                end_datetime=origin + timedelta(hours=hours + 8),
            )
            for rest in (0, 8, 12):
                with self.subTest(hours=hours, rest=rest):
                    args = (state, instances, candidate, 1, {1: {1}}, {1: Decimal(rest)})
                    self.assertEqual(
                        _can_assign_in_state(*args),
                        _can_assign_in_state(*args, assigned_intervals=intervals),
                    )
                    self.assertEqual(
                        _can_assign_in_state(*args),
                        _can_assign_in_state(*args, assigned_intervals=interval_index),
                    )

    def test_empty_interval_cache_still_enforces_membership_and_facility(self):
        candidate = SimpleNamespace(id=1, facility_id=2)
        state = defaultdict(list, {1: [1]})
        self.assertFalse(_can_assign_in_state(
            state, {}, candidate, 1, {1: {2}}, {1: Decimal(8)}, assigned_intervals=[],
        ))
        state[1] = []
        self.assertFalse(_can_assign_in_state(
            state, {}, candidate, 1, {1: {1}}, {1: Decimal(8)}, assigned_intervals=[],
        ))
