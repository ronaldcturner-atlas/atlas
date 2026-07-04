from datetime import date, datetime, time

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Physician
from apps.facilities.models import Facility

from .models import ScheduleBlock, ScheduleRequest, ShiftTemplate
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
        self.physician.active = False
        self.physician.save(update_fields=['active'])

        response = self.client.get(f'/api/schedule-blocks/{self.block.id}/requests/context/')

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['schedule_block']['id'], self.block.id)
        self.assertEqual(payload['selected_physician_id'], self.physician.id)
        self.assertGreaterEqual(len(payload['physicians']), 1)
