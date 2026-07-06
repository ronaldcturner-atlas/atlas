from datetime import date, datetime, time

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Physician
from apps.domains.models import Domain
from apps.facilities.models import Facility

from .models import (
    Contract,
    ContractUserAssignment,
    ScheduleBlock,
    ScheduleRequest,
    ScheduleShiftInstance,
    ScheduleVersion,
    ShiftTemplate,
)
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
        ShiftTemplate.objects.create(
            facility=self.facility,
            start_time=time(10, 0),
            end_time=time(18, 0),
            active_days_of_week=['Monday'],
            weekend_days=[],
            night_shift=False,
            default_staffing_count=1,
            active=False,
        )
        self.block = ScheduleBlock.objects.create(
            start_date=date(2026, 7, 6),
            end_date=date(2026, 7, 7),
            request_open_datetime=timezone.make_aware(datetime(2026, 5, 1, 12, 0)),
            request_close_datetime=timezone.make_aware(datetime(2026, 5, 15, 12, 0)),
            build_status=ScheduleBlock.BuildStatus.PRE_BUILD,
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
        day_instance = instances.get(shift_template=self.day_template)
        self.assertEqual(day_instance.date, date(2026, 7, 6))
        self.assertEqual(day_instance.required_staffing, 2)
        self.assertIsNone(day_instance.assigned_user)
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
