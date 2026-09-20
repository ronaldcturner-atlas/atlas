from datetime import date, datetime, timedelta
from types import SimpleNamespace

from django.test import SimpleTestCase

from .models import ScheduleRequest
from .optimizer import _fixed_shift_on_request_workload_floor


class _RelatedTemplates:
    def __init__(self, *template_ids):
        self.templates = [SimpleNamespace(id=template_id) for template_id in template_ids]

    def all(self):
        return self.templates


class FixedRequestDiagnosticTests(SimpleTestCase):
    def test_fixed_shift_on_hours_prove_unavoidable_workload_floor(self):
        instances = []
        next_id = 1

        def add_instance(day, template_id, hours):
            nonlocal next_id
            start = datetime(2027, 1, day, 7)
            instance = SimpleNamespace(
                id=next_id,
                date=date(2027, 1, day),
                shift_template_id=template_id,
                facility_id=1,
                start_datetime=start,
                end_datetime=start + timedelta(hours=hours),
                schedule_block=None,
            )
            instances.append(instance)
            next_id += 1
            return instance

        # Establish the complete January workload window.
        add_instance(1, 999, 8)
        requested = []
        requested.extend(add_instance(day, 100 + day, hours) for day, hours in ((11, 12), (12, 12)))
        requested.extend(add_instance(day, 100 + day, 8) for day in range(13, 18))
        requested.extend(add_instance(day, 100 + day, 12) for day in range(25, 32))

        physician = SimpleNamespace(id=7, display_name='Verplancken')
        requests_by_physician_date = {}
        for request_id, instance in enumerate(requested, start=1):
            request = SimpleNamespace(
                id=request_id,
                weight=ScheduleRequest.Weight.FIXED,
                request_type=ScheduleRequest.RequestType.SHIFT_ON,
                shift_templates=_RelatedTemplates(instance.shift_template_id),
            )
            requests_by_physician_date[(physician.id, instance.date)] = [request]

        targets = {
            physician.id: {
                'rules': [{
                    'period_type': 'MONTH',
                    'units': 'HOURS',
                    'min_value': 100,
                    'max_value': 140,
                    'min_penalty_weight': 10000,
                    'max_penalty_weight': 10000,
                }],
            },
        }
        state = {instance.id: [physician.id] for instance in requested}

        diagnostic = _fixed_shift_on_request_workload_floor(
            instances,
            [physician],
            targets,
            requests_by_physician_date,
            {physician.id: {1}},
            state=state,
        )

        self.assertEqual(diagnostic['fixed_shift_on_request_count'], 14)
        self.assertEqual(diagnostic['resolved_request_count'], 14)
        self.assertEqual(diagnostic['unsatisfied_request_count'], 0)
        self.assertTrue(diagnostic['all_resolved_requests_satisfied'])
        self.assertEqual(diagnostic['score_floor'], 80000.0)
        self.assertEqual(len(diagnostic['floor_rows']), 1)
        self.assertEqual(diagnostic['floor_rows'][0]['fixed_request_required_value'], 148.0)
        self.assertEqual(diagnostic['floor_rows'][0]['effective_max_value'], 140.0)

    def test_ambiguous_request_is_reported_but_not_claimed_as_floor(self):
        first = SimpleNamespace(
            id=1, date=date(2027, 1, 1), shift_template_id=10, facility_id=1,
            start_datetime=datetime(2027, 1, 1, 7),
            end_datetime=datetime(2027, 1, 1, 15), schedule_block=None,
        )
        second = SimpleNamespace(**{**first.__dict__, 'id': 2})
        physician = SimpleNamespace(id=7, display_name='Verplancken')
        request = SimpleNamespace(
            id=1,
            weight=ScheduleRequest.Weight.FIXED,
            request_type=ScheduleRequest.RequestType.SHIFT_ON,
            shift_templates=_RelatedTemplates(10),
        )
        diagnostic = _fixed_shift_on_request_workload_floor(
            [first, second], [physician], {physician.id: {'rules': []}},
            {(physician.id, first.date): [request]}, {physician.id: {1}},
        )
        self.assertEqual(diagnostic['unresolved_request_count'], 1)
        self.assertEqual(diagnostic['score_floor'], 0.0)
        self.assertEqual(
            diagnostic['unresolved_requests'][0]['reason'],
            'ambiguous_matching_shift_instances',
        )

    def test_manual_only_request_uses_request_authority_not_optimizer_eligibility(self):
        instance = SimpleNamespace(
            id=1, date=date(2027, 1, 1), shift_template_id=10, facility_id=1,
            start_datetime=datetime(2027, 1, 1, 19),
            end_datetime=datetime(2027, 1, 2, 7), schedule_block=None,
        )
        physician = SimpleNamespace(id=7, display_name='Verplancken')
        request = SimpleNamespace(
            id=1,
            weight=ScheduleRequest.Weight.FIXED,
            request_type=ScheduleRequest.RequestType.SHIFT_ON,
            shift_templates=_RelatedTemplates(10),
        )

        diagnostic = _fixed_shift_on_request_workload_floor(
            [instance], [physician], {physician.id: {'rules': []}},
            {(physician.id, instance.date): [request]},
            {physician.id: set()},
            state={instance.id: [physician.id]},
            manual_assignment_only_physician_ids={physician.id},
        )

        self.assertEqual(diagnostic['resolved_request_count'], 1)
        self.assertEqual(diagnostic['unresolved_request_count'], 0)
        self.assertEqual(diagnostic['unsatisfied_request_count'], 0)
        self.assertTrue(diagnostic['all_resolved_requests_satisfied'])
