from datetime import time

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from rest_framework.test import APIClient

from apps.domains.models import Domain
from apps.facilities.models import Facility

from .models import Contract, SharedRule, ShiftTemplate


class SharedRuleApiTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.scheduler = User.objects.create_user('shared.scheduler', password='x')
        scheduler_group, _ = Group.objects.get_or_create(name='Scheduler')
        self.scheduler.groups.add(scheduler_group)
        self.client = APIClient()
        self.client.force_authenticate(self.scheduler)
        self.domain = Domain.objects.create(name='Shared Physician', active=True)
        self.berkeley = Facility.objects.create(
            name='Berkeley', short_name='BER',
        )
        self.northwoods = Facility.objects.create(
            name='Northwoods', short_name='NW',
        )
        self.four_to_one = ShiftTemplate.objects.create(
            facility=self.berkeley, name='4p-1a',
            start_time=time(16), end_time=time(1),
            active_days_of_week=['Monday'], weekend_days=[],
            default_staffing_count=1,
        )
        self.northwoods_day = ShiftTemplate.objects.create(
            facility=self.northwoods, name='Day',
            start_time=time(7), end_time=time(19),
            active_days_of_week=['Monday'], weekend_days=[],
            default_staffing_count=1,
        )
        self.full = Contract.objects.create(
            domain=self.domain, name='Full Time', active=True,
        )
        self.part = Contract.objects.create(
            domain=self.domain, name='Part Time', active=True,
        )
        self.excluded = Contract.objects.create(
            domain=self.domain, name='No Berkeley', active=True,
        )
        self.full.facilities.add(self.berkeley, self.northwoods)
        self.part.facilities.add(self.berkeley)
        self.excluded.facilities.add(self.northwoods)

    def payload(self, contract_ids=None):
        contract_ids = contract_ids or [self.full.id, self.part.id]
        return {
            'domain': self.domain.id,
            'name': '4p-1a',
            'active': True,
            'period_type': 'SCHEDULE_BLOCK',
            'units': 'SHIFTS',
            'shift_template_ids': [self.four_to_one.id],
            'contract_settings': [{
                'contract_id': contract_id,
                'enabled': True,
                'min_value': '2' if contract_id == self.part.id else '3',
                'max_value': '3' if contract_id == self.part.id else '4',
                'min_penalty_weight': '10000',
                'max_penalty_weight': '10000',
                'spread_violations': True,
            } for contract_id in contract_ids],
        }

    def test_create_materializes_one_shared_rule_for_each_contract(self):
        response = self.client.post(
            '/api/shared-rules/', self.payload(), format='json',
        )

        self.assertEqual(response.status_code, 201, response.json())
        shared_rule = SharedRule.objects.get()
        self.assertEqual(shared_rule.contract_links.count(), 2)
        self.full.refresh_from_db()
        self.part.refresh_from_db()
        for contract, expected_maximum in ((self.full, '4.00'), (self.part, '3.00')):
            rule = next(
                row for row in contract.shift_settings['rules']
                if row['shared_rule_id'] == shared_rule.id
            )
            self.assertEqual(rule['shift_template_ids'], [self.four_to_one.id])
            self.assertEqual(
                rule['period_rules'][0]['max_value'],
                str(int(float(expected_maximum))),
            )

    def test_incompatible_contract_is_rejected(self):
        response = self.client.post(
            '/api/shared-rules/',
            self.payload([self.full.id, self.excluded.id]),
            format='json',
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('No Berkeley', str(response.json()))
        self.assertFalse(SharedRule.objects.exists())

    def test_invalid_contract_limits_are_rejected_cleanly(self):
        payload = self.payload()
        payload['contract_settings'][0]['min_value'] = '5'
        payload['contract_settings'][0]['max_value'] = '4'

        response = self.client.post(
            '/api/shared-rules/', payload, format='json',
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('minimum greater', str(response.json()))
        self.assertFalse(SharedRule.objects.exists())

    def test_fractional_contract_limits_are_rejected(self):
        payload = self.payload()
        payload['contract_settings'][0]['max_value'] = '4.5'

        response = self.client.post(
            '/api/shared-rules/', payload, format='json',
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('whole numbers', str(response.json()))
        self.assertFalse(SharedRule.objects.exists())

    def test_fractional_local_contract_rule_values_are_rejected(self):
        response = self.client.patch(
            f'/api/contracts/{self.full.id}/',
            {'shift_settings': {'rules': [{
                'label': 'Late shifts',
                'shift_template_ids': [self.four_to_one.id],
                'period_rules': [{
                    'period_type': 'MONTH',
                    'units': 'SHIFTS',
                    'min_value': '2.5',
                    'max_value': '4',
                    'min_penalty_weight': '10000',
                    'max_penalty_weight': '10000',
                }],
            }]}},
            format='json',
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('whole number', str(response.json()))

    def test_active_filter_and_permanent_delete(self):
        created = self.client.post(
            '/api/shared-rules/', self.payload(), format='json',
        )
        shared_rule_id = created.json()['id']
        deactivated_payload = self.payload()
        deactivated_payload['active'] = False
        updated = self.client.patch(
            f'/api/shared-rules/{shared_rule_id}/',
            deactivated_payload,
            format='json',
        )
        self.assertEqual(updated.status_code, 200, updated.json())
        self.assertEqual(self.client.get('/api/shared-rules/').json(), [])
        self.assertEqual(
            [row['id'] for row in self.client.get(
                '/api/shared-rules/?status=inactive',
            ).json()],
            [shared_rule_id],
        )

        deleted = self.client.delete(f'/api/shared-rules/{shared_rule_id}/')

        self.assertEqual(deleted.status_code, 204)
        self.assertFalse(SharedRule.objects.filter(id=shared_rule_id).exists())
        self.full.refresh_from_db()
        self.assertFalse(any(
            row.get('shared_rule_id') == shared_rule_id
            for row in self.full.shift_settings.get('rules', [])
        ))

    def test_contract_cannot_exclude_every_shared_rule_facility(self):
        created = self.client.post(
            '/api/shared-rules/', self.payload(), format='json',
        )
        self.assertEqual(created.status_code, 201, created.json())

        response = self.client.patch(
            f'/api/contracts/{self.full.id}/',
            {'facility_ids': [self.northwoods.id]},
            format='json',
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('4p-1a', str(response.json()))

    def test_duplicate_contract_keeps_shared_rule_membership(self):
        created = self.client.post(
            '/api/shared-rules/', self.payload(), format='json',
        )
        self.assertEqual(created.status_code, 201, created.json())

        response = self.client.post(
            f'/api/contracts/{self.full.id}/duplicate/', {}, format='json',
        )

        self.assertEqual(response.status_code, 201, response.json())
        duplicate = Contract.objects.get(id=response.json()['id'])
        self.assertEqual(duplicate.shared_rule_links.count(), 1)
        self.assertEqual(
            duplicate.shift_settings['rules'][0]['shared_rule_id'],
            created.json()['id'],
        )

    def test_contract_can_remove_local_shift_rules_without_removing_shared_rule(self):
        created = self.client.post(
            '/api/shared-rules/', self.payload(), format='json',
        )
        self.assertEqual(created.status_code, 201, created.json())

        response = self.client.patch(
            f'/api/contracts/{self.full.id}/',
            {'shift_settings': {'rules': []}},
            format='json',
        )

        self.assertEqual(response.status_code, 200, response.json())
        self.full.refresh_from_db()
        self.assertEqual(len(self.full.shift_settings['rules']), 1)
        self.assertEqual(
            self.full.shift_settings['rules'][0]['shared_rule_id'],
            created.json()['id'],
        )

    def test_contract_can_adjust_only_its_shared_rule_limits_and_penalties(self):
        created = self.client.post(
            '/api/shared-rules/', self.payload(), format='json',
        )
        self.assertEqual(created.status_code, 201, created.json())
        shared_rule_id = created.json()['id']

        response = self.client.patch(
            f'/api/contracts/{self.full.id}/',
            {'shared_rule_settings': [{
                'shared_rule_id': shared_rule_id,
                'min_value': '4',
                'max_value': '6',
                'min_penalty_weight': '12000',
                'max_penalty_weight': '18000',
            }]},
            format='json',
        )

        self.assertEqual(response.status_code, 200, response.json())
        shared_row = response.json()['shared_rules'][0]
        self.assertEqual(shared_row['name'], '4p-1a')
        self.assertEqual(shared_row['min_value'], '4')
        self.assertEqual(shared_row['max_value'], '6')
        self.assertEqual(shared_row['min_penalty_weight'], '12000')
        self.assertEqual(shared_row['max_penalty_weight'], '18000')

        self.full.refresh_from_db()
        materialized = self.full.shift_settings['rules'][0]
        self.assertEqual(materialized['label'], '4p-1a')
        self.assertEqual(materialized['period_rules'][0]['min_value'], '4')
        self.assertEqual(
            materialized['period_rules'][0]['max_penalty_weight'],
            '18000',
        )

        self.part.refresh_from_db()
        untouched = self.part.shift_settings['rules'][0]['period_rules'][0]
        self.assertEqual(untouched['min_value'], '2')
        self.assertEqual(untouched['max_value'], '3')
