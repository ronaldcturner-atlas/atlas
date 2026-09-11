from decimal import Decimal
from datetime import date
from collections import defaultdict
from types import SimpleNamespace

from django.test import SimpleTestCase

from .models import ScheduleRequest
from .optimizer import (
    _request_candidate_rank,
    _fixed_request_on_unmet_count,
    _optimization_priority,
    _request_on_repair_candidates,
    _request_repair_candidates,
    _request_score,
)


class FakeRelatedManager:
    def __init__(self, values):
        self.values = values

    def all(self):
        return self.values


class RequestScoringTests(SimpleTestCase):
    def setUp(self):
        self.contract = SimpleNamespace(request_settings={
            'weight_high': '35000',
        })

    def request(self, request_type):
        return SimpleNamespace(
            request_type=request_type,
            weight=ScheduleRequest.Weight.HIGH,
        )

    def test_fulfilled_request_on_never_reduces_official_penalty(self):
        for request_type in (
            ScheduleRequest.RequestType.DAY_ON,
            ScheduleRequest.RequestType.SHIFT_ON,
        ):
            score, violations, rewards = _request_score(
                [self.request(request_type)], self.contract,
            )
            self.assertEqual(score, Decimal('0'))
            self.assertEqual(violations, 0)
            self.assertEqual(rewards, 0)

    def test_request_on_still_receives_initial_fill_preference(self):
        requested = self.request(ScheduleRequest.RequestType.SHIFT_ON)
        self.assertEqual(
            _request_candidate_rank([requested], self.contract),
            Decimal('-17500'),
        )
        self.assertEqual(_request_candidate_rank([], self.contract), Decimal('0'))

    def test_request_off_remains_a_positive_penalty(self):
        request = self.request(ScheduleRequest.RequestType.DAY_OFF)
        score, violations, rewards = _request_score([request], self.contract)
        self.assertEqual(score, Decimal('35000'))
        self.assertEqual(violations, 1)
        self.assertEqual(rewards, 0)

    def test_unmet_shift_on_creates_reassignment_candidate(self):
        shift_date = date(2026, 12, 3)
        template = SimpleNamespace(id=12, generated_name=lambda: 'Northwoods 7p-7a')
        instance = SimpleNamespace(id=40, date=shift_date, shift_template_id=template.id)
        request = SimpleNamespace(
            id=50,
            physician_id=2,
            physician=SimpleNamespace(id=2, display_name='Requested Physician'),
            date=shift_date,
            request_type=ScheduleRequest.RequestType.SHIFT_ON,
            request_scope=ScheduleRequest.RequestScope.ADMIN,
            weight=ScheduleRequest.Weight.FIXED,
            shift_templates=FakeRelatedManager([template]),
        )
        state = defaultdict(list, {instance.id: [1]})

        candidates = _request_repair_candidates(
            [instance], [request.physician], state, set(),
            {2: self.contract}, {(2, shift_date): [request]},
        )

        self.assertEqual(candidates, [(1, instance.id)])
        self.assertEqual(
            _request_on_repair_candidates(
                [instance], state, set(),
                {2: self.contract}, {(2, shift_date): [request]},
            ),
            [(2, 1, instance.id)],
        )

        state[instance.id] = [2]
        self.assertEqual(
            _request_repair_candidates(
                [instance], [request.physician], state, set(),
                {2: self.contract}, {(2, shift_date): [request]},
            ),
            [],
        )
        self.assertEqual(
            _request_on_repair_candidates(
                [instance], state, set(),
                {2: self.contract}, {(2, shift_date): [request]},
            ),
            [],
        )

    def test_fixed_request_on_outranks_lower_soft_penalty(self):
        self.assertLess(
            _optimization_priority({
                'fixed_request_on_unmet': 0,
                'score': Decimal('200000'),
            }),
            _optimization_priority({
                'fixed_request_on_unmet': 1,
                'score': Decimal('1000'),
            }),
        )

    def test_fixed_request_on_unmet_count_tracks_fulfillment(self):
        shift_date = date(2027, 1, 25)
        template = SimpleNamespace(id=22)
        instance = SimpleNamespace(id=70, date=shift_date, shift_template_id=template.id)
        request = SimpleNamespace(
            request_type=ScheduleRequest.RequestType.SHIFT_ON,
            weight=ScheduleRequest.Weight.FIXED,
            shift_templates=FakeRelatedManager([template]),
        )
        requests = {(2, shift_date): [request]}
        self.assertEqual(
            _fixed_request_on_unmet_count([instance], {instance.id: [1]}, requests),
            1,
        )
        self.assertEqual(
            _fixed_request_on_unmet_count([instance], {instance.id: [2]}, requests),
            0,
        )
