from collections import defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.db.models import Count
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Physician
from apps.domains.models import Domain
from apps.facilities.models import Facility

from .models import (
    Contract,
    ContractUserAssignment,
    OptimizerRun,
    ScheduleBlock,
    ScheduleRequest,
    ScheduleShiftAssignment,
    ScheduleShiftInstance,
    ScheduleVersion,
    ShiftTemplate,
)
from .optimizer import _night_violation_report, _score_schedule, _validated_night_report_for_current_assignments
from .serializers import ScheduleBlockSerializer


class SchedulingTests(TestCase):
    def test_shift_template_generated_name_uses_facility_short_name(self):
        facility = Facility.objects.create(name='Berkeley Hospital', short_name='Berkeley')
        template = ShiftTemplate.objects.create(
            facility=facility,
            start_time=time(7, 0),
            end_time=time(16, 0),
            active_days_of_week=['Monday'],
            weekend_days=[],
            night_shift=False,
            default_staffing_count=1,
            active=True,
        )

        self.assertEqual(template.name, 'Berkeley 7a-4p')


class ScheduleBlockSerializerTests(TestCase):
    def test_schedule_block_length_cannot_exceed_twelve_months(self):
        serializer = ScheduleBlockSerializer(
            data={
                'start_date': '2026-01-01',
                'end_date': '2027-02-01',
                'request_open_datetime': '2025-11-01T00:00:00Z',
                'request_close_datetime': '2025-12-01T00:00:00Z',
            }
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn('end_date', serializer.errors)

    def test_request_close_must_be_after_open(self):
        serializer = ScheduleBlockSerializer(
            data={
                'start_date': '2026-01-01',
                'end_date': '2026-01-31',
                'request_open_datetime': '2025-11-01T00:00:00Z',
                'request_close_datetime': '2025-11-01T00:00:00Z',
            }
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn('request_close_datetime', serializer.errors)


class ScheduleBlockApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = get_user_model().objects.create_user(username='scheduler', password='password123')
        self.client.force_authenticate(user=self.user)

    def _create_block(self, **overrides):
        defaults = {
            'start_date': date(2026, 7, 1),
            'end_date': date(2026, 7, 31),
            'request_open_datetime': timezone.make_aware(datetime(2026, 5, 1, 12, 0, 0)),
            'request_close_datetime': timezone.make_aware(datetime(2026, 5, 15, 12, 0, 0)),
            'build_status': ScheduleBlock.BuildStatus.PRE_BUILD,
        }
        defaults.update(overrides)
        return ScheduleBlock.objects.create(**defaults)

    def test_delete_only_allowed_for_pre_build(self):
        block = self._create_block(build_status=ScheduleBlock.BuildStatus.BUILD)

        response = self.client.delete(f'/api/schedule-blocks/{block.id}/')

        self.assertEqual(response.status_code, 400)
        self.assertTrue(ScheduleBlock.objects.filter(id=block.id).exists())

    def test_publish_sets_archive_and_timestamp(self):
        block = self._create_block(build_status=ScheduleBlock.BuildStatus.PREVIEW)

        response = self.client.post(f'/api/schedule-blocks/{block.id}/publish/', data={}, format='json')

        self.assertEqual(response.status_code, 200)
        block.refresh_from_db()
        self.assertEqual(block.build_status, ScheduleBlock.BuildStatus.ARCHIVE)
        self.assertIsNotNone(block.published_at)


class ScheduleRequestApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()

        self.scheduler_user = get_user_model().objects.create_user(
            username='scheduler@example.com',
            email='scheduler@example.com',
            password='password123',
        )
        scheduler_group, _ = Group.objects.get_or_create(name='Scheduler')
        self.scheduler_user.groups.add(scheduler_group)

        self.physician_user = get_user_model().objects.create_user(
            username='physician@example.com',
            email='physician@example.com',
            password='password123',
            first_name='Pat',
            last_name='Physician',
        )
        self.physician = Physician.objects.create(user=self.physician_user, display_name='Pat Physician')

        self.facility = Facility.objects.create(name='Main Hospital', short_name='Main')
        self.shift_template = ShiftTemplate.objects.create(
            facility=self.facility,
            start_time=time(7, 0),
            end_time=time(16, 0),
            active_days_of_week=['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday'],
            weekend_days=[],
            night_shift=False,
            default_staffing_count=1,
            active=True,
        )

        self.block = ScheduleBlock.objects.create(
            start_date=date(2026, 7, 1),
            end_date=date(2026, 7, 31),
            request_open_datetime=timezone.make_aware(datetime(2026, 5, 1, 12, 0, 0)),
            request_close_datetime=timezone.make_aware(datetime(2026, 5, 15, 12, 0, 0)),
            build_status=ScheduleBlock.BuildStatus.PRE_BUILD,
        )

    def _assign_contract(self, request_settings, facilities=None):
        domain = Domain.objects.create(name='Emergency Medicine', active=True)
        contract = Contract.objects.create(
            domain=domain,
            name='Request Contract',
            active=True,
            request_settings=request_settings,
        )
        contract.facilities.set(facilities or [self.facility])
        ContractUserAssignment.objects.create(
            contract=contract,
            domain=domain,
            physician=self.physician,
        )
        return contract

    def test_scheduler_can_store_user_and_admin_request_same_day(self):
        self.client.force_authenticate(user=self.scheduler_user)

        user_payload = {
            'physician_id': self.physician.id,
            'date': '2026-07-01',
            'request_scope': 'USER',
            'request_type': 'DAY_OFF',
            'weight': 'HIGH',
            'shift_template_ids': [],
        }
        admin_payload = {
            'physician_id': self.physician.id,
            'date': '2026-07-01',
            'request_scope': 'ADMIN',
            'request_type': 'DAY_OFF',
            'weight': 'FIXED',
            'shift_template_ids': [],
        }

        user_response = self.client.post(
            f'/api/schedule-blocks/{self.block.id}/requests/upsert/',
            data=user_payload,
            format='json',
        )
        admin_response = self.client.post(
            f'/api/schedule-blocks/{self.block.id}/requests/upsert/',
            data=admin_payload,
            format='json',
        )

        self.assertEqual(user_response.status_code, 200)
        self.assertEqual(admin_response.status_code, 200)
        self.assertEqual(ScheduleRequest.objects.filter(schedule_block=self.block).count(), 2)

    def test_preview_block_rejects_request_writes(self):
        self.client.force_authenticate(user=self.scheduler_user)
        self.block.build_status = ScheduleBlock.BuildStatus.PREVIEW
        self.block.save(update_fields=['build_status', 'updated_at'])

        response = self.client.post(
            f'/api/schedule-blocks/{self.block.id}/requests/upsert/',
            data={
                'physician_id': self.physician.id,
                'date': '2026-07-01',
                'request_scope': 'USER',
                'request_type': 'DAY_OFF',
                'weight': 'HIGH',
                'shift_template_ids': [],
            },
            format='json',
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(ScheduleRequest.objects.count(), 0)

    def test_context_returns_schedule_block_data_for_scheduler(self):
        self.client.force_authenticate(user=self.scheduler_user)
        inactive_user = get_user_model().objects.create_user(
            username='inactive@example.com',
            email='inactive@example.com',
        )
        inactive_physician = Physician.objects.create(
            user=inactive_user,
            display_name='Inactive Physician',
            active=False,
        )

        response = self.client.get(f'/api/schedule-blocks/{self.block.id}/requests/context/')

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['schedule_block']['id'], self.block.id)
        self.assertTrue(payload['is_scheduler_or_admin'])
        self.assertEqual(payload['selected_physician_id'], self.physician.id)
        returned_ids = {item['id'] for item in payload['physicians']}
        self.assertIn(self.physician.id, returned_ids)
        self.assertNotIn(inactive_physician.id, returned_ids)

    def test_request_change_permission_grants_scheduler_context_access(self):
        permission_user = get_user_model().objects.create_user(
            username='request-manager@example.com',
            email='request-manager@example.com',
        )
        permission_user.user_permissions.add(
            Permission.objects.get(
                content_type__app_label='scheduling',
                codename='change_schedulerequest',
            )
        )
        self.client.force_authenticate(user=permission_user)

        response = self.client.get(f'/api/schedule-blocks/{self.block.id}/requests/context/')

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['is_scheduler_or_admin'])
        self.assertEqual(payload['selected_physician_id'], self.physician.id)

    def test_context_returns_200_for_authenticated_user_without_physician(self):
        user_without_physician = get_user_model().objects.create_user(
            username='observer@example.com',
            email='observer@example.com',
            password='password123',
        )
        self.client.force_authenticate(user=user_without_physician)

        response = self.client.get(f'/api/schedule-blocks/{self.block.id}/requests/context/')

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['schedule_block']['id'], self.block.id)
        self.assertEqual(payload['can_manage_requests'], False)
        self.assertEqual(payload['is_scheduler_or_admin'], False)
        self.assertEqual(payload['selected_physician_id'], None)
        self.assertEqual(payload['physicians'], [])

    def test_normal_user_request_options_and_templates_follow_contract(self):
        other_facility = Facility.objects.create(name='Other Hospital', short_name='Other')
        other_template = ShiftTemplate.objects.create(
            facility=other_facility,
            start_time=time(8, 0),
            end_time=time(16, 0),
            active_days_of_week=['Wednesday'],
            weekend_days=[],
            night_shift=False,
            default_staffing_count=1,
            active=True,
        )
        self._assign_contract(
            {
                'allow_day_off': False,
                'allow_shift_off': True,
                'allow_day_on': False,
                'allow_shift_on': True,
                'low_request_unlimited': True,
            }
        )
        self.client.force_authenticate(user=self.physician_user)

        context_response = self.client.get(f'/api/schedule-blocks/{self.block.id}/requests/context/')

        self.assertEqual(context_response.status_code, 200)
        payload = context_response.json()
        self.assertEqual(payload['request_policy']['allowed_request_types'], ['SHIFT_OFF', 'SHIFT_ON'])
        returned_template_ids = {item['id'] for item in payload['shift_templates']}
        self.assertIn(self.shift_template.id, returned_template_ids)
        self.assertNotIn(other_template.id, returned_template_ids)

        disallowed_response = self.client.post(
            f'/api/schedule-blocks/{self.block.id}/requests/upsert/',
            data={
                'physician_id': self.physician.id,
                'date': '2026-07-01',
                'request_scope': 'USER',
                'request_type': 'DAY_OFF',
                'weight': 'HIGH',
                'shift_template_ids': [],
            },
            format='json',
        )

        self.assertEqual(disallowed_response.status_code, 400)
        self.assertIn('request_type', disallowed_response.json())

    def test_scheduler_has_all_request_types_even_when_contract_restricts_user(self):
        self._assign_contract(
            {
                'allow_day_off': False,
                'allow_shift_off': False,
                'allow_day_on': False,
                'allow_shift_on': False,
            }
        )
        self.client.force_authenticate(user=self.scheduler_user)

        response = self.client.get(
            f'/api/schedule-blocks/{self.block.id}/requests/context/?physician_id={self.physician.id}'
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            set(response.json()['request_policy']['allowed_request_types']),
            {'DAY_OFF', 'SHIFT_OFF', 'DAY_ON', 'SHIFT_ON'},
        )

    def test_normal_user_without_unambiguous_contract_cannot_create_request(self):
        self.client.force_authenticate(user=self.physician_user)

        response = self.client.post(
            f'/api/schedule-blocks/{self.block.id}/requests/upsert/',
            data={
                'physician_id': self.physician.id,
                'date': '2026-07-01',
                'request_scope': 'USER',
                'request_type': 'DAY_OFF',
                'weight': 'HIGH',
                'shift_template_ids': [],
            },
            format='json',
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('request_type', response.json())

    def test_user_request_limit_is_enforced_and_returned_in_counters(self):
        self._assign_contract(
            {
                'allow_day_off': True,
                'allow_shift_off': True,
                'allow_day_on': True,
                'allow_shift_on': True,
                'high_request_limit': '1',
                'low_request_unlimited': True,
            }
        )
        self.client.force_authenticate(user=self.physician_user)
        payload = {
            'physician_id': self.physician.id,
            'request_scope': 'USER',
            'request_type': 'DAY_OFF',
            'weight': 'HIGH',
            'shift_template_ids': [],
        }

        first_response = self.client.post(
            f'/api/schedule-blocks/{self.block.id}/requests/upsert/',
            data={**payload, 'date': '2026-07-01'},
            format='json',
        )
        second_response = self.client.post(
            f'/api/schedule-blocks/{self.block.id}/requests/upsert/',
            data={**payload, 'date': '2026-07-02'},
            format='json',
        )
        context_response = self.client.get(f'/api/schedule-blocks/{self.block.id}/requests/context/')

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(second_response.status_code, 400)
        self.assertIn('request_limit', second_response.json())
        high_counter = context_response.json()['request_counters']['HIGH']
        self.assertEqual(high_counter, {'used': 1, 'limit': 1, 'unlimited': False})

    def test_weekend_counter_uses_shift_template_weekend_designation(self):
        self.shift_template.active_days_of_week = ['Friday']
        self.shift_template.weekend_days = ['Friday']
        self.shift_template.save(update_fields=['active_days_of_week', 'weekend_days'])
        self._assign_contract(
            {
                'allow_day_off': True,
                'allow_shift_off': True,
                'allow_day_on': True,
                'allow_shift_on': True,
                'weekend_request_limit': '2',
                'low_request_unlimited': True,
            }
        )
        self.client.force_authenticate(user=self.physician_user)

        save_response = self.client.post(
            f'/api/schedule-blocks/{self.block.id}/requests/upsert/',
            data={
                'physician_id': self.physician.id,
                'date': '2026-07-03',
                'request_scope': 'USER',
                'request_type': 'DAY_OFF',
                'weight': 'MEDIUM',
                'shift_template_ids': [],
            },
            format='json',
        )
        context_response = self.client.get(f'/api/schedule-blocks/{self.block.id}/requests/context/')

        self.assertEqual(save_response.status_code, 200)
        self.assertEqual(context_response.json()['request_counters']['WEEKEND']['used'], 1)

    def test_user_counters_increment_and_none_decrements_selected_scope_only(self):
        self._assign_contract(
            {
                'allow_day_off': True,
                'allow_shift_off': True,
                'allow_day_on': True,
                'allow_shift_on': True,
                'high_request_limit': '5',
                'medium_request_limit': '5',
                'low_request_limit': '5',
                'low_request_unlimited': False,
            }
        )
        self.client.force_authenticate(user=self.scheduler_user)
        request_url = f'/api/schedule-blocks/{self.block.id}/requests/upsert/'
        base_payload = {
            'physician_id': self.physician.id,
            'request_scope': 'USER',
            'request_type': 'DAY_OFF',
            'shift_template_ids': [],
        }

        for request_date, weight in [
            ('2026-07-01', 'HIGH'),
            ('2026-07-02', 'MEDIUM'),
            ('2026-07-03', 'LOW'),
        ]:
            response = self.client.post(
                request_url,
                data={**base_payload, 'date': request_date, 'weight': weight},
                format='json',
            )
            self.assertEqual(response.status_code, 200)

        context_response = self.client.get(
            f'/api/schedule-blocks/{self.block.id}/requests/context/?physician_id={self.physician.id}'
        )
        counters = context_response.json()['request_counters']
        self.assertEqual(counters['HIGH']['used'], 1)
        self.assertEqual(counters['MEDIUM']['used'], 1)
        self.assertEqual(counters['LOW']['used'], 1)

        delete_response = self.client.post(
            request_url,
            data={
                'physician_id': self.physician.id,
                'date': '2026-07-01',
                'request_scope': 'USER',
                'request_type': 'NONE',
            },
            format='json',
        )
        self.assertEqual(delete_response.status_code, 200)

        refreshed_context = self.client.get(
            f'/api/schedule-blocks/{self.block.id}/requests/context/?physician_id={self.physician.id}'
        )
        refreshed_counters = refreshed_context.json()['request_counters']
        self.assertEqual(refreshed_counters['HIGH']['used'], 0)
        self.assertEqual(refreshed_counters['MEDIUM']['used'], 1)
        self.assertEqual(refreshed_counters['LOW']['used'], 1)

    def test_admin_request_does_not_create_user_request_or_consume_counter(self):
        self._assign_contract(
            {
                'allow_day_off': True,
                'high_request_limit': '5',
                'low_request_unlimited': True,
            }
        )
        self.client.force_authenticate(user=self.scheduler_user)
        request_url = f'/api/schedule-blocks/{self.block.id}/requests/upsert/'

        admin_response = self.client.post(
            request_url,
            data={
                'physician_id': self.physician.id,
                'date': '2026-07-01',
                'request_scope': 'ADMIN',
                'request_type': 'DAY_OFF',
                'weight': 'HIGH',
                'shift_template_ids': [],
            },
            format='json',
        )

        self.assertEqual(admin_response.status_code, 200)
        self.assertEqual(
            ScheduleRequest.objects.filter(
                schedule_block=self.block,
                physician=self.physician,
                request_scope='ADMIN',
            ).count(),
            1,
        )
        self.assertFalse(
            ScheduleRequest.objects.filter(
                schedule_block=self.block,
                physician=self.physician,
                request_scope='USER',
            ).exists()
        )

        context_response = self.client.get(
            f'/api/schedule-blocks/{self.block.id}/requests/context/?physician_id={self.physician.id}'
        )
        self.assertEqual(context_response.json()['request_counters']['HIGH']['used'], 0)

        user_response = self.client.post(
            request_url,
            data={
                'physician_id': self.physician.id,
                'date': '2026-07-01',
                'request_scope': 'USER',
                'request_type': 'DAY_ON',
                'weight': 'MEDIUM',
                'shift_template_ids': [],
            },
            format='json',
        )
        self.assertEqual(user_response.status_code, 200)
        self.assertEqual(
            ScheduleRequest.objects.filter(
                schedule_block=self.block,
                physician=self.physician,
                date=date(2026, 7, 1),
            ).count(),
            2,
        )

    def test_multiple_weekend_shift_off_templates_count_as_one_weekend_request(self):
        self.shift_template.active_days_of_week = ['Friday']
        self.shift_template.weekend_days = ['Friday']
        self.shift_template.save(update_fields=['active_days_of_week', 'weekend_days'])
        second_template = ShiftTemplate.objects.create(
            facility=self.facility,
            start_time=time(16, 0),
            end_time=time(23, 0),
            active_days_of_week=['Friday'],
            weekend_days=['Friday'],
            night_shift=False,
            default_staffing_count=1,
            active=True,
        )
        self._assign_contract(
            {
                'allow_shift_off': True,
                'weekend_request_limit': '5',
                'low_request_unlimited': True,
            }
        )
        self.client.force_authenticate(user=self.scheduler_user)

        response = self.client.post(
            f'/api/schedule-blocks/{self.block.id}/requests/upsert/',
            data={
                'physician_id': self.physician.id,
                'date': '2026-07-03',
                'request_scope': 'USER',
                'request_type': 'SHIFT_OFF',
                'weight': 'MEDIUM',
                'shift_template_ids': [self.shift_template.id, second_template.id],
            },
            format='json',
        )
        context_response = self.client.get(
            f'/api/schedule-blocks/{self.block.id}/requests/context/?physician_id={self.physician.id}'
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(context_response.json()['request_counters']['WEEKEND']['used'], 1)

    def test_normal_user_request_list_hides_admin_request(self):
        ScheduleRequest.objects.create(
            schedule_block=self.block,
            physician=self.physician,
            date=date(2026, 7, 1),
            request_scope=ScheduleRequest.RequestScope.USER,
            request_type=ScheduleRequest.RequestType.DAY_OFF,
            weight=ScheduleRequest.Weight.HIGH,
            created_by=self.physician_user,
        )
        ScheduleRequest.objects.create(
            schedule_block=self.block,
            physician=self.physician,
            date=date(2026, 7, 1),
            request_scope=ScheduleRequest.RequestScope.ADMIN,
            request_type=ScheduleRequest.RequestType.DAY_ON,
            weight=ScheduleRequest.Weight.FIXED,
            created_by=self.scheduler_user,
        )
        self.client.force_authenticate(user=self.physician_user)

        response = self.client.get(f'/api/schedule-blocks/{self.block.id}/requests/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 1)
        self.assertEqual(response.json()[0]['request_scope'], 'USER')

    def test_scheduler_bulk_request_creates_one_request_per_physician_and_date(self):
        second_user = get_user_model().objects.create_user(
            username='second.physician@example.com',
            email='second.physician@example.com',
            password='password123',
        )
        second_physician = Physician.objects.create(user=second_user, display_name='Second Physician')
        self.client.force_authenticate(user=self.scheduler_user)

        response = self.client.post(
            f'/api/schedule-blocks/{self.block.id}/requests/bulk/',
            data={
                'physician_ids': [self.physician.id, second_physician.id],
                'dates': ['2026-07-01', '2026-07-02'],
                'request_type': 'DAY_OFF',
                'weight': 'MEDIUM',
                'shift_template_ids': [],
            },
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['saved_count'], 4)
        self.assertEqual(ScheduleRequest.objects.filter(schedule_block=self.block).count(), 4)

    def test_bulk_request_scope_creates_only_the_selected_scope_without_duplicates(self):
        self._assign_contract(
            {
                'allow_day_off': True,
                'medium_request_limit': '5',
                'low_request_unlimited': True,
            }
        )
        self.client.force_authenticate(user=self.scheduler_user)
        bulk_url = f'/api/schedule-blocks/{self.block.id}/requests/bulk/'
        common_payload = {
            'physician_ids': [self.physician.id],
            'dates': ['2026-07-01', '2026-07-02'],
            'request_type': 'DAY_OFF',
            'shift_template_ids': [],
        }

        user_response = self.client.post(
            bulk_url,
            data={
                **common_payload,
                'request_scope': 'USER',
                'weight': 'MEDIUM',
            },
            format='json',
        )
        admin_response = self.client.post(
            bulk_url,
            data={
                **common_payload,
                'request_scope': 'ADMIN',
                'weight': 'FIXED',
            },
            format='json',
        )
        repeated_admin_response = self.client.post(
            bulk_url,
            data={
                **common_payload,
                'request_scope': 'ADMIN',
                'weight': 'FIXED',
            },
            format='json',
        )

        self.assertEqual(user_response.status_code, 200)
        self.assertEqual(admin_response.status_code, 200)
        self.assertEqual(repeated_admin_response.status_code, 200)
        self.assertEqual(
            ScheduleRequest.objects.filter(
                schedule_block=self.block,
                physician=self.physician,
                request_scope='USER',
            ).count(),
            2,
        )
        self.assertEqual(
            ScheduleRequest.objects.filter(
                schedule_block=self.block,
                physician=self.physician,
                request_scope='ADMIN',
            ).count(),
            2,
        )
        self.assertEqual(
            ScheduleRequest.objects.filter(
                schedule_block=self.block,
                physician=self.physician,
            ).count(),
            4,
        )

        context_response = self.client.get(
            f'/api/schedule-blocks/{self.block.id}/requests/context/?physician_id={self.physician.id}'
        )
        self.assertEqual(context_response.json()['request_counters']['MEDIUM']['used'], 2)

    def test_clear_requests_removes_only_requested_scope(self):
        user_request = ScheduleRequest.objects.create(
            schedule_block=self.block,
            physician=self.physician,
            date=date(2026, 7, 1),
            request_scope=ScheduleRequest.RequestScope.USER,
            request_type=ScheduleRequest.RequestType.DAY_OFF,
            weight=ScheduleRequest.Weight.HIGH,
            created_by=self.scheduler_user,
        )
        admin_request = ScheduleRequest.objects.create(
            schedule_block=self.block,
            physician=self.physician,
            date=date(2026, 7, 1),
            request_scope=ScheduleRequest.RequestScope.ADMIN,
            request_type=ScheduleRequest.RequestType.DAY_ON,
            weight=ScheduleRequest.Weight.FIXED,
            created_by=self.scheduler_user,
        )
        self.client.force_authenticate(user=self.scheduler_user)
        clear_url = f'/api/schedule-blocks/{self.block.id}/requests/clear/'

        user_clear_response = self.client.post(
            clear_url,
            data={'request_scope': 'USER'},
            format='json',
        )

        self.assertEqual(user_clear_response.status_code, 200)
        self.assertFalse(ScheduleRequest.objects.filter(id=user_request.id).exists())
        self.assertTrue(ScheduleRequest.objects.filter(id=admin_request.id).exists())

        admin_clear_response = self.client.post(
            clear_url,
            data={'request_scope': 'ADMIN'},
            format='json',
        )

        self.assertEqual(admin_clear_response.status_code, 200)
        self.assertFalse(ScheduleRequest.objects.filter(id=admin_request.id).exists())

    def test_normal_user_cannot_clear_schedule_block_requests(self):
        self.client.force_authenticate(user=self.physician_user)

        response = self.client.post(
            f'/api/schedule-blocks/{self.block.id}/requests/clear/',
            data={'request_scope': 'USER'},
            format='json',
        )

        self.assertEqual(response.status_code, 403)


class ScheduleBuildWorkspaceApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.scheduler_user = get_user_model().objects.create_user(
            username='build-scheduler@example.com',
            password='password123',
        )
        scheduler_group, _ = Group.objects.get_or_create(name='Scheduler')
        self.scheduler_user.groups.add(scheduler_group)
        self.client.force_authenticate(user=self.scheduler_user)

        self.domain = Domain.objects.create(name='Physician', active=True)
        self.facility = Facility.objects.create(
            name='Berkeley Hospital',
            short_name='Berkeley',
            timezone='UTC',
        )
        self.day_template = ShiftTemplate.objects.create(
            facility=self.facility,
            start_time=time(7, 0),
            end_time=time(16, 0),
            active_days_of_week=['Monday'],
            weekend_days=[],
            night_shift=False,
            default_staffing_count=2,
            active=True,
        )
        self.overnight_template = ShiftTemplate.objects.create(
            facility=self.facility,
            start_time=time(19, 0),
            end_time=time(7, 0),
            active_days_of_week=['Monday'],
            weekend_days=[],
            night_shift=True,
            default_staffing_count=1,
            active=True,
        )
        self.inactive_template = ShiftTemplate.objects.create(
            facility=self.facility,
            start_time=time(10, 0),
            end_time=time(18, 0),
            active_days_of_week=['Monday'],
            weekend_days=[],
            night_shift=False,
            default_staffing_count=1,
            active=False,
        )
        self.inactive_facility = Facility.objects.create(
            name='Closed Hospital',
            short_name='Closed',
            timezone='UTC',
            active=False,
        )
        self.inactive_facility_template = ShiftTemplate.objects.create(
            facility=self.inactive_facility,
            start_time=time(8, 0),
            end_time=time(17, 0),
            active_days_of_week=['Monday'],
            weekend_days=[],
            night_shift=False,
            default_staffing_count=1,
            active=True,
        )
        self.block = ScheduleBlock.objects.create(
            start_date=date(2026, 7, 6),
            end_date=date(2026, 7, 7),
            request_open_datetime=timezone.make_aware(datetime(2026, 5, 1, 12, 0)),
            request_close_datetime=timezone.make_aware(datetime(2026, 5, 15, 12, 0)),
            build_status=ScheduleBlock.BuildStatus.PRE_BUILD,
        )

    def _create_assignment_physician(self, email, display_name, facilities=None, active=True):
        name_parts = display_name.split()
        user = get_user_model().objects.create_user(
            username=email,
            email=email,
            first_name=name_parts[0],
            last_name=name_parts[-1],
        )
        physician = Physician.objects.create(
            user=user,
            display_name=display_name,
            active=active,
        )
        contract = Contract.objects.create(
            domain=self.domain,
            name=f'{display_name} Contract',
            active=True,
        )
        contract.facilities.set(facilities or [])
        ContractUserAssignment.objects.create(
            contract=contract,
            domain=self.domain,
            physician=physician,
        )
        return physician

    def _optimizer_run_signature(self, optimizer_run):
        return tuple(
            ScheduleShiftAssignment.objects.filter(optimizer_run=optimizer_run)
            .order_by('shift_instance_id', 'physician_id')
            .values_list('shift_instance_id', 'physician_id')
        )

    def _create_build_version(self, start_date=None, end_date=None):
        if start_date is not None:
            self.block.start_date = start_date
        if end_date is not None:
            self.block.end_date = end_date
        self.block.build_status = ScheduleBlock.BuildStatus.BUILD
        self.block.save(update_fields=['start_date', 'end_date', 'build_status', 'updated_at'])
        return ScheduleVersion.objects.create(
            schedule_block=self.block,
            domain=self.domain,
            version_number=1,
            name='Build 1',
            status=ScheduleVersion.Status.BUILD,
        )

    def _create_shift_instance(self, version, template, target_date):
        end_date = target_date + timedelta(days=1) if template.end_time <= template.start_time else target_date
        return ScheduleShiftInstance.objects.create(
            schedule_block=self.block,
            schedule_version=version,
            shift_template=template,
            facility=template.facility,
            date=target_date,
            start_datetime=timezone.make_aware(datetime.combine(target_date, template.start_time)),
            end_datetime=timezone.make_aware(datetime.combine(end_date, template.end_time)),
            required_staffing=1,
        )

    def test_generate_creates_open_dated_instances_and_moves_block_to_build(self):
        response = self.client.post(
            f'/api/schedule-blocks/{self.block.id}/build/generate/',
            data={'domain_id': self.domain.id},
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['created_count'], 2)
        self.block.refresh_from_db()
        self.assertEqual(self.block.build_status, ScheduleBlock.BuildStatus.BUILD)

        version = ScheduleVersion.objects.get(schedule_block=self.block)
        self.assertEqual(version.domain, self.domain)
        self.assertEqual(version.status, ScheduleVersion.Status.BUILD)

        instances = ScheduleShiftInstance.objects.filter(schedule_version=version)
        self.assertEqual(instances.count(), 2)
        self.assertFalse(
            instances.filter(shift_template=self.inactive_template).exists()
        )
        self.assertFalse(
            instances.filter(shift_template=self.inactive_facility_template).exists()
        )
        day_instance = instances.get(shift_template=self.day_template)
        self.assertEqual(day_instance.date, date(2026, 7, 6))
        self.assertEqual(day_instance.required_staffing, 2)
        self.assertFalse(day_instance.assignments.exists())
        self.assertEqual(day_instance.status, ScheduleShiftInstance.Status.OPEN)

        overnight_instance = instances.get(shift_template=self.overnight_template)
        self.assertEqual(overnight_instance.start_datetime.date(), date(2026, 7, 6))
        self.assertEqual(overnight_instance.end_datetime.date(), date(2026, 7, 7))
        self.assertGreater(overnight_instance.end_datetime, overnight_instance.start_datetime)

    def test_generate_is_idempotent_for_existing_build_version(self):
        generate_url = f'/api/schedule-blocks/{self.block.id}/build/generate/'

        first_response = self.client.post(
            generate_url,
            data={'domain_id': self.domain.id},
            format='json',
        )
        second_response = self.client.post(
            generate_url,
            data={'domain_id': self.domain.id},
            format='json',
        )

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(second_response.status_code, 200)
        self.assertEqual(second_response.json()['created_count'], 0)
        self.assertEqual(ScheduleVersion.objects.filter(schedule_block=self.block).count(), 1)
        self.assertEqual(ScheduleShiftInstance.objects.filter(schedule_block=self.block).count(), 2)

    def test_context_and_list_endpoints_return_selected_version_and_instances(self):
        generate_response = self.client.post(
            f'/api/schedule-blocks/{self.block.id}/build/generate/',
            data={'domain_id': self.domain.id},
            format='json',
        )
        version_id = generate_response.json()['schedule_version']['id']

        context_response = self.client.get(f'/api/schedule-blocks/{self.block.id}/build/')
        versions_response = self.client.get(f'/api/schedule-blocks/{self.block.id}/build/versions/')
        shifts_response = self.client.get(
            f'/api/schedule-blocks/{self.block.id}/build/versions/{version_id}/shifts/'
        )

        self.assertEqual(context_response.status_code, 200)
        self.assertEqual(context_response.json()['selected_version']['id'], version_id)
        self.assertEqual(len(context_response.json()['shift_instances']), 2)
        self.assertEqual(versions_response.status_code, 200)
        self.assertEqual(len(versions_response.json()), 1)
        self.assertEqual(shifts_response.status_code, 200)
        self.assertEqual(len(shifts_response.json()), 2)
        self.assertEqual(shifts_response.json()[0]['assigned_count'], 0)

    def test_generate_rejects_preview_or_archived_block(self):
        self.block.build_status = ScheduleBlock.BuildStatus.PREVIEW
        self.block.save(update_fields=['build_status', 'updated_at'])

        response = self.client.post(
            f'/api/schedule-blocks/{self.block.id}/build/generate/',
            data={'domain_id': self.domain.id},
            format='json',
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(ScheduleVersion.objects.filter(schedule_block=self.block).exists())

    def test_assign_remove_and_refresh_multiple_physicians(self):
        self.client.post(
            f'/api/schedule-blocks/{self.block.id}/build/generate/',
            data={'domain_id': self.domain.id},
            format='json',
        )
        shift_instance = ScheduleShiftInstance.objects.get(shift_template=self.day_template)
        first = self._create_assignment_physician(
            'turner@example.com',
            'Alex Turner',
            facilities=[self.facility],
        )
        second = self._create_assignment_physician(
            'ng@example.com',
            'Casey Ng',
            facilities=[self.facility],
        )
        third = self._create_assignment_physician(
            'third@example.com',
            'Jamie Third',
            facilities=[self.facility],
        )
        assignment_url = (
            f'/api/schedule-blocks/{self.block.id}/build/'
            f'shift-instances/{shift_instance.id}/assignments/'
        )

        context_response = self.client.get(assignment_url)
        self.assertEqual(context_response.status_code, 200)
        physician_context = {
            item['id']: item for item in context_response.json()['eligible_physicians']
        }
        self.assertTrue(physician_context[first.id]['can_assign'])
        self.assertTrue(physician_context[second.id]['can_assign'])

        first_response = self.client.post(
            assignment_url,
            data={'physician_id': first.id},
            format='json',
        )
        self.assertEqual(first_response.status_code, 201)
        first_shift = first_response.json()['shift_instance']
        self.assertEqual(first_shift['assigned_count'], 1)
        self.assertEqual(first_shift['open_count'], 1)
        self.assertTrue(first_shift['is_open'])
        self.assertEqual(first_shift['status'], ScheduleShiftInstance.Status.OPEN)

        duplicate_response = self.client.post(
            assignment_url,
            data={'physician_id': first.id},
            format='json',
        )
        self.assertEqual(duplicate_response.status_code, 400)
        self.assertEqual(
            ScheduleShiftAssignment.objects.filter(shift_instance=shift_instance).count(),
            1,
        )

        second_response = self.client.post(
            assignment_url,
            data={'physician_id': second.id},
            format='json',
        )
        self.assertEqual(second_response.status_code, 201)
        second_shift = second_response.json()['shift_instance']
        self.assertEqual(second_shift['assigned_count'], 2)
        self.assertEqual(second_shift['open_count'], 0)
        self.assertFalse(second_shift['is_open'])
        self.assertEqual(second_shift['status'], ScheduleShiftInstance.Status.ASSIGNED)

        full_response = self.client.post(
            assignment_url,
            data={'physician_id': third.id},
            format='json',
        )
        self.assertEqual(full_response.status_code, 400)
        self.assertEqual(full_response.json()['detail'], 'This shift instance is already fully staffed.')
        self.assertEqual(
            ScheduleShiftAssignment.objects.filter(shift_instance=shift_instance).count(),
            2,
        )

        first_assignment_id = next(
            assignment['id']
            for assignment in second_shift['assignments']
            if assignment['physician'] == first.id
        )
        delete_response = self.client.delete(f'{assignment_url}{first_assignment_id}/')
        self.assertEqual(delete_response.status_code, 200)
        deleted_shift = delete_response.json()['shift_instance']
        self.assertEqual(deleted_shift['assigned_count'], 1)
        self.assertEqual(deleted_shift['open_count'], 1)
        self.assertEqual(deleted_shift['status'], ScheduleShiftInstance.Status.OPEN)

        refresh_response = self.client.get(assignment_url)
        self.assertEqual(refresh_response.status_code, 200)
        refreshed_shift = refresh_response.json()['shift_instance']
        self.assertEqual(refreshed_shift['assigned_count'], 1)
        self.assertEqual(refreshed_shift['assignments'][0]['physician'], second.id)

    def test_assignment_context_marks_facility_ineligible_and_excludes_inactive(self):
        self.client.post(
            f'/api/schedule-blocks/{self.block.id}/build/generate/',
            data={'domain_id': self.domain.id},
            format='json',
        )
        shift_instance = ScheduleShiftInstance.objects.get(shift_template=self.day_template)
        ineligible = self._create_assignment_physician(
            'outside@example.com',
            'Morgan Outside',
        )
        inactive = self._create_assignment_physician(
            'inactive@example.com',
            'Inactive Physician',
            facilities=[self.facility],
            active=False,
        )
        assignment_url = (
            f'/api/schedule-blocks/{self.block.id}/build/'
            f'shift-instances/{shift_instance.id}/assignments/'
        )

        response = self.client.get(assignment_url)
        self.assertEqual(response.status_code, 200)
        physician_context = {
            item['id']: item for item in response.json()['eligible_physicians']
        }
        self.assertIn(ineligible.id, physician_context)
        self.assertFalse(physician_context[ineligible.id]['facility_eligible'])
        self.assertFalse(physician_context[ineligible.id]['can_assign'])
        self.assertNotIn(inactive.id, physician_context)

        assign_response = self.client.post(
            assignment_url,
            data={'physician_id': ineligible.id},
            format='json',
        )
        self.assertEqual(assign_response.status_code, 400)
        self.assertIn('Contract does not include Berkeley Hospital.', assign_response.json()['physician_id'])
        self.assertFalse(
            ScheduleShiftAssignment.objects.filter(shift_instance=shift_instance).exists()
        )

    def test_assignment_rejects_inactive_and_out_of_domain_physicians_clearly(self):
        self.client.post(
            f'/api/schedule-blocks/{self.block.id}/build/generate/',
            data={'domain_id': self.domain.id},
            format='json',
        )
        shift_instance = ScheduleShiftInstance.objects.get(shift_template=self.day_template)
        inactive = self._create_assignment_physician(
            'inactive.assign@example.com',
            'Inactive Physician',
            facilities=[self.facility],
            active=False,
        )
        other_domain = Domain.objects.create(name='Other Domain', active=True)
        outside_user = get_user_model().objects.create_user(
            username='outside.domain@example.com',
            email='outside.domain@example.com',
            first_name='Outside',
            last_name='Domain',
        )
        outside_physician = Physician.objects.create(
            user=outside_user,
            display_name='Outside Domain',
            active=True,
        )
        outside_contract = Contract.objects.create(
            domain=other_domain,
            name='Outside Domain Contract',
            active=True,
        )
        outside_contract.facilities.set([self.facility])
        ContractUserAssignment.objects.create(
            contract=outside_contract,
            domain=other_domain,
            physician=outside_physician,
        )
        assignment_url = (
            f'/api/schedule-blocks/{self.block.id}/build/'
            f'shift-instances/{shift_instance.id}/assignments/'
        )

        context_response = self.client.get(assignment_url)
        physician_context = {
            item['id']: item for item in context_response.json()['eligible_physicians']
        }
        self.assertNotIn(inactive.id, physician_context)
        self.assertFalse(physician_context[outside_physician.id]['can_assign'])
        self.assertIn(
            'No active Contract assignment in Physician.',
            physician_context[outside_physician.id]['ineligibility_reason'],
        )

        inactive_response = self.client.post(
            assignment_url,
            data={'physician_id': inactive.id},
            format='json',
        )
        outside_response = self.client.post(
            assignment_url,
            data={'physician_id': outside_physician.id},
            format='json',
        )

        self.assertEqual(inactive_response.status_code, 400)
        self.assertEqual(inactive_response.json()['physician_id'], 'Physician is inactive.')
        self.assertEqual(outside_response.status_code, 400)
        self.assertIn(
            'No active Contract assignment in Physician.',
            outside_response.json()['physician_id'],
        )
        self.assertFalse(
            ScheduleShiftAssignment.objects.filter(shift_instance=shift_instance).exists()
        )

    def test_assignment_rejects_overlapping_shift_in_same_version(self):
        overlapping_template = ShiftTemplate.objects.create(
            facility=self.facility,
            start_time=time(12, 0),
            end_time=time(22, 0),
            active_days_of_week=['Monday'],
            weekend_days=[],
            night_shift=False,
            default_staffing_count=1,
            active=True,
        )
        self.client.post(
            f'/api/schedule-blocks/{self.block.id}/build/generate/',
            data={'domain_id': self.domain.id},
            format='json',
        )
        day_instance = ScheduleShiftInstance.objects.get(shift_template=self.day_template)
        overlapping_instance = ScheduleShiftInstance.objects.get(
            shift_template=overlapping_template
        )
        physician = self._create_assignment_physician(
            'turner.overlap@example.com',
            'Alex Turner',
            facilities=[self.facility],
        )
        day_assignment_url = (
            f'/api/schedule-blocks/{self.block.id}/build/'
            f'shift-instances/{day_instance.id}/assignments/'
        )
        overlapping_assignment_url = (
            f'/api/schedule-blocks/{self.block.id}/build/'
            f'shift-instances/{overlapping_instance.id}/assignments/'
        )

        assigned_response = self.client.post(
            day_assignment_url,
            data={'physician_id': physician.id},
            format='json',
        )
        overlap_response = self.client.post(
            overlapping_assignment_url,
            data={'physician_id': physician.id},
            format='json',
        )

        self.assertEqual(assigned_response.status_code, 201)
        self.assertEqual(overlap_response.status_code, 400)
        self.assertIn('Alex Turner is already assigned to Berkeley 7a-4p', str(overlap_response.json()))
        self.assertEqual(
            ScheduleShiftAssignment.objects.filter(physician=physician).count(),
            1,
        )

        context_response = self.client.get(overlapping_assignment_url)
        physician_context = {
            item['id']: item for item in context_response.json()['eligible_physicians']
        }
        self.assertFalse(physician_context[physician.id]['can_assign'])
        self.assertIn(
            'overlaps this shift',
            physician_context[physician.id]['ineligibility_reason'],
        )

    def test_assignment_rejects_overnight_overlap(self):
        early_template = ShiftTemplate.objects.create(
            facility=self.facility,
            start_time=time(6, 0),
            end_time=time(12, 0),
            active_days_of_week=['Tuesday'],
            weekend_days=[],
            night_shift=False,
            default_staffing_count=1,
            active=True,
        )
        self.client.post(
            f'/api/schedule-blocks/{self.block.id}/build/generate/',
            data={'domain_id': self.domain.id},
            format='json',
        )
        overnight_instance = ScheduleShiftInstance.objects.get(
            shift_template=self.overnight_template
        )
        early_instance = ScheduleShiftInstance.objects.get(shift_template=early_template)
        physician = self._create_assignment_physician(
            'turner.overnight@example.com',
            'Alex Turner',
            facilities=[self.facility],
        )
        overnight_assignment_url = (
            f'/api/schedule-blocks/{self.block.id}/build/'
            f'shift-instances/{overnight_instance.id}/assignments/'
        )
        early_assignment_url = (
            f'/api/schedule-blocks/{self.block.id}/build/'
            f'shift-instances/{early_instance.id}/assignments/'
        )

        assigned_response = self.client.post(
            overnight_assignment_url,
            data={'physician_id': physician.id},
            format='json',
        )
        overlap_response = self.client.post(
            early_assignment_url,
            data={'physician_id': physician.id},
            format='json',
        )

        self.assertEqual(assigned_response.status_code, 201)
        self.assertEqual(overlap_response.status_code, 400)
        self.assertIn('overlaps this shift', str(overlap_response.json()))
        self.assertEqual(
            ScheduleShiftAssignment.objects.filter(physician=physician).count(),
            1,
        )

    def test_optimizer_assigns_open_slots_and_keeps_manual_assignments_locked(self):
        self.client.post(
            f'/api/schedule-blocks/{self.block.id}/build/generate/',
            data={'domain_id': self.domain.id},
            format='json',
        )
        version = ScheduleVersion.objects.get(schedule_block=self.block)
        day_instance = ScheduleShiftInstance.objects.get(shift_template=self.day_template)
        manual = self._create_assignment_physician(
            'manual.optimizer@example.com',
            'Manual Locked',
            facilities=[self.facility],
        )
        neutral = self._create_assignment_physician(
            'neutral.optimizer@example.com',
            'Neutral Candidate',
            facilities=[self.facility],
        )
        backup = self._create_assignment_physician(
            'backup.optimizer@example.com',
            'Backup Candidate',
            facilities=[self.facility],
        )
        day_off = self._create_assignment_physician(
            'dayoff.optimizer@example.com',
            'Dayoff Candidate',
            facilities=[self.facility],
        )
        ScheduleRequest.objects.create(
            schedule_block=self.block,
            physician=day_off,
            date=day_instance.date,
            request_scope=ScheduleRequest.RequestScope.USER,
            request_type=ScheduleRequest.RequestType.DAY_OFF,
            weight=ScheduleRequest.Weight.FIXED,
            created_by=self.scheduler_user,
        )
        manual_assignment = ScheduleShiftAssignment.objects.create(
            shift_instance=day_instance,
            physician=manual,
            created_by=self.scheduler_user,
        )

        response = self.client.post(
            f'/api/schedule-blocks/{self.block.id}/build/versions/{version.id}/optimize/',
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertGreaterEqual(payload['assignments_made'], 2)
        self.assertEqual(payload['unfilled_shift_count'], 0)
        manual_assignment.refresh_from_db()
        self.assertEqual(manual_assignment.physician, manual)
        day_assignments = list(
            ScheduleShiftAssignment.objects.filter(shift_instance=day_instance)
            .order_by('created_at')
            .values_list('physician_id', 'assignment_source')
        )
        self.assertIn(
            (manual.id, ScheduleShiftAssignment.AssignmentSource.MANUAL),
            day_assignments,
        )
        self.assertTrue(
            any(
                physician_id != manual.id
                and assignment_source == ScheduleShiftAssignment.AssignmentSource.OPTIMIZER
                for physician_id, assignment_source in day_assignments
            )
        )
        self.assertNotIn(
            (day_off.id, ScheduleShiftAssignment.AssignmentSource.OPTIMIZER),
            day_assignments,
        )
        self.assertEqual(day_instance.assignments.count(), day_instance.required_staffing)

    def test_optimizer_summary_persists_and_build_context_reloads_it(self):
        self.client.post(
            f'/api/schedule-blocks/{self.block.id}/build/generate/',
            data={'domain_id': self.domain.id},
            format='json',
        )
        version = ScheduleVersion.objects.get(schedule_block=self.block)
        for index in range(4):
            self._create_assignment_physician(
                f'persist{index}@example.com',
                f'Persist {index}',
                facilities=[self.facility],
            )

        optimize_response = self.client.post(
            f'/api/schedule-blocks/{self.block.id}/build/versions/{version.id}/optimize/',
            format='json',
        )

        self.assertEqual(optimize_response.status_code, 200)
        optimize_payload = optimize_response.json()
        version.refresh_from_db()
        self.assertEqual(version.optimizer_summary['final_score'], optimize_payload['final_score'])
        self.assertIn('debug', version.optimizer_summary)

        context_response = self.client.get(
            f'/api/schedule-blocks/{self.block.id}/build/?version_id={version.id}'
        )

        self.assertEqual(context_response.status_code, 200)
        context_payload = context_response.json()
        self.assertEqual(
            context_payload['optimizer_summary']['final_score'],
            optimize_payload['final_score'],
        )
        self.assertIn('workload_summary', context_payload['optimizer_summary'])

    def test_optimizer_skips_disabled_night_block_builder_and_keeps_night_debug(self):
        self.block.start_date = date(2026, 7, 1)
        self.block.end_date = date(2026, 7, 4)
        self.block.build_status = ScheduleBlock.BuildStatus.PRE_BUILD
        self.block.save(update_fields=['start_date', 'end_date', 'build_status', 'updated_at'])
        night_template = ShiftTemplate.objects.create(
            facility=self.facility,
            start_time=time(19, 0),
            end_time=time(7, 0),
            active_days_of_week=['Wednesday', 'Thursday'],
            weekend_days=[],
            night_shift=True,
            default_staffing_count=1,
            active=True,
        )
        day_template = ShiftTemplate.objects.create(
            facility=self.facility,
            start_time=time(7, 0),
            end_time=time(16, 0),
            active_days_of_week=['Friday', 'Saturday'],
            weekend_days=[],
            night_shift=False,
            default_staffing_count=1,
            active=True,
        )
        physicians = [
            self._create_assignment_physician(
                f'nightblock{index}@example.com',
                f'Night Block {index}',
                facilities=[self.facility],
            )
            for index in range(3)
        ]
        for physician in physicians:
            contract = Contract.objects.get(user_assignments__physician=physician)
            contract.night_settings = {
                'max_consecutive_night_shifts': '4',
                'max_consecutive_night_shifts_penalty_weight': '2000',
                'days_off_after_night_block': '2',
                'days_off_after_night_block_penalty_weight': '10000',
            }
            contract.save(update_fields=['night_settings', 'updated_at'])
        self.client.post(
            f'/api/schedule-blocks/{self.block.id}/build/generate/',
            data={'domain_id': self.domain.id},
            format='json',
        )
        version = ScheduleVersion.objects.get(schedule_block=self.block)

        response = self.client.post(
            f'/api/schedule-blocks/{self.block.id}/build/versions/{version.id}/optimize/',
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['unfilled_shift_count'], 0)
        night_instances = ScheduleShiftInstance.objects.filter(
            schedule_version=version,
            shift_template=night_template,
        )
        day_instances = ScheduleShiftInstance.objects.filter(
            schedule_version=version,
            shift_template=day_template,
        )
        night_physician_ids = set(
            ScheduleShiftAssignment.objects.filter(shift_instance__in=night_instances)
            .values_list('physician_id', flat=True)
        )
        day_physician_ids = set(
            ScheduleShiftAssignment.objects.filter(shift_instance__in=day_instances)
            .values_list('physician_id', flat=True)
        )
        self.assertGreaterEqual(len(night_physician_ids), 1)
        self.assertGreaterEqual(len(day_physician_ids), 1)
        self.assertFalse(payload['debug']['night_block_builder_enabled'])
        self.assertTrue(payload['debug']['night_block_builder_skipped'])
        self.assertEqual(
            payload['debug']['night_block_builder_disabled_reason'],
            'Disabled after runtime regression',
        )
        self.assertEqual(payload['debug']['night_block_candidates_created'], 0)
        self.assertEqual(payload['debug']['night_block_lengths_assigned'], [])
        self.assertIn('isolated_night_count', payload['debug'])
        self.assertIn('night_blocks_count', payload['debug'])
        self.assertIn('max_night_block_length', payload['debug'])
        self.assertIn('post_night_recovery_violations_count', payload['debug'])

    def test_optimizer_prioritizes_configured_night_minimum(self):
        self.block.start_date = date(2026, 7, 1)
        self.block.end_date = date(2026, 7, 3)
        self.block.build_status = ScheduleBlock.BuildStatus.PRE_BUILD
        self.block.save(update_fields=['start_date', 'end_date', 'build_status', 'updated_at'])
        self.day_template.active = False
        self.overnight_template.active = False
        self.day_template.save(update_fields=['active'])
        self.overnight_template.save(update_fields=['active'])
        night_template = ShiftTemplate.objects.create(
            facility=self.facility,
            start_time=time(19, 0),
            end_time=time(7, 0),
            active_days_of_week=['Wednesday', 'Thursday', 'Friday'],
            weekend_days=[],
            night_shift=True,
            default_staffing_count=1,
            active=True,
        )
        physicians = [
            self._create_assignment_physician(
                f'nightmin{index}@example.com',
                f'Night Minimum {index}',
                facilities=[self.facility],
            )
            for index in range(3)
        ]
        for physician in physicians:
            contract = Contract.objects.get(user_assignments__physician=physician)
            contract.night_settings = {
                'period_rules': [
                    {
                        'period_type': 'SCHEDULE_BLOCK',
                        'min_shifts': '1',
                        'min_penalty_weight': '50000',
                    }
                ],
                'max_consecutive_night_shifts': '4',
            }
            contract.save(update_fields=['night_settings', 'updated_at'])
        self.client.post(
            f'/api/schedule-blocks/{self.block.id}/build/generate/',
            data={'domain_id': self.domain.id},
            format='json',
        )
        version = ScheduleVersion.objects.get(schedule_block=self.block)

        response = self.client.post(
            f'/api/schedule-blocks/{self.block.id}/build/versions/{version.id}/optimize/',
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        night_counts = dict(
            ScheduleShiftAssignment.objects.filter(
                shift_instance__schedule_version=version,
                shift_instance__shift_template=night_template,
            )
            .values('physician_id')
            .annotate(row_count=Count('id'))
            .values_list('physician_id', 'row_count')
        )
        self.assertEqual(set(night_counts.values()), {1})
        self.assertEqual(set(night_counts.keys()), {physician.id for physician in physicians})
        self.assertEqual(payload['debug']['night_minimum_violations_count'], 0)
        self.assertEqual(payload['debug']['night_minimum_required'], 1)
        self.assertEqual(payload['debug']['night_minimum_period'], 'SCHEDULE_BLOCK')
        self.assertGreater(payload['debug']['night_block_assignment_attempts'], 0)

    def test_night_block_builder_is_disabled_by_default_after_safety_rollback(self):
        version = self._create_build_version(date(2026, 7, 1), date(2026, 7, 3))
        night_template = ShiftTemplate.objects.create(
            facility=self.facility,
            start_time=time(19, 0),
            end_time=time(7, 0),
            active_days_of_week=['Wednesday', 'Thursday', 'Friday'],
            weekend_days=[],
            night_shift=True,
            default_staffing_count=1,
            active=True,
        )
        block_physician = self._create_assignment_physician(
            'night.builder.block@example.com',
            'Night Builder Block',
            facilities=[self.facility],
        )
        optional_physician = self._create_assignment_physician(
            'night.builder.optional@example.com',
            'Night Builder Optional',
            facilities=[self.facility],
        )
        block_contract = Contract.objects.get(user_assignments__physician=block_physician)
        block_contract.night_settings = {
            'period_rules': [
                {
                    'period_type': 'SCHEDULE_BLOCK',
                    'min_shifts': '2',
                    'min_penalty_weight': '50000',
                }
            ],
            'min_consecutive_night_shifts': '2',
            'max_consecutive_night_shifts': '3',
        }
        block_contract.save(update_fields=['night_settings', 'updated_at'])
        optional_contract = Contract.objects.get(user_assignments__physician=optional_physician)
        optional_contract.night_settings = {
            'period_rules': [
                {
                    'period_type': 'SCHEDULE_BLOCK',
                    'min_shifts': '0',
                    'min_penalty_weight': '50000',
                }
            ],
            'min_consecutive_night_shifts': '2',
            'max_consecutive_night_shifts': '3',
        }
        optional_contract.save(update_fields=['night_settings', 'updated_at'])
        for day in [1, 2, 3]:
            self._create_shift_instance(version, night_template, date(2026, 7, day))

        response = self.client.post(
            f'/api/schedule-versions/{version.id}/run-optimizer/',
            data={'seed': 606},
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload['debug']['night_block_builder_enabled'])
        self.assertTrue(payload['debug']['night_block_builder_skipped'])
        self.assertEqual(
            payload['debug']['night_block_builder_disabled_reason'],
            'Disabled after runtime regression',
        )
        self.assertEqual(payload['debug']['night_shift_instances_considered'], 3)
        self.assertEqual(payload['debug']['night_block_candidates_created'], 0)
        self.assertGreater(payload['debug']['night_block_assignment_successes'], 0)
        self.assertEqual(payload['debug']['night_block_lengths_assigned'], [])
        block_night_dates = set(
            ScheduleShiftAssignment.objects.filter(
                shift_instance__schedule_version=version,
                shift_instance__shift_template=night_template,
                physician=block_physician,
            ).values_list('shift_instance__date', flat=True)
        )
        self.assertTrue({date(2026, 7, 1), date(2026, 7, 2)}.issubset(block_night_dates) or {
            date(2026, 7, 2),
            date(2026, 7, 3),
        }.issubset(block_night_dates))
        optional_night_count = ScheduleShiftAssignment.objects.filter(
            shift_instance__schedule_version=version,
            shift_instance__shift_template=night_template,
            physician=optional_physician,
        ).count()
        self.assertLessEqual(optional_night_count, 1)
        self.assertEqual(payload['debug']['score_audit']['warnings'], [])

    def test_night_block_builder_respects_max_consecutive_and_facility_eligibility(self):
        version = self._create_build_version(date(2026, 7, 1), date(2026, 7, 4))
        night_template = ShiftTemplate.objects.create(
            facility=self.facility,
            start_time=time(19, 0),
            end_time=time(7, 0),
            active_days_of_week=['Wednesday', 'Thursday', 'Friday', 'Saturday'],
            weekend_days=[],
            night_shift=True,
            default_staffing_count=1,
            active=True,
        )
        first = self._create_assignment_physician(
            'night.builder.max.first@example.com',
            'Night Builder Max First',
            facilities=[self.facility],
        )
        second = self._create_assignment_physician(
            'night.builder.max.second@example.com',
            'Night Builder Max Second',
            facilities=[self.facility],
        )
        ineligible = self._create_assignment_physician(
            'night.builder.max.ineligible@example.com',
            'Night Builder Max Ineligible',
            facilities=[],
        )
        for physician in [first, second, ineligible]:
            contract = Contract.objects.get(user_assignments__physician=physician)
            contract.night_settings = {
                'period_rules': [
                    {
                        'period_type': 'SCHEDULE_BLOCK',
                        'min_shifts': '2' if physician != ineligible else '0',
                        'min_penalty_weight': '50000',
                    }
                ],
                'min_consecutive_night_shifts': '2',
                'max_consecutive_night_shifts': '2',
                'max_consecutive_night_shifts_penalty_weight': '50000',
            }
            contract.save(update_fields=['night_settings', 'updated_at'])
        for day in [1, 2, 3, 4]:
            self._create_shift_instance(version, night_template, date(2026, 7, day))

        response = self.client.post(
            f'/api/schedule-versions/{version.id}/run-optimizer/',
            data={'seed': 707},
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['unfilled_shift_count'], 0)
        for physician in [first, second]:
            night_dates = list(
                ScheduleShiftAssignment.objects.filter(
                    shift_instance__schedule_version=version,
                    shift_instance__shift_template=night_template,
                    physician=physician,
                )
                .order_by('shift_instance__date')
                .values_list('shift_instance__date', flat=True)
            )
            self.assertEqual(len(night_dates), 2)
            self.assertEqual(night_dates[1], night_dates[0] + timedelta(days=1))
        self.assertFalse(
            ScheduleShiftAssignment.objects.filter(
                shift_instance__schedule_version=version,
                shift_instance__shift_template=night_template,
                physician=ineligible,
            ).exists()
        )
        self.assertEqual(payload['debug']['max_consecutive_night_violations'], [])
        self.assertEqual(payload['debug']['night_block_assignment_rejections_by_reason'], {})

    def test_night_block_builder_preserves_manual_night_assignment(self):
        version = self._create_build_version(date(2026, 7, 1), date(2026, 7, 3))
        night_template = ShiftTemplate.objects.create(
            facility=self.facility,
            start_time=time(19, 0),
            end_time=time(7, 0),
            active_days_of_week=['Wednesday', 'Thursday', 'Friday'],
            weekend_days=[],
            night_shift=True,
            default_staffing_count=1,
            active=True,
        )
        manual = self._create_assignment_physician(
            'night.builder.manual@example.com',
            'Night Builder Manual',
            facilities=[self.facility],
        )
        backup = self._create_assignment_physician(
            'night.builder.backup@example.com',
            'Night Builder Backup',
            facilities=[self.facility],
        )
        for physician in [manual, backup]:
            contract = Contract.objects.get(user_assignments__physician=physician)
            contract.night_settings = {
                'period_rules': [
                    {
                        'period_type': 'SCHEDULE_BLOCK',
                        'min_shifts': '2',
                        'min_penalty_weight': '50000',
                    }
                ],
                'min_consecutive_night_shifts': '2',
                'max_consecutive_night_shifts': '3',
            }
            contract.save(update_fields=['night_settings', 'updated_at'])
        first_night = self._create_shift_instance(version, night_template, date(2026, 7, 1))
        for day in [2, 3]:
            self._create_shift_instance(version, night_template, date(2026, 7, day))
        manual_assignment = ScheduleShiftAssignment.objects.create(
            shift_instance=first_night,
            physician=manual,
            created_by=self.scheduler_user,
            assignment_source=ScheduleShiftAssignment.AssignmentSource.MANUAL,
        )

        response = self.client.post(
            f'/api/schedule-versions/{version.id}/run-optimizer/',
            data={'seed': 808},
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        manual_assignment.refresh_from_db()
        self.assertEqual(manual_assignment.shift_instance_id, first_night.id)
        self.assertEqual(manual_assignment.physician_id, manual.id)
        manual_night_count = ScheduleShiftAssignment.objects.filter(
            shift_instance__schedule_version=version,
            shift_instance__shift_template=night_template,
            physician=manual,
        ).count()
        self.assertGreaterEqual(manual_night_count, 1)

    def test_optimizer_reruns_create_history_and_active_view_does_not_overstaff(self):
        self.client.post(
            f'/api/schedule-blocks/{self.block.id}/build/generate/',
            data={'domain_id': self.domain.id},
            format='json',
        )
        version = ScheduleVersion.objects.get(schedule_block=self.block)
        for index in range(4):
            self._create_assignment_physician(
                f'idempotent{index}@example.com',
                f'Idempotent {index}',
                facilities=[self.facility],
            )

        first_response = self.client.post(
            f'/api/schedule-blocks/{self.block.id}/build/versions/{version.id}/optimize/',
            format='json',
        )
        first_run = OptimizerRun.objects.get(schedule_version=version, run_number=1)
        first_assignment_count = ScheduleShiftAssignment.objects.filter(
            shift_instance__schedule_version=version,
            optimizer_run=first_run,
        ).count()
        second_response = self.client.post(
            f'/api/schedule-blocks/{self.block.id}/build/versions/{version.id}/optimize/',
            format='json',
        )

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(second_response.status_code, 200)
        self.assertEqual(OptimizerRun.objects.filter(schedule_version=version).count(), 2)
        second_run = OptimizerRun.objects.get(schedule_version=version, run_number=2)
        self.assertFalse(OptimizerRun.objects.get(id=first_run.id).is_active)
        self.assertTrue(second_run.is_active)
        self.assertEqual(second_response.json()['assignments_made'], first_assignment_count)
        self.assertEqual(
            ScheduleShiftAssignment.objects.filter(
                shift_instance__schedule_version=version,
                optimizer_run=first_run,
            ).count(),
            first_assignment_count,
        )
        self.assertEqual(
            ScheduleShiftAssignment.objects.filter(
                shift_instance__schedule_version=version,
                optimizer_run=second_run,
            ).count(),
            first_assignment_count,
        )
        context_response = self.client.get(
            f'/api/schedule-blocks/{self.block.id}/build/?version_id={version.id}'
        )
        self.assertEqual(context_response.status_code, 200)
        for instance in context_response.json()['shift_instances']:
            self.assertLessEqual(instance['assigned_count'], instance['required_staffing'])
            self.assertEqual(instance['assigned_count'], instance['required_staffing'])

    def test_optimizer_seed_is_stored_and_returned_in_summary_debug(self):
        self.client.post(
            f'/api/schedule-blocks/{self.block.id}/build/generate/',
            data={'domain_id': self.domain.id},
            format='json',
        )
        version = ScheduleVersion.objects.get(schedule_block=self.block)
        for index in range(4):
            self._create_assignment_physician(
                f'seeded{index}@example.com',
                f'Seeded {index}',
                facilities=[self.facility],
            )

        response = self.client.post(
            f'/api/schedule-versions/{version.id}/run-optimizer/',
            data={'seed': 12345},
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        optimizer_run = OptimizerRun.objects.get(id=payload['optimizer_run_id'])
        self.assertEqual(payload['seed'], 12345)
        self.assertEqual(payload['debug']['seed'], 12345)
        self.assertEqual(optimizer_run.seed, 12345)
        self.assertEqual(optimizer_run.optimizer_summary['seed'], 12345)
        self.assertEqual(optimizer_run.optimizer_debug['seed'], 12345)

    def test_optimizer_same_seed_reproduces_assignment_signature(self):
        self.client.post(
            f'/api/schedule-blocks/{self.block.id}/build/generate/',
            data={'domain_id': self.domain.id},
            format='json',
        )
        version = ScheduleVersion.objects.get(schedule_block=self.block)
        for index in range(5):
            self._create_assignment_physician(
                f'replay{index}@example.com',
                f'Replay {index}',
                facilities=[self.facility],
            )

        first_response = self.client.post(
            f'/api/schedule-versions/{version.id}/run-optimizer/',
            data={'seed': 20260709},
            format='json',
        )
        second_response = self.client.post(
            f'/api/schedule-versions/{version.id}/run-optimizer/',
            data={'seed': 20260709},
            format='json',
        )

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(second_response.status_code, 200)
        first_run = OptimizerRun.objects.get(id=first_response.json()['optimizer_run_id'])
        second_run = OptimizerRun.objects.get(id=second_response.json()['optimizer_run_id'])
        self.assertEqual(
            self._optimizer_run_signature(first_run),
            self._optimizer_run_signature(second_run),
        )

    def test_optimizer_different_seeds_produce_different_assignment_signature(self):
        self.client.post(
            f'/api/schedule-blocks/{self.block.id}/build/generate/',
            data={'domain_id': self.domain.id},
            format='json',
        )
        version = ScheduleVersion.objects.get(schedule_block=self.block)
        for index in range(7):
            self._create_assignment_physician(
                f'diverse{index}@example.com',
                f'Diverse {index}',
                facilities=[self.facility],
            )

        first_response = self.client.post(
            f'/api/schedule-versions/{version.id}/run-optimizer/',
            data={'seed': 101},
            format='json',
        )
        second_response = self.client.post(
            f'/api/schedule-versions/{version.id}/run-optimizer/',
            data={'seed': 202},
            format='json',
        )

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(second_response.status_code, 200)
        first_run = OptimizerRun.objects.get(id=first_response.json()['optimizer_run_id'])
        second_run = OptimizerRun.objects.get(id=second_response.json()['optimizer_run_id'])
        self.assertNotEqual(
            self._optimizer_run_signature(first_run),
            self._optimizer_run_signature(second_run),
        )

    def test_optimizer_generated_seeds_are_distinct_for_repeated_runs(self):
        self.client.post(
            f'/api/schedule-blocks/{self.block.id}/build/generate/',
            data={'domain_id': self.domain.id},
            format='json',
        )
        version = ScheduleVersion.objects.get(schedule_block=self.block)
        for index in range(5):
            self._create_assignment_physician(
                f'generatedseed{index}@example.com',
                f'Generated Seed {index}',
                facilities=[self.facility],
            )

        first_response = self.client.post(f'/api/schedule-versions/{version.id}/run-optimizer/', format='json')
        second_response = self.client.post(f'/api/schedule-versions/{version.id}/run-optimizer/', format='json')
        third_response = self.client.post(f'/api/schedule-versions/{version.id}/run-optimizer/', format='json')

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(second_response.status_code, 200)
        self.assertEqual(third_response.status_code, 200)
        seeds = {
            first_response.json()['seed'],
            second_response.json()['seed'],
            third_response.json()['seed'],
        }
        self.assertEqual(len(seeds), 3)

    def test_activating_prior_optimizer_run_switches_active_summary(self):
        self.client.post(
            f'/api/schedule-blocks/{self.block.id}/build/generate/',
            data={'domain_id': self.domain.id},
            format='json',
        )
        version = ScheduleVersion.objects.get(schedule_block=self.block)
        for index in range(4):
            self._create_assignment_physician(
                f'activate{index}@example.com',
                f'Activate {index}',
                facilities=[self.facility],
            )

        first_response = self.client.post(
            f'/api/schedule-versions/{version.id}/run-optimizer/',
            format='json',
        )
        second_response = self.client.post(
            f'/api/schedule-versions/{version.id}/run-optimizer/',
            format='json',
        )
        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(second_response.status_code, 200)
        run_one = OptimizerRun.objects.get(schedule_version=version, run_number=1)
        run_two = OptimizerRun.objects.get(schedule_version=version, run_number=2)

        activate_response = self.client.post(f'/api/optimizer-runs/{run_one.id}/activate/')

        self.assertEqual(activate_response.status_code, 200)
        run_one.refresh_from_db()
        run_two.refresh_from_db()
        self.assertTrue(run_one.is_active)
        self.assertFalse(run_two.is_active)
        context_response = self.client.get(
            f'/api/schedule-blocks/{self.block.id}/build/?version_id={version.id}'
        )
        self.assertEqual(context_response.status_code, 200)
        context_payload = context_response.json()
        self.assertEqual(context_payload['selected_optimizer_run']['id'], run_one.id)
        self.assertEqual(context_payload['optimizer_summary']['optimizer_run_id'], run_one.id)

    def test_optimizer_run_violation_report_uses_selected_or_active_run(self):
        self.client.post(
            f'/api/schedule-blocks/{self.block.id}/build/generate/',
            data={'domain_id': self.domain.id},
            format='json',
        )
        version = ScheduleVersion.objects.get(schedule_block=self.block)
        for index in range(3):
            self._create_assignment_physician(
                f'runreport{index}@example.com',
                f'Run Report {index}',
                facilities=[self.facility],
            )
        self.client.post(f'/api/schedule-versions/{version.id}/run-optimizer/', format='json')
        self.client.post(f'/api/schedule-versions/{version.id}/run-optimizer/', format='json')
        active_run = OptimizerRun.objects.get(schedule_version=version, is_active=True)
        run_one = OptimizerRun.objects.get(schedule_version=version, run_number=1)

        active_response = self.client.get(f'/api/schedule-versions/{version.id}/violation-report/')
        selected_response = self.client.get(f'/api/optimizer-runs/{run_one.id}/violations/')

        self.assertEqual(active_response.status_code, 200)
        self.assertEqual(selected_response.status_code, 200)
        self.assertEqual(active_response.json()['optimizer_run']['id'], active_run.id)
        self.assertEqual(selected_response.json()['optimizer_run']['id'], run_one.id)
        self.assertEqual(selected_response.json()['schedule_version']['id'], version.id)

    def test_build_context_with_optimizer_run_id_loads_exact_run_without_activating(self):
        self.client.post(
            f'/api/schedule-blocks/{self.block.id}/build/generate/',
            data={'domain_id': self.domain.id},
            format='json',
        )
        version = ScheduleVersion.objects.get(schedule_block=self.block)
        for index in range(6):
            self._create_assignment_physician(
                f'viewrun{index}@example.com',
                f'View Run {index}',
                facilities=[self.facility],
            )
        first_response = self.client.post(
            f'/api/schedule-versions/{version.id}/run-optimizer/',
            data={'seed': 101},
            format='json',
        )
        second_response = self.client.post(
            f'/api/schedule-versions/{version.id}/run-optimizer/',
            data={'seed': 202},
            format='json',
        )
        run_one = OptimizerRun.objects.get(id=first_response.json()['optimizer_run_id'])
        run_two = OptimizerRun.objects.get(id=second_response.json()['optimizer_run_id'])
        self.assertTrue(run_two.is_active)

        context_response = self.client.get(
            f'/api/schedule-blocks/{self.block.id}/build/?optimizer_run_id={run_one.id}'
        )

        self.assertEqual(context_response.status_code, 200)
        payload = context_response.json()
        self.assertEqual(payload['selected_optimizer_run']['id'], run_one.id)
        self.assertEqual(payload['optimizer_summary']['optimizer_run_id'], run_one.id)
        self.assertEqual(
            {
                (assignment['shift_instance'], assignment['physician'])
                for instance in payload['shift_instances']
                for assignment in instance['assignments']
            },
            set(self._optimizer_run_signature(run_one)),
        )
        run_one.refresh_from_db()
        run_two.refresh_from_db()
        self.assertFalse(run_one.is_active)
        self.assertTrue(run_two.is_active)

    def test_build_context_without_optimizer_run_id_loads_active_run(self):
        self.client.post(
            f'/api/schedule-blocks/{self.block.id}/build/generate/',
            data={'domain_id': self.domain.id},
            format='json',
        )
        version = ScheduleVersion.objects.get(schedule_block=self.block)
        for index in range(5):
            self._create_assignment_physician(
                f'activerun{index}@example.com',
                f'Active Run {index}',
                facilities=[self.facility],
            )
        self.client.post(f'/api/schedule-versions/{version.id}/run-optimizer/', data={'seed': 11}, format='json')
        second_response = self.client.post(f'/api/schedule-versions/{version.id}/run-optimizer/', data={'seed': 22}, format='json')
        active_run = OptimizerRun.objects.get(id=second_response.json()['optimizer_run_id'])

        context_response = self.client.get(
            f'/api/schedule-blocks/{self.block.id}/build/'
        )

        self.assertEqual(context_response.status_code, 200)
        self.assertEqual(context_response.json()['selected_optimizer_run']['id'], active_run.id)

    def test_delete_inactive_optimizer_run_removes_only_that_run_assignments(self):
        self.client.post(
            f'/api/schedule-blocks/{self.block.id}/build/generate/',
            data={'domain_id': self.domain.id},
            format='json',
        )
        version = ScheduleVersion.objects.get(schedule_block=self.block)
        for index in range(5):
            self._create_assignment_physician(
                f'deleterun{index}@example.com',
                f'Delete Run {index}',
                facilities=[self.facility],
            )
        first_response = self.client.post(f'/api/schedule-versions/{version.id}/run-optimizer/', data={'seed': 301}, format='json')
        second_response = self.client.post(f'/api/schedule-versions/{version.id}/run-optimizer/', data={'seed': 302}, format='json')
        run_one = OptimizerRun.objects.get(id=first_response.json()['optimizer_run_id'])
        run_two = OptimizerRun.objects.get(id=second_response.json()['optimizer_run_id'])
        self.assertTrue(run_two.is_active)
        run_one_assignment_count = ScheduleShiftAssignment.objects.filter(optimizer_run=run_one).count()
        run_two_assignment_count = ScheduleShiftAssignment.objects.filter(optimizer_run=run_two).count()

        delete_response = self.client.delete(f'/api/optimizer-runs/{run_one.id}/')

        self.assertEqual(delete_response.status_code, 200)
        self.assertFalse(OptimizerRun.objects.filter(id=run_one.id).exists())
        self.assertFalse(ScheduleShiftAssignment.objects.filter(optimizer_run=run_one).exists())
        self.assertEqual(ScheduleShiftAssignment.objects.filter(optimizer_run=run_two).count(), run_two_assignment_count)
        self.assertEqual(delete_response.json()['assignments_deleted'], run_one_assignment_count)
        run_two.refresh_from_db()
        self.assertTrue(run_two.is_active)

    def test_delete_active_optimizer_run_fails(self):
        self.client.post(
            f'/api/schedule-blocks/{self.block.id}/build/generate/',
            data={'domain_id': self.domain.id},
            format='json',
        )
        version = ScheduleVersion.objects.get(schedule_block=self.block)
        for index in range(4):
            self._create_assignment_physician(
                f'active-delete{index}@example.com',
                f'Active Delete {index}',
                facilities=[self.facility],
            )
        response = self.client.post(f'/api/schedule-versions/{version.id}/run-optimizer/', data={'seed': 401}, format='json')
        active_run = OptimizerRun.objects.get(id=response.json()['optimizer_run_id'])

        delete_response = self.client.delete(f'/api/optimizer-runs/{active_run.id}/')

        self.assertEqual(delete_response.status_code, 400)
        self.assertEqual(
            delete_response.json()['detail'],
            'Cannot delete active optimizer run. Activate another run first.',
        )
        self.assertTrue(OptimizerRun.objects.filter(id=active_run.id).exists())

    def test_delete_failed_optimizer_run_succeeds_without_touching_active_run(self):
        self.client.post(
            f'/api/schedule-blocks/{self.block.id}/build/generate/',
            data={'domain_id': self.domain.id},
            format='json',
        )
        version = ScheduleVersion.objects.get(schedule_block=self.block)
        for index in range(4):
            self._create_assignment_physician(
                f'failed-delete{index}@example.com',
                f'Failed Delete {index}',
                facilities=[self.facility],
            )
        completed_response = self.client.post(
            f'/api/schedule-versions/{version.id}/run-optimizer/',
            data={'seed': 451},
            format='json',
        )
        active_run = OptimizerRun.objects.get(id=completed_response.json()['optimizer_run_id'])
        active_assignment_count = ScheduleShiftAssignment.objects.filter(optimizer_run=active_run).count()
        failed_run = OptimizerRun.objects.create(
            schedule_version=version,
            run_number=active_run.run_number + 1,
            created_by=self.scheduler_user,
            status=OptimizerRun.Status.FAILED,
            seed=452,
        )
        failed_assignment = ScheduleShiftAssignment.objects.filter(optimizer_run=active_run).first()
        if failed_assignment is not None:
            failed_assignment.id = None
            failed_assignment.optimizer_run = failed_run
            failed_assignment.assignment_source = ScheduleShiftAssignment.AssignmentSource.OPTIMIZER
            failed_assignment.save()

        delete_response = self.client.delete(f'/api/optimizer-runs/{failed_run.id}/')

        self.assertEqual(delete_response.status_code, 200)
        self.assertFalse(OptimizerRun.objects.filter(id=failed_run.id).exists())
        active_run.refresh_from_db()
        self.assertTrue(active_run.is_active)
        self.assertEqual(ScheduleShiftAssignment.objects.filter(optimizer_run=active_run).count(), active_assignment_count)

    def test_delete_fresh_running_optimizer_run_is_blocked(self):
        self.client.post(
            f'/api/schedule-blocks/{self.block.id}/build/generate/',
            data={'domain_id': self.domain.id},
            format='json',
        )
        version = ScheduleVersion.objects.get(schedule_block=self.block)
        running_run = OptimizerRun.objects.create(
            schedule_version=version,
            run_number=1,
            created_by=self.scheduler_user,
            status=OptimizerRun.Status.RUNNING,
            seed=453,
        )

        delete_response = self.client.delete(f'/api/optimizer-runs/{running_run.id}/')

        self.assertEqual(delete_response.status_code, 400)
        self.assertTrue(OptimizerRun.objects.filter(id=running_run.id).exists())

    def test_build_context_ignores_failed_latest_optimizer_run(self):
        self.client.post(
            f'/api/schedule-blocks/{self.block.id}/build/generate/',
            data={'domain_id': self.domain.id},
            format='json',
        )
        version = ScheduleVersion.objects.get(schedule_block=self.block)
        for index in range(4):
            self._create_assignment_physician(
                f'failed-latest{index}@example.com',
                f'Failed Latest {index}',
                facilities=[self.facility],
            )
        completed_response = self.client.post(
            f'/api/schedule-versions/{version.id}/run-optimizer/',
            data={'seed': 501},
            format='json',
        )
        active_run = OptimizerRun.objects.get(id=completed_response.json()['optimizer_run_id'])
        failed_run = OptimizerRun.objects.create(
            schedule_version=version,
            run_number=active_run.run_number + 1,
            created_by=self.scheduler_user,
            status=OptimizerRun.Status.FAILED,
            seed=999,
        )

        context_response = self.client.get(
            f'/api/schedule-blocks/{self.block.id}/build/?optimizer_run_id={failed_run.id}'
        )

        self.assertEqual(context_response.status_code, 200)
        payload = context_response.json()
        self.assertEqual(payload['selected_optimizer_run']['id'], active_run.id)
        self.assertNotEqual(payload['selected_optimizer_run']['id'], failed_run.id)
        active_run.refresh_from_db()
        self.assertTrue(active_run.is_active)

    def test_stale_running_optimizer_run_is_marked_failed_and_ignored(self):
        self.client.post(
            f'/api/schedule-blocks/{self.block.id}/build/generate/',
            data={'domain_id': self.domain.id},
            format='json',
        )
        version = ScheduleVersion.objects.get(schedule_block=self.block)
        stale_run = OptimizerRun.objects.create(
            schedule_version=version,
            run_number=1,
            created_by=self.scheduler_user,
            status=OptimizerRun.Status.RUNNING,
            seed=111,
        )
        OptimizerRun.objects.filter(id=stale_run.id).update(
            created_at=timezone.now() - timedelta(minutes=11)
        )

        context_response = self.client.get(f'/api/schedule-blocks/{self.block.id}/build/')

        self.assertEqual(context_response.status_code, 200)
        stale_run.refresh_from_db()
        self.assertEqual(stale_run.status, OptimizerRun.Status.FAILED)
        self.assertFalse(stale_run.is_active)
        self.assertIsNone(context_response.json()['selected_optimizer_run'])

    def test_concurrent_optimizer_run_is_blocked(self):
        self.client.post(
            f'/api/schedule-blocks/{self.block.id}/build/generate/',
            data={'domain_id': self.domain.id},
            format='json',
        )
        version = ScheduleVersion.objects.get(schedule_block=self.block)
        running_run = OptimizerRun.objects.create(
            schedule_version=version,
            run_number=1,
            created_by=self.scheduler_user,
            status=OptimizerRun.Status.RUNNING,
            seed=222,
        )

        response = self.client.post(
            f'/api/schedule-versions/{version.id}/run-optimizer/',
            format='json',
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['optimizer_run_id'], running_run.id)
        running_run.refresh_from_db()
        self.assertEqual(running_run.status, OptimizerRun.Status.RUNNING)

    def test_optimizer_timeout_does_not_replace_previous_active_run(self):
        self.client.post(
            f'/api/schedule-blocks/{self.block.id}/build/generate/',
            data={'domain_id': self.domain.id},
            format='json',
        )
        version = ScheduleVersion.objects.get(schedule_block=self.block)
        for index in range(4):
            self._create_assignment_physician(
                f'timeout-safe{index}@example.com',
                f'Timeout Safe {index}',
                facilities=[self.facility],
            )
        completed_response = self.client.post(
            f'/api/schedule-versions/{version.id}/run-optimizer/',
            data={'seed': 601},
            format='json',
        )
        active_run = OptimizerRun.objects.get(id=completed_response.json()['optimizer_run_id'])

        with patch('apps.scheduling.optimizer.MAX_RUNTIME_SECONDS', 0):
            timeout_response = self.client.post(
                f'/api/schedule-versions/{version.id}/run-optimizer/',
                data={'seed': 602},
                format='json',
            )

        self.assertEqual(timeout_response.status_code, 200)
        timeout_payload = timeout_response.json()
        self.assertTrue(timeout_payload['timed_out'])
        self.assertEqual(
            timeout_payload['message'],
            'Optimizer stopped after runtime limit. Previous active run preserved.',
        )
        self.assertFalse(timeout_payload['debug']['night_block_builder_enabled'])
        self.assertTrue(timeout_payload['debug']['night_block_builder_skipped'])
        self.assertEqual(
            timeout_payload['debug']['night_block_builder_disabled_reason'],
            'Disabled after runtime regression',
        )
        timeout_run = OptimizerRun.objects.get(id=timeout_payload['optimizer_run_id'])
        self.assertEqual(timeout_run.status, OptimizerRun.Status.FAILED)
        self.assertFalse(timeout_run.is_active)
        active_run.refresh_from_db()
        self.assertTrue(active_run.is_active)

    def test_optimizer_balances_unbalanced_workload_scoring(self):
        self.day_template.default_staffing_count = 1
        self.day_template.save(update_fields=['default_staffing_count'])
        self.overnight_template.active = False
        self.overnight_template.save(update_fields=['active'])
        ShiftTemplate.objects.create(
            facility=self.facility,
            start_time=time(7, 0),
            end_time=time(16, 0),
            active_days_of_week=['Tuesday'],
            weekend_days=[],
            night_shift=False,
            default_staffing_count=1,
            active=True,
        )
        self.client.post(
            f'/api/schedule-blocks/{self.block.id}/build/generate/',
            data={'domain_id': self.domain.id},
            format='json',
        )
        version = ScheduleVersion.objects.get(schedule_block=self.block)
        first = self._create_assignment_physician(
            'able.improvement@example.com',
            'Able Improvement',
            facilities=[self.facility],
        )
        second = self._create_assignment_physician(
            'baker.improvement@example.com',
            'Baker Improvement',
            facilities=[self.facility],
        )
        for physician in [first, second]:
            contract = Contract.objects.get(user_assignments__physician=physician)
            contract.workload_settings = {
                'period_rules': [
                    {
                        'units': 'HOURS',
                        'min_value': '9',
                        'max_value': '9',
                    }
                ]
            }
            contract.save(update_fields=['workload_settings', 'updated_at'])

        response = self.client.post(
            f'/api/schedule-blocks/{self.block.id}/build/versions/{version.id}/optimize/',
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['final_rest_violations'], 0)
        self.assertEqual(payload['final_overlap_violations'], 0)
        self.assertEqual(payload['final_duplicate_violations'], 0)
        self.assertEqual(payload['final_overstaffed_violations'], 0)
        self.assertIn('workload_score', payload['score_breakdown'])
        self.assertIn('consecutive_days_score', payload['score_breakdown'])
        self.assertIn('same_shift_score', payload['score_breakdown'])
        self.assertIn('night_score', payload['score_breakdown'])
        self.assertIn('weekend_score', payload['score_breakdown'])
        self.assertIn('facility_distribution_score', payload['score_breakdown'])
        self.assertIn('same_shift_violations_count', payload)
        self.assertIn('same_shift_violations_initial', payload['debug'])
        self.assertIn('same_shift_violations_final', payload['debug'])
        self.assertIn('same_shift_fix_attempts', payload['debug'])
        self.assertIn('same_shift_fix_valid_alternatives', payload['debug'])
        self.assertIn('same_shift_fix_improvements', payload['debug'])
        self.assertIn('same_shift_violations', payload['debug'])
        self.assertIn('night_violations_count', payload)
        self.assertIn('total_night_shifts', payload)
        self.assertIn('max_nights_assigned_to_one_physician', payload)
        self.assertIn('night_fix_improvements', payload)
        self.assertIn('night_score_initial', payload['debug'])
        self.assertIn('night_score_final', payload['debug'])
        self.assertIn('night_fix_attempts', payload['debug'])
        self.assertIn('night_fix_valid_alternatives', payload['debug'])
        self.assertIn('night_violations', payload['debug'])
        self.assertIn('night_blocks_by_physician', payload['debug'])
        self.assertIn('isolated_night_count', payload['debug'])
        self.assertIn('night_blocks_count', payload['debug'])
        self.assertIn('average_night_block_length', payload['debug'])
        self.assertIn('max_night_block_length', payload['debug'])
        self.assertIn('post_night_recovery_violations_count', payload['debug'])
        self.assertIn('next_night_block_recovery_violations_count', payload['debug'])
        self.assertIn('night_block_assignment_attempts', payload['debug'])
        self.assertIn('night_block_assignment_successes', payload['debug'])
        self.assertIn('nonnight_assignments_blocked_by_recovery', payload['debug'])
        self.assertIn('nonnight_assignments_allowed_despite_recovery', payload['debug'])
        self.assertLessEqual(payload['final_score'], payload['initial_score'])
        self.assertIn('phase_order', payload['debug'])
        self.assertIn('phase_passes_run', payload['debug'])
        self.assertIn('phase_attempts', payload['debug'])
        self.assertIn('phase_improvements', payload['debug'])
        self.assertIn('request_repair_attempts', payload['debug'])
        self.assertIn('request_repair_improvements', payload['debug'])
        self.assertIn('night_minimum_repair_attempts', payload['debug'])
        self.assertIn('night_minimum_repair_improvements', payload['debug'])
        self.assertIn('post_night_recovery_repair_attempts', payload['debug'])
        self.assertIn('post_night_recovery_repair_improvements', payload['debug'])
        self.assertIn('workload_repair_attempts', payload['debug'])
        self.assertIn('workload_repair_improvements', payload['debug'])
        self.assertIn('workload_over_range_count_initial', payload['debug'])
        self.assertIn('workload_under_range_count_initial', payload['debug'])
        self.assertIn('workload_over_range_count_final', payload['debug'])
        self.assertIn('workload_under_range_count_final', payload['debug'])
        self.assertIn('workload_candidate_moves_considered', payload['debug'])
        self.assertIn('workload_candidate_swaps_considered', payload['debug'])
        self.assertIn('workload_moves_accepted', payload['debug'])
        self.assertIn('workload_swaps_accepted', payload['debug'])
        self.assertIn('workload_score_initial', payload['debug'])
        self.assertIn('workload_score_final', payload['debug'])
        self.assertIn('general_swap_attempts', payload['debug'])
        self.assertIn('general_swap_improvements', payload['debug'])
        self.assertIn('stopped_reason', payload['debug'])
        self.assertIn('runtime_seconds', payload['debug'])
        self.assertEqual(payload['score_breakdown']['workload_score'], 0)
        if payload['debug']['workload_over_range_count_initial'] > 0:
            self.assertGreater(payload['iterations_run'], 0)
            self.assertGreater(payload['debug']['workload_repair_attempts'], 0)
        if payload['debug']['phase_attempts']['general_hill_climb_swaps'] > 0:
            self.assertGreater(payload['debug']['general_swap_attempts'], 0)
        if payload['score_breakdown']['same_shift_score'] > 0:
            self.assertGreater(payload['debug']['same_shift_fix_attempts'], 0)
        if payload['iterations_run'] > 0:
            self.assertGreater(payload['debug']['reassignment_moves_attempted'], 0)
        assigned_counts = dict(
            ScheduleShiftAssignment.objects.filter(shift_instance__schedule_version=version)
            .values('physician_id')
            .annotate(row_count=Count('id'))
            .values_list('physician_id', 'row_count')
        )
        self.assertEqual(assigned_counts[first.id], 1)
        self.assertEqual(assigned_counts[second.id], 1)

    def test_workload_min_max_range_scores_only_outside_range(self):
        self.block.start_date = date(2026, 7, 1)
        self.block.end_date = date(2026, 7, 18)
        self.block.build_status = ScheduleBlock.BuildStatus.BUILD
        self.block.save(update_fields=['start_date', 'end_date', 'build_status', 'updated_at'])
        version = ScheduleVersion.objects.create(
            schedule_block=self.block,
            domain=self.domain,
            version_number=1,
            name='Build 1',
            status=ScheduleVersion.Status.BUILD,
        )
        template = ShiftTemplate.objects.create(
            facility=self.facility,
            start_time=time(7, 0),
            end_time=time(16, 0),
            active_days_of_week=['Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday', 'Monday', 'Tuesday'],
            weekend_days=[],
            night_shift=False,
            default_staffing_count=1,
            active=True,
        )
        physicians = [
            self._create_assignment_physician(
                f'workload.range{index}@example.com',
                f'Workload Range {index}',
                facilities=[self.facility],
            )
            for index in range(3)
        ]
        instances = [
            ScheduleShiftInstance.objects.create(
                schedule_block=self.block,
                schedule_version=version,
                shift_template=template,
                facility=self.facility,
                date=date(2026, 7, 1) + timedelta(days=index),
                start_datetime=timezone.make_aware(datetime(2026, 7, 1, 7, 0) + timedelta(days=index)),
                end_datetime=timezone.make_aware(datetime(2026, 7, 1, 16, 0) + timedelta(days=index)),
                required_staffing=1,
            )
            for index in range(18)
        ]
        state = defaultdict(list)
        for instance in instances[:5]:
            state[instance.id].append(physicians[0].id)
        for instance in instances[5:11]:
            state[instance.id].append(physicians[1].id)
        for instance in instances[11:18]:
            state[instance.id].append(physicians[2].id)
        workload_rule = {
            'period_type': 'SCHEDULE_BLOCK',
            'units': 'HOURS',
            'min_value': Decimal('45'),
            'max_value': Decimal('55'),
            'min_penalty_weight': Decimal('10000'),
            'max_penalty_weight': Decimal('10000'),
        }
        targets = {
            physician.id: {
                'units': 'HOURS',
                'target': Decimal('50'),
                'rules': [workload_rule],
            }
            for physician in physicians
        }
        contract_by_physician = {
            assignment.physician_id: assignment.contract
            for assignment in ContractUserAssignment.objects.filter(
                physician__in=physicians,
            ).select_related('contract')
        }
        eligible_facilities_by_physician = {
            physician.id: {self.facility.id}
            for physician in physicians
        }
        minimum_rest_by_physician = {
            physician.id: Decimal('0')
            for physician in physicians
        }

        scoring = _score_schedule(
            instances,
            physicians,
            state,
            targets,
            contract_by_physician,
            defaultdict(list),
            eligible_facilities_by_physician,
            minimum_rest_by_physician,
        )

        workload_rows = {
            row['physician_id']: row
            for row in scoring['workload_score_rows']
        }
        self.assertEqual(workload_rows[physicians[0].id]['assigned_hours'], 45.0)
        self.assertEqual(workload_rows[physicians[0].id]['score_contribution'], 0.0)
        self.assertEqual(workload_rows[physicians[1].id]['assigned_hours'], 54.0)
        self.assertEqual(workload_rows[physicians[1].id]['score_contribution'], 0.0)
        self.assertEqual(workload_rows[physicians[2].id]['assigned_hours'], 63.0)
        self.assertEqual(workload_rows[physicians[2].id]['deviation_direction'], 'above_maximum')
        self.assertEqual(workload_rows[physicians[2].id]['deviation'], 8.0)
        self.assertEqual(workload_rows[physicians[2].id]['score_contribution'], 80000.0)
        self.assertEqual(float(scoring['breakdown']['workload_score']), 80000.0)

    def test_lower_workload_contract_limits_initial_allocation(self):
        version = self._create_build_version(date(2026, 7, 1), date(2026, 7, 6))
        day_template = ShiftTemplate.objects.create(
            facility=self.facility,
            start_time=time(7, 0),
            end_time=time(16, 0),
            active_days_of_week=['Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday', 'Monday'],
            weekend_days=[],
            night_shift=False,
            default_staffing_count=1,
            active=True,
        )
        low = self._create_assignment_physician(
            'lower.contract@example.com',
            'Lower Contract',
            facilities=[self.facility],
        )
        full_one = self._create_assignment_physician(
            'full.contract.one@example.com',
            'Full Contract One',
            facilities=[self.facility],
        )
        full_two = self._create_assignment_physician(
            'full.contract.two@example.com',
            'Full Contract Two',
            facilities=[self.facility],
        )
        low_contract = Contract.objects.get(user_assignments__physician=low)
        low_contract.workload_settings = {
            'period_rules': [
                {
                    'period_type': 'SCHEDULE_BLOCK',
                    'units': 'SHIFTS',
                    'min_value': '0',
                    'max_value': '1',
                    'max_penalty_weight': '50000',
                }
            ]
        }
        low_contract.save(update_fields=['workload_settings', 'updated_at'])
        for physician in [full_one, full_two]:
            contract = Contract.objects.get(user_assignments__physician=physician)
            contract.workload_settings = {
                'period_rules': [
                    {
                        'period_type': 'SCHEDULE_BLOCK',
                        'units': 'SHIFTS',
                        'min_value': '2',
                        'max_value': '6',
                        'max_penalty_weight': '50000',
                    }
                ]
            }
            contract.save(update_fields=['workload_settings', 'updated_at'])
        for day in range(1, 7):
            self._create_shift_instance(version, day_template, date(2026, 7, day))

        response = self.client.post(
            f'/api/schedule-versions/{version.id}/run-optimizer/',
            data={'seed': 901},
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        counts = dict(
            ScheduleShiftAssignment.objects.filter(
                optimizer_run_id=response.json()['optimizer_run_id'],
            )
            .values('physician_id')
            .annotate(row_count=Count('id'))
            .values_list('physician_id', 'row_count')
        )
        self.assertLessEqual(counts.get(low.id, 0), 1)
        self.assertGreaterEqual(counts.get(full_one.id, 0) + counts.get(full_two.id, 0), 5)
        summary = {
            row['physician_id']: row
            for row in response.json()['workload_summary']
        }
        self.assertEqual(summary[low.id]['contract_name'], 'Lower Contract Contract')
        self.assertEqual(summary[low.id]['effective_workload_range']['max_value'], 1.0)

    def test_month_workload_rule_is_prorated_for_partial_schedule_block(self):
        version = self._create_build_version(date(2026, 11, 1), date(2026, 11, 14))
        day_template = ShiftTemplate.objects.create(
            facility=self.facility,
            start_time=time(7, 0),
            end_time=time(16, 0),
            active_days_of_week=['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'],
            weekend_days=[],
            night_shift=False,
            default_staffing_count=1,
            active=True,
        )
        physician = self._create_assignment_physician(
            'month.prorated@example.com',
            'Month Prorated',
            facilities=[self.facility],
        )
        contract = Contract.objects.get(user_assignments__physician=physician)
        contract.workload_settings = {
            'period_rules': [
                {
                    'period_type': 'MONTH',
                    'units': 'SHIFTS',
                    'min_value': '20',
                    'max_value': '35',
                },
                {
                    'period_type': 'SCHEDULE_BLOCK',
                    'units': 'SHIFTS',
                    'min_value': '1',
                    'max_value': '14',
                },
            ]
        }
        contract.save(update_fields=['workload_settings', 'updated_at'])
        for day in range(1, 11):
            self._create_shift_instance(version, day_template, date(2026, 11, day))

        response = self.client.post(
            f'/api/schedule-versions/{version.id}/run-optimizer/',
            data={'seed': 902},
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        row = response.json()['debug']['workload_score_rows'][0]
        month_rule = next(item for item in row['rule_rows'] if item['period_type'] == 'MONTH')
        block_rule = next(item for item in row['rule_rows'] if item['period_type'] == 'SCHEDULE_BLOCK')
        self.assertEqual(month_rule['raw_min_value'], 20.0)
        self.assertEqual(month_rule['raw_max_value'], 35.0)
        self.assertEqual(month_rule['effective_min_value'], 9.0)
        self.assertEqual(month_rule['effective_max_value'], 17.0)
        self.assertIsNotNone(month_rule['proration'])
        self.assertEqual(block_rule['effective_min_value'], 1.0)
        self.assertEqual(block_rule['effective_max_value'], 14.0)
        summary_row = response.json()['workload_summary'][0]
        self.assertEqual(summary_row['contract_name'], 'Month Prorated Contract')
        self.assertEqual(summary_row['effective_workload_range']['min_value'], 9.0)

    def test_night_days_off_after_scores_next_assignment_once(self):
        self.block.start_date = date(2026, 7, 1)
        self.block.end_date = date(2026, 7, 7)
        self.block.build_status = ScheduleBlock.BuildStatus.BUILD
        self.block.save(update_fields=['start_date', 'end_date', 'build_status', 'updated_at'])
        version = ScheduleVersion.objects.create(
            schedule_block=self.block,
            domain=self.domain,
            version_number=1,
            name='Build 1',
            status=ScheduleVersion.Status.BUILD,
        )
        night_template = ShiftTemplate.objects.create(
            facility=self.facility,
            start_time=time(19, 0),
            end_time=time(7, 0),
            active_days_of_week=['Wednesday', 'Thursday'],
            weekend_days=[],
            night_shift=True,
            default_staffing_count=1,
            active=True,
        )
        day_template = ShiftTemplate.objects.create(
            facility=self.facility,
            start_time=time(7, 0),
            end_time=time(16, 0),
            active_days_of_week=['Friday', 'Saturday'],
            weekend_days=[],
            night_shift=False,
            default_staffing_count=1,
            active=True,
        )
        physician = self._create_assignment_physician(
            'night.boundary@example.com',
            'Night Boundary',
            facilities=[self.facility],
        )
        contract = Contract.objects.get(user_assignments__physician=physician)
        contract.night_settings = {
            'max_consecutive_night_shifts': '4',
            'days_off_after_night_block': '2',
            'days_off_after_night_block_penalty_weight': '1000',
        }
        contract.save(update_fields=['night_settings', 'updated_at'])

        night_one = ScheduleShiftInstance.objects.create(
            schedule_block=self.block,
            schedule_version=version,
            shift_template=night_template,
            facility=self.facility,
            date=date(2026, 7, 1),
            start_datetime=timezone.make_aware(datetime(2026, 7, 1, 19, 0)),
            end_datetime=timezone.make_aware(datetime(2026, 7, 2, 7, 0)),
            required_staffing=1,
        )
        night_two = ScheduleShiftInstance.objects.create(
            schedule_block=self.block,
            schedule_version=version,
            shift_template=night_template,
            facility=self.facility,
            date=date(2026, 7, 2),
            start_datetime=timezone.make_aware(datetime(2026, 7, 2, 19, 0)),
            end_datetime=timezone.make_aware(datetime(2026, 7, 3, 7, 0)),
            required_staffing=1,
        )
        day_one = ScheduleShiftInstance.objects.create(
            schedule_block=self.block,
            schedule_version=version,
            shift_template=day_template,
            facility=self.facility,
            date=date(2026, 7, 3),
            start_datetime=timezone.make_aware(datetime(2026, 7, 3, 7, 0)),
            end_datetime=timezone.make_aware(datetime(2026, 7, 3, 16, 0)),
            required_staffing=1,
        )
        day_two = ScheduleShiftInstance.objects.create(
            schedule_block=self.block,
            schedule_version=version,
            shift_template=day_template,
            facility=self.facility,
            date=date(2026, 7, 4),
            start_datetime=timezone.make_aware(datetime(2026, 7, 4, 7, 0)),
            end_datetime=timezone.make_aware(datetime(2026, 7, 4, 16, 0)),
            required_staffing=1,
        )
        instances = [night_one, night_two, day_one, day_two]
        state = defaultdict(list)
        for instance in instances:
            state[instance.id].append(physician.id)

        report = _night_violation_report(
            instances,
            [physician],
            state,
            {physician.id: contract},
        )

        after_violations = [
            violation for violation in report['night_violations']
            if violation['violation_type'] == 'INSUFFICIENT_DAYS_OFF_AFTER_NIGHT_BEFORE_NON_NIGHT'
        ]
        self.assertEqual(len(after_violations), 1)
        self.assertEqual(after_violations[0]['night_block_dates'], ['2026-07-01', '2026-07-02'])
        self.assertEqual(after_violations[0]['next_assignment']['shift_instance_id'], day_one.id)
        self.assertEqual(after_violations[0]['actual_value'], 0)
        self.assertEqual(after_violations[0]['penalty'], 2000.0)
        self.assertNotIn(day_two.id, after_violations[0]['shift_instance_ids'])

    def test_night_rules_are_evaluated_per_physician_contract(self):
        version = self._create_build_version(date(2026, 7, 1), date(2026, 7, 7))
        night_template = ShiftTemplate.objects.create(
            facility=self.facility,
            start_time=time(19, 0),
            end_time=time(7, 0),
            active_days_of_week=['Wednesday'],
            weekend_days=[],
            night_shift=True,
            default_staffing_count=1,
            active=True,
        )
        zero_min = self._create_assignment_physician(
            'night.zero.min@example.com',
            'Night Zero',
            facilities=[self.facility],
        )
        two_min = self._create_assignment_physician(
            'night.two.min@example.com',
            'Night Two',
            facilities=[self.facility],
        )
        zero_contract = Contract.objects.get(user_assignments__physician=zero_min)
        zero_contract.name = 'Zero Night Contract'
        zero_contract.night_settings = {
            'period_rules': [
                {
                    'period_type': 'SCHEDULE_BLOCK',
                    'min_shifts': '0',
                    'min_penalty_weight': '9000',
                }
            ]
        }
        zero_contract.save(update_fields=['name', 'night_settings', 'updated_at'])
        two_contract = Contract.objects.get(user_assignments__physician=two_min)
        two_contract.name = 'Two Night Contract'
        two_contract.night_settings = {
            'period_rules': [
                {
                    'period_type': 'SCHEDULE_BLOCK',
                    'min_shifts': '2',
                    'min_penalty_weight': '9000',
                }
            ],
            'min_consecutive_night_shifts': '2',
            'min_consecutive_night_shifts_penalty_weight': '700',
        }
        two_contract.save(update_fields=['name', 'night_settings', 'updated_at'])
        night = self._create_shift_instance(version, night_template, date(2026, 7, 1))
        state = defaultdict(list)
        state[night.id].append(two_min.id)

        report = _night_violation_report(
            [night],
            [zero_min, two_min],
            state,
            {
                zero_min.id: zero_contract,
                two_min.id: two_contract,
            },
        )

        zero_rows = [
            violation for violation in report['night_violations']
            if violation['physician_id'] == zero_min.id
        ]
        two_under_rows = [
            violation for violation in report['night_violations']
            if violation['physician_id'] == two_min.id
            and violation['violation_type'] == 'NIGHT_UNDER_MINIMUM'
        ]
        two_min_consecutive_rows = [
            violation for violation in report['night_violations']
            if violation['physician_id'] == two_min.id
            and violation['violation_type'] == 'MIN_CONSECUTIVE_NIGHTS'
        ]
        self.assertEqual(zero_rows, [])
        self.assertEqual(len(two_under_rows), 1)
        self.assertEqual(two_under_rows[0]['contract_name'], 'Two Night Contract')
        self.assertEqual(two_under_rows[0]['configured_limit'], 2)
        self.assertEqual(two_under_rows[0]['actual_value'], 1)
        self.assertEqual(len(two_min_consecutive_rows), 1)
        self.assertEqual(two_min_consecutive_rows[0]['configured_limit'], 2)
        self.assertEqual(two_min_consecutive_rows[0]['actual_value'], 1)
        self.assertEqual(
            report['night_minimum_violations_by_contract'][0]['contract_name'],
            'Two Night Contract',
        )
        self.assertIn('Two Night Contract', {
            item['contract_name'] for item in report['night_rules_by_contract']
        })

    def test_max_consecutive_nights_uses_physician_contract(self):
        version = self._create_build_version(date(2026, 7, 1), date(2026, 7, 4))
        night_template = ShiftTemplate.objects.create(
            facility=self.facility,
            start_time=time(19, 0),
            end_time=time(7, 0),
            active_days_of_week=['Wednesday', 'Thursday', 'Friday'],
            weekend_days=[],
            night_shift=True,
            default_staffing_count=1,
            active=True,
        )
        physician = self._create_assignment_physician(
            'night.max.contract@example.com',
            'Night Max Contract',
            facilities=[self.facility],
        )
        contract = Contract.objects.get(user_assignments__physician=physician)
        contract.night_settings = {
            'max_consecutive_night_shifts': '2',
            'max_consecutive_night_shifts_penalty_weight': '1200',
        }
        contract.save(update_fields=['night_settings', 'updated_at'])
        nights = [
            self._create_shift_instance(version, night_template, date(2026, 7, day))
            for day in [1, 2, 3]
        ]
        state = defaultdict(list)
        for night in nights:
            state[night.id].append(physician.id)

        report = _night_violation_report(
            nights,
            [physician],
            state,
            {physician.id: contract},
        )

        max_rows = [
            violation for violation in report['night_violations']
            if violation['violation_type'] == 'MAX_CONSECUTIVE_NIGHTS'
        ]
        self.assertEqual(len(max_rows), 1)
        self.assertEqual(max_rows[0]['configured_limit'], 2)
        self.assertEqual(max_rows[0]['actual_value'], 3)
        self.assertEqual(max_rows[0]['penalty'], 1200.0)
        self.assertEqual(len(report['max_consecutive_night_violations']), 1)

    def test_post_night_recovery_uses_each_physician_contract(self):
        version = self._create_build_version(date(2026, 7, 1), date(2026, 7, 4))
        night_template = ShiftTemplate.objects.create(
            facility=self.facility,
            start_time=time(19, 0),
            end_time=time(7, 0),
            active_days_of_week=['Wednesday'],
            weekend_days=[],
            night_shift=True,
            default_staffing_count=1,
            active=True,
        )
        day_template = ShiftTemplate.objects.create(
            facility=self.facility,
            start_time=time(7, 0),
            end_time=time(16, 0),
            active_days_of_week=['Thursday'],
            weekend_days=[],
            night_shift=False,
            default_staffing_count=1,
            active=True,
        )
        strict = self._create_assignment_physician(
            'night.strict.recovery@example.com',
            'Strict Recovery',
            facilities=[self.facility],
        )
        relaxed = self._create_assignment_physician(
            'night.relaxed.recovery@example.com',
            'Relaxed Recovery',
            facilities=[self.facility],
        )
        strict_contract = Contract.objects.get(user_assignments__physician=strict)
        strict_contract.name = 'Strict Recovery Contract'
        strict_contract.night_settings = {
            'days_off_after_night_block': '2',
            'days_off_after_night_block_penalty_weight': '1500',
        }
        strict_contract.save(update_fields=['name', 'night_settings', 'updated_at'])
        relaxed_contract = Contract.objects.get(user_assignments__physician=relaxed)
        relaxed_contract.name = 'Relaxed Recovery Contract'
        relaxed_contract.night_settings = {}
        relaxed_contract.save(update_fields=['name', 'night_settings', 'updated_at'])
        night = self._create_shift_instance(version, night_template, date(2026, 7, 1))
        day = self._create_shift_instance(version, day_template, date(2026, 7, 2))
        instances = [night, day]
        state = defaultdict(list)
        state[night.id].extend([strict.id, relaxed.id])
        state[day.id].extend([strict.id, relaxed.id])

        report = _night_violation_report(
            instances,
            [strict, relaxed],
            state,
            {
                strict.id: strict_contract,
                relaxed.id: relaxed_contract,
            },
        )

        recovery_rows = [
            violation for violation in report['night_violations']
            if violation['violation_type'] == 'INSUFFICIENT_DAYS_OFF_AFTER_NIGHT_BEFORE_NON_NIGHT'
        ]
        self.assertEqual(len(recovery_rows), 1)
        self.assertEqual(recovery_rows[0]['physician_id'], strict.id)
        self.assertEqual(recovery_rows[0]['contract_name'], 'Strict Recovery Contract')
        self.assertEqual(recovery_rows[0]['penalty'], 3000.0)
        self.assertEqual(len(report['post_night_to_non_night_recovery_violations']), 1)

    def test_next_night_block_recovery_uses_physician_contract(self):
        version = self._create_build_version(date(2026, 7, 1), date(2026, 7, 7))
        night_template = ShiftTemplate.objects.create(
            facility=self.facility,
            start_time=time(19, 0),
            end_time=time(7, 0),
            active_days_of_week=['Wednesday', 'Saturday'],
            weekend_days=[],
            night_shift=True,
            default_staffing_count=1,
            active=True,
        )
        physician = self._create_assignment_physician(
            'night.next.contract@example.com',
            'Next Night Contract',
            facilities=[self.facility],
        )
        contract = Contract.objects.get(user_assignments__physician=physician)
        contract.name = 'Next Block Recovery Contract'
        contract.night_settings = {
            'days_off_before_next_night_shift': '5',
            'days_off_before_next_night_shift_penalty_weight': '1000',
        }
        contract.save(update_fields=['name', 'night_settings', 'updated_at'])
        first_night = self._create_shift_instance(version, night_template, date(2026, 7, 1))
        second_night = self._create_shift_instance(version, night_template, date(2026, 7, 4))
        state = defaultdict(list)
        state[first_night.id].append(physician.id)
        state[second_night.id].append(physician.id)

        report = _night_violation_report(
            [first_night, second_night],
            [physician],
            state,
            {physician.id: contract},
        )

        recovery_rows = [
            violation for violation in report['night_violations']
            if violation['violation_type'] == 'INSUFFICIENT_DAYS_OFF_AFTER_NIGHT_BEFORE_NEXT_NIGHT_BLOCK'
        ]
        self.assertEqual(len(recovery_rows), 1)
        self.assertEqual(recovery_rows[0]['contract_name'], 'Next Block Recovery Contract')
        self.assertEqual(recovery_rows[0]['actual_value'], 2)
        self.assertEqual(recovery_rows[0]['penalty'], 3000.0)
        self.assertEqual(len(report['post_night_to_next_night_block_recovery_violations']), 1)

    def test_night_recovery_does_not_penalize_non_night_before_night_block(self):
        self.block.start_date = date(2026, 7, 1)
        self.block.end_date = date(2026, 7, 4)
        self.block.build_status = ScheduleBlock.BuildStatus.BUILD
        self.block.save(update_fields=['start_date', 'end_date', 'build_status', 'updated_at'])
        version = ScheduleVersion.objects.create(
            schedule_block=self.block,
            domain=self.domain,
            version_number=1,
            name='Build 1',
            status=ScheduleVersion.Status.BUILD,
        )
        day_template = ShiftTemplate.objects.create(
            facility=self.facility,
            start_time=time(7, 0),
            end_time=time(16, 0),
            active_days_of_week=['Wednesday'],
            weekend_days=[],
            night_shift=False,
            default_staffing_count=1,
            active=True,
        )
        night_template = ShiftTemplate.objects.create(
            facility=self.facility,
            start_time=time(19, 0),
            end_time=time(7, 0),
            active_days_of_week=['Thursday'],
            weekend_days=[],
            night_shift=True,
            default_staffing_count=1,
            active=True,
        )
        physician = self._create_assignment_physician(
            'night.prior.day@example.com',
            'Night Prior Day',
            facilities=[self.facility],
        )
        contract = Contract.objects.get(user_assignments__physician=physician)
        contract.night_settings = {
            'days_off_after_night_block': '2',
            'days_off_after_night_block_penalty_weight': '1000',
            'days_off_before_next_night_shift': '5',
            'days_off_before_next_night_shift_penalty_weight': '1000',
        }
        contract.save(update_fields=['night_settings', 'updated_at'])
        prior_day = ScheduleShiftInstance.objects.create(
            schedule_block=self.block,
            schedule_version=version,
            shift_template=day_template,
            facility=self.facility,
            date=date(2026, 7, 1),
            start_datetime=timezone.make_aware(datetime(2026, 7, 1, 7, 0)),
            end_datetime=timezone.make_aware(datetime(2026, 7, 1, 16, 0)),
            required_staffing=1,
        )
        night = ScheduleShiftInstance.objects.create(
            schedule_block=self.block,
            schedule_version=version,
            shift_template=night_template,
            facility=self.facility,
            date=date(2026, 7, 2),
            start_datetime=timezone.make_aware(datetime(2026, 7, 2, 19, 0)),
            end_datetime=timezone.make_aware(datetime(2026, 7, 3, 7, 0)),
            required_staffing=1,
        )
        state = defaultdict(list)
        state[prior_day.id].append(physician.id)
        state[night.id].append(physician.id)

        report = _night_violation_report(
            [prior_day, night],
            [physician],
            state,
            {physician.id: contract},
        )

        self.assertEqual(report['night_violations'], [])
        self.assertEqual(float(report['score']), 0.0)

    def test_night_block_to_next_night_block_recovery_scores_separate_blocks(self):
        self.block.start_date = date(2026, 7, 1)
        self.block.end_date = date(2026, 7, 10)
        self.block.build_status = ScheduleBlock.BuildStatus.BUILD
        self.block.save(update_fields=['start_date', 'end_date', 'build_status', 'updated_at'])
        version = ScheduleVersion.objects.create(
            schedule_block=self.block,
            domain=self.domain,
            version_number=1,
            name='Build 1',
            status=ScheduleVersion.Status.BUILD,
        )
        night_template = ShiftTemplate.objects.create(
            facility=self.facility,
            start_time=time(19, 0),
            end_time=time(7, 0),
            active_days_of_week=['Wednesday', 'Saturday'],
            weekend_days=[],
            night_shift=True,
            default_staffing_count=1,
            active=True,
        )
        physician = self._create_assignment_physician(
            'night.next.block@example.com',
            'Night Next Block',
            facilities=[self.facility],
        )
        contract = Contract.objects.get(user_assignments__physician=physician)
        contract.night_settings = {
            'max_consecutive_night_shifts': '4',
            'days_off_before_next_night_shift': '5',
            'days_off_before_next_night_shift_penalty_weight': '1000',
        }
        contract.save(update_fields=['night_settings', 'updated_at'])
        first_night = ScheduleShiftInstance.objects.create(
            schedule_block=self.block,
            schedule_version=version,
            shift_template=night_template,
            facility=self.facility,
            date=date(2026, 7, 1),
            start_datetime=timezone.make_aware(datetime(2026, 7, 1, 19, 0)),
            end_datetime=timezone.make_aware(datetime(2026, 7, 2, 7, 0)),
            required_staffing=1,
        )
        second_block_night = ScheduleShiftInstance.objects.create(
            schedule_block=self.block,
            schedule_version=version,
            shift_template=night_template,
            facility=self.facility,
            date=date(2026, 7, 4),
            start_datetime=timezone.make_aware(datetime(2026, 7, 4, 19, 0)),
            end_datetime=timezone.make_aware(datetime(2026, 7, 5, 7, 0)),
            required_staffing=1,
        )
        state = defaultdict(list)
        state[first_night.id].append(physician.id)
        state[second_block_night.id].append(physician.id)

        report = _night_violation_report(
            [first_night, second_block_night],
            [physician],
            state,
            {physician.id: contract},
        )

        violations = [
            violation for violation in report['night_violations']
            if violation['violation_type'] == 'INSUFFICIENT_DAYS_OFF_AFTER_NIGHT_BEFORE_NEXT_NIGHT_BLOCK'
        ]
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0]['prior_night_block_dates'], ['2026-07-01'])
        self.assertEqual(violations[0]['next_night_block_dates'], ['2026-07-04'])
        self.assertEqual(violations[0]['actual_value'], 2)
        self.assertEqual(violations[0]['penalty'], 3000.0)

    def test_consecutive_nights_do_not_trigger_next_night_block_recovery(self):
        self.block.start_date = date(2026, 7, 1)
        self.block.end_date = date(2026, 7, 3)
        self.block.build_status = ScheduleBlock.BuildStatus.BUILD
        self.block.save(update_fields=['start_date', 'end_date', 'build_status', 'updated_at'])
        version = ScheduleVersion.objects.create(
            schedule_block=self.block,
            domain=self.domain,
            version_number=1,
            name='Build 1',
            status=ScheduleVersion.Status.BUILD,
        )
        night_template = ShiftTemplate.objects.create(
            facility=self.facility,
            start_time=time(19, 0),
            end_time=time(7, 0),
            active_days_of_week=['Wednesday', 'Thursday'],
            weekend_days=[],
            night_shift=True,
            default_staffing_count=1,
            active=True,
        )
        physician = self._create_assignment_physician(
            'night.consecutive.valid@example.com',
            'Night Consecutive',
            facilities=[self.facility],
        )
        contract = Contract.objects.get(user_assignments__physician=physician)
        contract.night_settings = {
            'max_consecutive_night_shifts': '4',
            'days_off_before_next_night_shift': '5',
            'days_off_before_next_night_shift_penalty_weight': '1000',
        }
        contract.save(update_fields=['night_settings', 'updated_at'])
        first_night = ScheduleShiftInstance.objects.create(
            schedule_block=self.block,
            schedule_version=version,
            shift_template=night_template,
            facility=self.facility,
            date=date(2026, 7, 1),
            start_datetime=timezone.make_aware(datetime(2026, 7, 1, 19, 0)),
            end_datetime=timezone.make_aware(datetime(2026, 7, 2, 7, 0)),
            required_staffing=1,
        )
        second_night = ScheduleShiftInstance.objects.create(
            schedule_block=self.block,
            schedule_version=version,
            shift_template=night_template,
            facility=self.facility,
            date=date(2026, 7, 2),
            start_datetime=timezone.make_aware(datetime(2026, 7, 2, 19, 0)),
            end_datetime=timezone.make_aware(datetime(2026, 7, 3, 7, 0)),
            required_staffing=1,
        )
        state = defaultdict(list)
        state[first_night.id].append(physician.id)
        state[second_night.id].append(physician.id)

        report = _night_violation_report(
            [first_night, second_night],
            [physician],
            state,
            {physician.id: contract},
        )

        self.assertEqual(report['night_violations'], [])
        self.assertEqual(float(report['score']), 0.0)

    def test_night_under_minimum_duplicate_rules_emit_one_violation(self):
        self.block.start_date = date(2026, 7, 1)
        self.block.end_date = date(2026, 7, 3)
        self.block.build_status = ScheduleBlock.BuildStatus.BUILD
        self.block.save(update_fields=['start_date', 'end_date', 'build_status', 'updated_at'])
        version = ScheduleVersion.objects.create(
            schedule_block=self.block,
            domain=self.domain,
            version_number=1,
            name='Build 1',
            status=ScheduleVersion.Status.BUILD,
        )
        night_template = ShiftTemplate.objects.create(
            facility=self.facility,
            start_time=time(19, 0),
            end_time=time(7, 0),
            active_days_of_week=['Wednesday'],
            weekend_days=[],
            night_shift=True,
            default_staffing_count=1,
            active=True,
        )
        ScheduleShiftInstance.objects.create(
            schedule_block=self.block,
            schedule_version=version,
            shift_template=night_template,
            facility=self.facility,
            date=date(2026, 7, 1),
            start_datetime=timezone.make_aware(datetime(2026, 7, 1, 19, 0)),
            end_datetime=timezone.make_aware(datetime(2026, 7, 2, 7, 0)),
            required_staffing=1,
        )
        physician = self._create_assignment_physician(
            'night.under.minimum@example.com',
            'Night Under',
            facilities=[self.facility],
        )
        contract = Contract.objects.get(user_assignments__physician=physician)
        duplicate_rule = {
            'period_type': 'SCHEDULE_BLOCK',
            'min_shifts': '1',
            'min_penalty_weight': '2500',
        }
        contract.night_settings = {'period_rules': [duplicate_rule, duplicate_rule.copy()]}
        contract.save(update_fields=['night_settings', 'updated_at'])
        state = defaultdict(list)

        report = _night_violation_report(
            list(ScheduleShiftInstance.objects.filter(schedule_version=version)),
            [physician],
            state,
            {physician.id: contract},
        )

        under_violations = [
            violation for violation in report['night_violations']
            if violation['violation_type'] == 'NIGHT_UNDER_MINIMUM'
        ]
        self.assertEqual(len(under_violations), 1)
        self.assertEqual(under_violations[0]['period_type'], 'SCHEDULE_BLOCK')
        self.assertEqual(under_violations[0]['configured_limit'], 1)
        self.assertEqual(under_violations[0]['actual_value'], 0)
        self.assertEqual(under_violations[0]['penalty'], 2500.0)

    def test_equivalent_month_and_schedule_block_night_minimum_penalizes_once(self):
        self.block.start_date = date(2026, 11, 1)
        self.block.end_date = date(2026, 11, 14)
        self.block.build_status = ScheduleBlock.BuildStatus.BUILD
        self.block.save(update_fields=['start_date', 'end_date', 'build_status', 'updated_at'])
        version = ScheduleVersion.objects.create(
            schedule_block=self.block,
            domain=self.domain,
            version_number=1,
            name='Build 1',
            status=ScheduleVersion.Status.BUILD,
        )
        night_template = ShiftTemplate.objects.create(
            facility=self.facility,
            start_time=time(19, 0),
            end_time=time(7, 0),
            active_days_of_week=['Sunday'],
            weekend_days=[],
            night_shift=True,
            default_staffing_count=1,
            active=True,
        )
        day_template = ShiftTemplate.objects.create(
            facility=self.facility,
            start_time=time(7, 0),
            end_time=time(16, 0),
            active_days_of_week=['Saturday'],
            weekend_days=[],
            night_shift=False,
            default_staffing_count=1,
            active=True,
        )
        night_instance = ScheduleShiftInstance.objects.create(
            schedule_block=self.block,
            schedule_version=version,
            shift_template=night_template,
            facility=self.facility,
            date=date(2026, 11, 1),
            start_datetime=timezone.make_aware(datetime(2026, 11, 1, 19, 0)),
            end_datetime=timezone.make_aware(datetime(2026, 11, 2, 7, 0)),
            required_staffing=1,
        )
        range_end_instance = ScheduleShiftInstance.objects.create(
            schedule_block=self.block,
            schedule_version=version,
            shift_template=day_template,
            facility=self.facility,
            date=date(2026, 11, 14),
            start_datetime=timezone.make_aware(datetime(2026, 11, 14, 7, 0)),
            end_datetime=timezone.make_aware(datetime(2026, 11, 14, 16, 0)),
            required_staffing=1,
        )
        physician = self._create_assignment_physician(
            'night.duplicate.month@example.com',
            'Night Duplicate Month',
            facilities=[self.facility],
        )
        contract = Contract.objects.get(user_assignments__physician=physician)
        contract.night_settings = {
            'period_rules': [
                {
                    'period_type': 'SCHEDULE_BLOCK',
                    'min_shifts': '1',
                    'min_penalty_weight': '10000',
                },
                {
                    'period_type': 'MONTH',
                    'min_shifts': '1',
                    'min_penalty_weight': '10000',
                },
            ],
        }
        contract.save(update_fields=['night_settings', 'updated_at'])
        state = defaultdict(list)

        report = _night_violation_report(
            [night_instance, range_end_instance],
            [physician],
            state,
            {physician.id: contract},
        )

        under_violations = [
            violation for violation in report['night_violations']
            if violation['violation_type'] == 'NIGHT_UNDER_MINIMUM'
        ]
        self.assertEqual(len(under_violations), 1)
        self.assertEqual(under_violations[0]['period_type'], 'SCHEDULE_BLOCK')
        self.assertEqual(under_violations[0]['penalty'], 10000.0)
        self.assertEqual(float(report['score']), 10000.0)
        self.assertEqual(report['night_minimum_rules_applied'][0]['period_type'], 'SCHEDULE_BLOCK')
        self.assertEqual(len(report['night_minimum_rules_suppressed_as_duplicates']), 1)
        self.assertEqual(
            report['night_minimum_rules_suppressed_as_duplicates'][0]['suppressed_period_type'],
            'MONTH',
        )
        self.assertEqual(
            report['night_minimum_rules_suppressed_as_duplicates'][0]['kept_period_type'],
            'SCHEDULE_BLOCK',
        )

    def test_distinct_month_and_schedule_block_night_minimum_rules_both_apply(self):
        self.block.start_date = date(2026, 11, 1)
        self.block.end_date = date(2027, 1, 31)
        self.block.build_status = ScheduleBlock.BuildStatus.BUILD
        self.block.save(update_fields=['start_date', 'end_date', 'build_status', 'updated_at'])
        version = ScheduleVersion.objects.create(
            schedule_block=self.block,
            domain=self.domain,
            version_number=1,
            name='Build 1',
            status=ScheduleVersion.Status.BUILD,
        )
        night_template = ShiftTemplate.objects.create(
            facility=self.facility,
            start_time=time(19, 0),
            end_time=time(7, 0),
            active_days_of_week=['Sunday'],
            weekend_days=[],
            night_shift=True,
            default_staffing_count=1,
            active=True,
        )
        instances = [
            ScheduleShiftInstance.objects.create(
                schedule_block=self.block,
                schedule_version=version,
                shift_template=night_template,
                facility=self.facility,
                date=instance_date,
                start_datetime=timezone.make_aware(datetime(instance_date.year, instance_date.month, instance_date.day, 19, 0)),
                end_datetime=timezone.make_aware(datetime(instance_date.year, instance_date.month, instance_date.day, 7, 0)) + timedelta(days=1),
                required_staffing=1,
            )
            for instance_date in [date(2026, 11, 1), date(2026, 12, 1), date(2027, 1, 31)]
        ]
        physician = self._create_assignment_physician(
            'night.distinct.periods@example.com',
            'Night Distinct Periods',
            facilities=[self.facility],
        )
        contract = Contract.objects.get(user_assignments__physician=physician)
        contract.night_settings = {
            'period_rules': [
                {
                    'period_type': 'SCHEDULE_BLOCK',
                    'min_shifts': '4',
                    'min_penalty_weight': '10000',
                },
                {
                    'period_type': 'MONTH',
                    'min_shifts': '1',
                    'min_penalty_weight': '10000',
                },
            ],
        }
        contract.save(update_fields=['night_settings', 'updated_at'])
        state = defaultdict(list)

        report = _night_violation_report(
            instances,
            [physician],
            state,
            {physician.id: contract},
        )

        under_violations = [
            violation for violation in report['night_violations']
            if violation['violation_type'] == 'NIGHT_UNDER_MINIMUM'
        ]
        self.assertEqual(len(under_violations), 4)
        self.assertEqual(
            [violation['period_type'] for violation in under_violations].count('SCHEDULE_BLOCK'),
            1,
        )
        self.assertEqual(
            [violation['period_type'] for violation in under_violations].count('MONTH'),
            3,
        )
        self.assertEqual(float(report['score']), 70000.0)
        self.assertEqual(report['night_minimum_rules_suppressed_as_duplicates'], [])

    def test_stale_night_violation_rows_are_dropped(self):
        self.block.start_date = date(2026, 7, 1)
        self.block.end_date = date(2026, 7, 2)
        self.block.build_status = ScheduleBlock.BuildStatus.BUILD
        self.block.save(update_fields=['start_date', 'end_date', 'build_status', 'updated_at'])
        version = ScheduleVersion.objects.create(
            schedule_block=self.block,
            domain=self.domain,
            version_number=1,
            name='Build 1',
            status=ScheduleVersion.Status.BUILD,
        )
        night_template = ShiftTemplate.objects.create(
            facility=self.facility,
            start_time=time(19, 0),
            end_time=time(7, 0),
            active_days_of_week=['Wednesday'],
            weekend_days=[],
            night_shift=True,
            default_staffing_count=1,
            active=True,
        )
        night_instance = ScheduleShiftInstance.objects.create(
            schedule_block=self.block,
            schedule_version=version,
            shift_template=night_template,
            facility=self.facility,
            date=date(2026, 7, 1),
            start_datetime=timezone.make_aware(datetime(2026, 7, 1, 19, 0)),
            end_datetime=timezone.make_aware(datetime(2026, 7, 2, 7, 0)),
            required_staffing=1,
        )
        assigned = self._create_assignment_physician(
            'assigned.night.validation@example.com',
            'Assigned Night',
            facilities=[self.facility],
        )
        stale = self._create_assignment_physician(
            'stale.night.validation@example.com',
            'Stale Night',
            facilities=[self.facility],
        )
        assignment = ScheduleShiftAssignment.objects.create(
            shift_instance=night_instance,
            physician=assigned,
            created_by=self.scheduler_user,
            assignment_source=ScheduleShiftAssignment.AssignmentSource.OPTIMIZER,
        )
        stale_report = {
            'score': Decimal('5000'),
            'total_night_shifts': 1,
            'night_shifts_by_physician': [],
            'night_violations_count': 1,
            'night_violations': [
                {
                    'physician_id': stale.id,
                    'physician': 'Stale Night',
                    'violation_type': 'MAX_CONSECUTIVE_NIGHTS',
                    'dates_involved': ['2026-07-01'],
                    'night_block_dates': ['2026-07-01'],
                    'night_block_assignments': [
                        {
                            'shift_instance_id': night_instance.id,
                            'date': '2026-07-01',
                            'facility': self.facility.short_name,
                            'shift_template': night_template.name,
                            'night_shift': True,
                        }
                    ],
                    'shift_instance_ids': [night_instance.id],
                    'configured_limit': 0,
                    'actual_value': 1,
                    'penalty_weight': 5000,
                    'penalty': 5000,
                }
            ],
            'night_unresolved_reasons': [],
            'max_nights_assigned_to_one_physician': 1,
        }

        filtered = _validated_night_report_for_current_assignments(
            stale_report,
            version,
            [assignment],
        )

        self.assertEqual(filtered['night_violations'], [])
        self.assertEqual(filtered['night_violations_count'], 0)
        self.assertEqual(filtered['stale_violation_rows_dropped'], 1)
        self.assertIn('not assigned to this physician', filtered['violation_assignment_validation_errors'][0]['message'])

    def test_isolated_night_search_heuristic_is_not_hidden_final_score(self):
        self.block.start_date = date(2026, 7, 1)
        self.block.end_date = date(2026, 7, 2)
        self.block.build_status = ScheduleBlock.BuildStatus.BUILD
        self.block.save(update_fields=['start_date', 'end_date', 'build_status', 'updated_at'])
        version = ScheduleVersion.objects.create(
            schedule_block=self.block,
            domain=self.domain,
            version_number=1,
            name='Build 1',
            status=ScheduleVersion.Status.BUILD,
        )
        night_template = ShiftTemplate.objects.create(
            facility=self.facility,
            start_time=time(19, 0),
            end_time=time(7, 0),
            active_days_of_week=['Wednesday', 'Thursday'],
            weekend_days=[],
            night_shift=True,
            default_staffing_count=1,
            active=True,
        )
        first_night = ScheduleShiftInstance.objects.create(
            schedule_block=self.block,
            schedule_version=version,
            shift_template=night_template,
            facility=self.facility,
            date=date(2026, 7, 1),
            start_datetime=timezone.make_aware(datetime(2026, 7, 1, 19, 0)),
            end_datetime=timezone.make_aware(datetime(2026, 7, 2, 7, 0)),
            required_staffing=1,
        )
        second_night = ScheduleShiftInstance.objects.create(
            schedule_block=self.block,
            schedule_version=version,
            shift_template=night_template,
            facility=self.facility,
            date=date(2026, 7, 2),
            start_datetime=timezone.make_aware(datetime(2026, 7, 2, 19, 0)),
            end_datetime=timezone.make_aware(datetime(2026, 7, 3, 7, 0)),
            required_staffing=1,
        )
        first = self._create_assignment_physician(
            'isolated.first@example.com',
            'Isolated First',
            facilities=[self.facility],
        )
        second = self._create_assignment_physician(
            'isolated.second@example.com',
            'Isolated Second',
            facilities=[self.facility],
        )
        instances = [first_night, second_night]
        state = defaultdict(list)
        state[first_night.id].append(first.id)
        state[second_night.id].append(second.id)
        contract_by_physician = {
            assignment.physician_id: assignment.contract
            for assignment in ContractUserAssignment.objects.filter(
                physician__in=[first, second],
            ).select_related('contract')
        }

        report = _night_violation_report(
            instances,
            [first, second],
            state,
            contract_by_physician,
        )

        self.assertEqual(float(report['score']), 0.0)
        self.assertEqual(report['night_violations'], [])

    def test_optimizer_score_includes_simple_distribution_penalties(self):
        second_facility = Facility.objects.create(
            name='Albany Hospital',
            short_name='Albany',
            timezone='UTC',
        )
        clustered_template = ShiftTemplate.objects.create(
            facility=self.facility,
            start_time=time(7, 0),
            end_time=time(16, 0),
            active_days_of_week=['Friday', 'Saturday', 'Sunday', 'Monday', 'Tuesday', 'Wednesday'],
            weekend_days=['Friday', 'Saturday', 'Sunday'],
            night_shift=True,
            default_staffing_count=1,
            active=True,
        )
        self.block.start_date = date(2026, 7, 3)
        self.block.end_date = date(2026, 7, 8)
        self.block.save(update_fields=['start_date', 'end_date', 'updated_at'])
        self.client.post(
            f'/api/schedule-blocks/{self.block.id}/build/generate/',
            data={'domain_id': self.domain.id},
            format='json',
        )
        version = ScheduleVersion.objects.get(schedule_block=self.block)
        clustered = self._create_assignment_physician(
            'clustered.distribution@example.com',
            'Clustered Distribution',
            facilities=[self.facility, second_facility],
        )
        alternate = self._create_assignment_physician(
            'alternate.distribution@example.com',
            'Alternate Distribution',
            facilities=[self.facility, second_facility],
        )
        instances = list(
            ScheduleShiftInstance.objects.filter(
                schedule_version=version,
                shift_template=clustered_template,
            ).order_by('date')
        )
        state = defaultdict(list)
        for instance in instances:
            state[instance.id].append(clustered.id)
        physicians = [clustered, alternate]
        contract_by_physician = {
            assignment.physician_id: assignment.contract
            for assignment in ContractUserAssignment.objects.filter(
                physician__in=physicians,
            ).select_related('contract')
        }
        targets = {
            physician.id: {'units': 'HOURS', 'target': Decimal('27')}
            for physician in physicians
        }
        eligible_facilities_by_physician = {
            physician.id: {self.facility.id, second_facility.id}
            for physician in physicians
        }
        minimum_rest_by_physician = {
            physician.id: Decimal('0')
            for physician in physicians
        }

        scoring = _score_schedule(
            instances,
            physicians,
            state,
            targets,
            contract_by_physician,
            defaultdict(list),
            eligible_facilities_by_physician,
            minimum_rest_by_physician,
        )

        breakdown = scoring['breakdown']
        self.assertEqual(float(breakdown['consecutive_days_score']), 500.0)
        self.assertEqual(float(breakdown['same_shift_score']), 8000.0)
        self.assertEqual(float(breakdown['night_score']), 1400.0)
        self.assertGreater(float(breakdown['weekend_score']), 0)
        self.assertGreater(float(breakdown['facility_distribution_score']), 0)
        self.assertEqual(
            breakdown['total_score'],
            sum(
                value
                for key, value in breakdown.items()
                if key != 'total_score'
            ),
        )

    def test_schedule_version_violation_report_lists_all_users(self):
        self.client.post(
            f'/api/schedule-blocks/{self.block.id}/build/generate/',
            data={'domain_id': self.domain.id},
            format='json',
        )
        version = ScheduleVersion.objects.get(schedule_block=self.block)
        for index in range(3):
            self._create_assignment_physician(
                f'report{index}@example.com',
                f'Report User {index}',
                facilities=[self.facility],
            )
        self.client.post(
            f'/api/schedule-blocks/{self.block.id}/build/versions/{version.id}/optimize/',
            format='json',
        )

        response = self.client.get(f'/api/schedule-versions/{version.id}/violation-report/')

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['schedule_version']['id'], version.id)
        self.assertEqual(payload['schedule_block']['id'], self.block.id)
        self.assertIn('total_score', payload)
        self.assertIn('score_breakdown', payload)
        self.assertTrue(payload['debug']['violations_recomputed_from_final_assignments'])
        self.assertIn('stale_violation_rows_dropped', payload['debug'])
        self.assertIn('violation_assignment_validation_errors', payload['debug'])
        self.assertIn('night_block_assignment_ids_by_physician', payload['debug'])
        self.assertIn('workload_score_rows', payload['debug'])
        self.assertAlmostEqual(
            sum(row['score_contribution'] for row in payload['debug']['workload_score_rows']),
            payload['score_breakdown']['workload_score'],
        )
        self.assertEqual(payload['score_audit']['warnings'], [])
        self.assertEqual(
            [user['display_name'] for user in payload['users']],
            sorted(user['display_name'] for user in payload['users']),
        )
        self.assertEqual(len(payload['users']), 3)
        for user in payload['users']:
            self.assertIn('night_shifts', user)
            self.assertIn('violations', user)
            self.assertIn('workload_score', user)
            self.assertAlmostEqual(
                user['total_score'],
                (
                    sum(violation['penalty_amount'] for violation in user['violations'])
                    + user['workload_score']['score_contribution']
                ),
            )
            penalties = [violation['penalty_amount'] for violation in user['violations']]
            self.assertEqual(penalties, sorted(penalties, reverse=True))

    def test_violation_report_lists_request_penalties_and_audits_scores(self):
        self.client.post(
            f'/api/schedule-blocks/{self.block.id}/build/generate/',
            data={'domain_id': self.domain.id},
            format='json',
        )
        version = ScheduleVersion.objects.get(schedule_block=self.block)
        shift_instance = ScheduleShiftInstance.objects.get(shift_template=self.day_template)
        physician = self._create_assignment_physician(
            'request.report@example.com',
            'Request Report',
            facilities=[self.facility],
        )
        ScheduleRequest.objects.create(
            schedule_block=self.block,
            physician=physician,
            date=shift_instance.date,
            request_scope=ScheduleRequest.RequestScope.USER,
            request_type=ScheduleRequest.RequestType.DAY_OFF,
            weight=ScheduleRequest.Weight.FIXED,
            created_by=self.scheduler_user,
        )
        ScheduleShiftAssignment.objects.create(
            shift_instance=shift_instance,
            physician=physician,
            created_by=self.scheduler_user,
            assignment_source=ScheduleShiftAssignment.AssignmentSource.MANUAL,
        )

        response = self.client.get(f'/api/schedule-versions/{version.id}/violation-report/')

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        request_rows = [
            violation
            for user in payload['users']
            for violation in user['violations']
            if violation['violation_type'].startswith('REQUEST_')
        ]
        self.assertEqual(len(request_rows), 1)
        self.assertEqual(request_rows[0]['violation_type'], 'REQUEST_DAY_OFF_VIOLATION')
        self.assertEqual(request_rows[0]['penalty_amount'], payload['score_breakdown']['request_score'])
        self.assertEqual(payload['score_audit']['warnings'], [])

    def test_optimizer_and_context_ignore_out_of_range_shift_instances(self):
        self.client.post(
            f'/api/schedule-blocks/{self.block.id}/build/generate/',
            data={'domain_id': self.domain.id},
            format='json',
        )
        version = ScheduleVersion.objects.get(schedule_block=self.block)
        out_of_range_date = self.block.end_date + timedelta(days=30)
        out_of_range_instance = ScheduleShiftInstance.objects.create(
            schedule_version=version,
            schedule_block=self.block,
            date=out_of_range_date,
            shift_template=self.day_template,
            facility=self.facility,
            start_datetime=timezone.make_aware(datetime.combine(out_of_range_date, time(7, 0))),
            end_datetime=timezone.make_aware(datetime.combine(out_of_range_date, time(16, 0))),
            required_staffing=1,
            status=ScheduleShiftInstance.Status.ASSIGNED,
        )
        stale_physician = self._create_assignment_physician(
            'stale.outofrange@example.com',
            'Stale Outrange',
            facilities=[self.facility],
        )
        ScheduleShiftAssignment.objects.create(
            shift_instance=out_of_range_instance,
            physician=stale_physician,
            created_by=self.scheduler_user,
            assignment_source=ScheduleShiftAssignment.AssignmentSource.OPTIMIZER,
        )
        for index in range(4):
            self._create_assignment_physician(
                f'scoped.optimizer{index}@example.com',
                f'Scoped Optimizer {index}',
                facilities=[self.facility],
            )

        context_response = self.client.get(
            f'/api/schedule-blocks/{self.block.id}/build/?version_id={version.id}'
        )
        optimize_response = self.client.post(
            f'/api/schedule-blocks/{self.block.id}/build/versions/{version.id}/optimize/',
            format='json',
        )

        self.assertEqual(context_response.status_code, 200)
        context_payload = context_response.json()
        self.assertEqual(context_payload['selected_version']['shift_instance_count'], 2)
        self.assertNotIn(
            out_of_range_instance.id,
            [instance['id'] for instance in context_payload['shift_instances']],
        )
        self.assertEqual(optimize_response.status_code, 200)
        payload = optimize_response.json()
        self.assertEqual(payload['debug']['shift_instances_considered'], 2)
        self.assertEqual(payload['debug']['assignment_rows_before'], 0)
        self.assertEqual(payload['debug']['optimizer_assignments_deleted'], 0)
        self.assertEqual(payload['assignments_made'], 3)
        self.assertEqual(payload['unfilled_shift_count'], 0)
        self.assertTrue(out_of_range_instance.assignments.exists())
        self.assertFalse(
            out_of_range_instance.assignments.filter(
                optimizer_run_id=payload['optimizer_run_id'],
            ).exists()
        )
        self.assertEqual(
            sum(item['assigned_shifts'] for item in payload['workload_summary']),
            3,
        )

    def test_optimizer_respects_active_domain_and_facility_eligibility(self):
        self.client.post(
            f'/api/schedule-blocks/{self.block.id}/build/generate/',
            data={'domain_id': self.domain.id},
            format='json',
        )
        version = ScheduleVersion.objects.get(schedule_block=self.block)
        eligible = self._create_assignment_physician(
            'eligible.optimizer@example.com',
            'Eligible Optimizer',
            facilities=[self.facility],
        )
        self._create_assignment_physician(
            'wrong.facility.optimizer@example.com',
            'Wrong Facility',
        )
        self._create_assignment_physician(
            'inactive.optimizer@example.com',
            'Inactive Optimizer',
            facilities=[self.facility],
            active=False,
        )
        other_domain = Domain.objects.create(name='Other Optimizer Domain', active=True)
        other_user = get_user_model().objects.create_user(
            username='other.optimizer@example.com',
            email='other.optimizer@example.com',
            first_name='Other',
            last_name='Domain',
        )
        other_physician = Physician.objects.create(user=other_user, active=True)
        other_contract = Contract.objects.create(
            domain=other_domain,
            name='Other Optimizer Contract',
            active=True,
        )
        other_contract.facilities.set([self.facility])
        ContractUserAssignment.objects.create(
            contract=other_contract,
            domain=other_domain,
            physician=other_physician,
        )

        response = self.client.post(
            f'/api/schedule-blocks/{self.block.id}/build/versions/{version.id}/optimize/',
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        assigned_physician_ids = set(
            ScheduleShiftAssignment.objects.filter(shift_instance__schedule_version=version)
            .values_list('physician_id', flat=True)
        )
        self.assertEqual(assigned_physician_ids, {eligible.id})
        self.assertGreater(response.json()['unfilled_shift_count'], 0)

    def test_optimizer_enforces_default_minimum_rest_for_back_to_back_shifts(self):
        day_12_template = ShiftTemplate.objects.create(
            facility=self.facility,
            start_time=time(7, 0),
            end_time=time(19, 0),
            active_days_of_week=['Tuesday'],
            weekend_days=[],
            night_shift=False,
            default_staffing_count=1,
            active=True,
        )
        night_12_template = ShiftTemplate.objects.create(
            facility=self.facility,
            start_time=time(19, 0),
            end_time=time(7, 0),
            active_days_of_week=['Tuesday'],
            weekend_days=[],
            night_shift=True,
            default_staffing_count=1,
            active=True,
        )
        self.client.post(
            f'/api/schedule-blocks/{self.block.id}/build/generate/',
            data={'domain_id': self.domain.id},
            format='json',
        )
        version = ScheduleVersion.objects.get(schedule_block=self.block)
        first = self._create_assignment_physician(
            'rest.first@example.com',
            'Rest First',
            facilities=[self.facility],
        )
        second = self._create_assignment_physician(
            'rest.second@example.com',
            'Rest Second',
            facilities=[self.facility],
        )
        third = self._create_assignment_physician(
            'rest.third@example.com',
            'Rest Third',
            facilities=[self.facility],
        )
        fourth = self._create_assignment_physician(
            'rest.fourth@example.com',
            'Rest Fourth',
            facilities=[self.facility],
        )

        response = self.client.post(
            f'/api/schedule-blocks/{self.block.id}/build/versions/{version.id}/optimize/',
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertGreater(response.json()['rest_violations_blocked'], 0)
        day_12 = ScheduleShiftInstance.objects.get(shift_template=day_12_template)
        night_12 = ScheduleShiftInstance.objects.get(shift_template=night_12_template)
        day_physician_id = day_12.assignments.get().physician_id
        night_physician_id = night_12.assignments.get().physician_id
        self.assertNotEqual(day_physician_id, night_physician_id)
        self.assertTrue(
            {day_physician_id, night_physician_id}.issubset(
                {first.id, second.id, third.id, fourth.id}
            )
        )

    def test_optimizer_enforces_contract_minimum_rest_after_overnight(self):
        self.day_template.active = False
        self.day_template.save(update_fields=['active'])
        afternoon_template = ShiftTemplate.objects.create(
            facility=self.facility,
            start_time=time(12, 0),
            end_time=time(20, 0),
            active_days_of_week=['Tuesday'],
            weekend_days=[],
            night_shift=False,
            default_staffing_count=1,
            active=True,
        )
        self.client.post(
            f'/api/schedule-blocks/{self.block.id}/build/generate/',
            data={'domain_id': self.domain.id},
            format='json',
        )
        version = ScheduleVersion.objects.get(schedule_block=self.block)
        physician = self._create_assignment_physician(
            'rest.contract@example.com',
            'Rest Contract',
            facilities=[self.facility],
        )
        contract = Contract.objects.get(user_assignments__physician=physician)
        contract.workload_settings = {'min_time_off_hours': '8'}
        contract.save(update_fields=['workload_settings', 'updated_at'])

        response = self.client.post(
            f'/api/schedule-blocks/{self.block.id}/build/versions/{version.id}/optimize/',
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        overnight_instance = ScheduleShiftInstance.objects.get(
            shift_template=self.overnight_template
        )
        afternoon_instance = ScheduleShiftInstance.objects.get(
            shift_template=afternoon_template
        )
        self.assertTrue(overnight_instance.assignments.exists())
        self.assertFalse(afternoon_instance.assignments.exists())
        self.assertGreater(response.json()['rest_violations_blocked'], 0)

    def test_optimizer_rejects_non_build_version(self):
        self.client.post(
            f'/api/schedule-blocks/{self.block.id}/build/generate/',
            data={'domain_id': self.domain.id},
            format='json',
        )
        version = ScheduleVersion.objects.get(schedule_block=self.block)
        version.status = ScheduleVersion.Status.PREVIEW
        version.save(update_fields=['status', 'updated_at'])

        response = self.client.post(
            f'/api/schedule-blocks/{self.block.id}/build/versions/{version.id}/optimize/',
            format='json',
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('BUILD Schedule Version', response.json()['detail'])


class ContractApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = get_user_model().objects.create_user(username='contract-admin', password='password123')
        self.client.force_authenticate(user=self.user)

        self.domain = Domain.objects.create(name='Emergency Medicine', active=True)
        self.facility = Facility.objects.create(name='North Hospital', short_name='North')

        physician_user = get_user_model().objects.create_user(
            username='physician.contract@example.com',
            email='physician.contract@example.com',
            first_name='Casey',
            last_name='Ng',
            password='password123',
        )
        self.physician = Physician.objects.create(user=physician_user, display_name='Casey Ng')

    def _build_payload(self):
        return {
            'domain': self.domain.id,
            'name': 'Full Time 120 Hours',
            'active': True,
            'facility_ids': [self.facility.id],
            'assigned_user_ids': [self.physician.id],
            'workload_settings': {
                'period_rules': [
                    {
                        'period_type': 'SCHEDULE_BLOCK',
                        'units': 'HOURS',
                        'min_value': '415',
                        'max_value': '425',
                        'penalty_weight': '100',
                        'hard_soft': 'SOFT',
                        'spread_violations': False,
                    },
                    {
                        'period_type': 'MONTH',
                        'units': 'HOURS',
                        'min_value': '130',
                        'max_value': '150',
                        'penalty_weight': '50',
                        'hard_soft': 'SOFT',
                        'spread_violations': True,
                    },
                    {
                        'period_type': 'WEEK',
                        'units': 'HOURS',
                        'min_value': '20',
                        'max_value': '40',
                        'penalty_weight': '25',
                        'hard_soft': 'SOFT',
                        'spread_violations': False,
                    },
                ],
                'min_time_off_hours': '12',
                'min_time_off_penalty_weight': '30',
                'min_time_off_hard_soft': 'SOFT',
                'circadian_enabled': True,
                'circadian_penalty_weight': '40',
                'min_days_in_row': '2',
                'max_days_in_row': '6',
                'min_same_shifts_in_row': '1',
                'max_same_shifts_in_row': '4',
            },
            'shift_settings': {
                'rules': [],
            },
            'night_settings': {
                'period_rules': [],
            },
            'weekend_settings': {
                'period_rules': [],
            },
            'request_settings': {
                'allow_day_off': True,
                'allow_shift_off': True,
                'allow_day_on': True,
                'allow_shift_on': True,
                'high_request_limit': '5',
                'medium_request_limit': '7',
                'low_request_limit': '',
                'low_request_unlimited': True,
                'weekend_request_limit': '2',
                'weight_low': '10',
                'weight_medium': '20',
                'weight_high': '30',
                'weight_fixed': '1000',
            },
        }

    def test_create_contract_persists_multiple_period_rules(self):
        response = self.client.post('/api/contracts/', data=self._build_payload(), format='json')

        self.assertEqual(response.status_code, 201)
        payload = response.json()
        self.assertEqual(payload['name'], 'Full Time 120 Hours')
        self.assertEqual(payload['domain'], self.domain.id)
        self.assertEqual(len(payload['workload_settings']['period_rules']), 3)
        self.assertEqual(payload['assigned_users_count'], 1)

    def test_edit_contract_updates_name_and_assignments(self):
        create_response = self.client.post('/api/contracts/', data=self._build_payload(), format='json')
        contract_id = create_response.json()['id']

        patch_response = self.client.patch(
            f'/api/contracts/{contract_id}/',
            data={
                'name': 'Nocturnist 12 Shifts',
                'assigned_user_ids': [],
            },
            format='json',
        )

        self.assertEqual(patch_response.status_code, 200)
        patch_payload = patch_response.json()
        self.assertEqual(patch_payload['name'], 'Nocturnist 12 Shifts')
        self.assertEqual(patch_payload['assigned_users_count'], 0)

    def test_deactivate_then_reactivate_contract(self):
        create_response = self.client.post('/api/contracts/', data=self._build_payload(), format='json')
        contract_id = create_response.json()['id']

        deactivate_response = self.client.post(f'/api/contracts/{contract_id}/deactivate/', data={}, format='json')
        self.assertEqual(deactivate_response.status_code, 200)
        self.assertFalse(deactivate_response.json()['active'])

        reactivate_response = self.client.post(f'/api/contracts/{contract_id}/reactivate/', data={}, format='json')
        self.assertEqual(reactivate_response.status_code, 200)
        self.assertTrue(reactivate_response.json()['active'])

    def test_duplicate_contract_creates_inactive_copy(self):
        create_response = self.client.post('/api/contracts/', data=self._build_payload(), format='json')
        contract_id = create_response.json()['id']

        duplicate_response = self.client.post(f'/api/contracts/{contract_id}/duplicate/', data={}, format='json')

        self.assertEqual(duplicate_response.status_code, 201)
        duplicate_payload = duplicate_response.json()
        self.assertFalse(duplicate_payload['active'])
        self.assertIn('(Copy)', duplicate_payload['name'])
        self.assertEqual(duplicate_payload['domain'], self.domain.id)

    def test_inactive_contract_cannot_be_assigned(self):
        payload = self._build_payload()
        payload['active'] = False

        response = self.client.post('/api/contracts/', data=payload, format='json')

        self.assertEqual(response.status_code, 400)
        self.assertIn('assigned_user_ids', response.json())
