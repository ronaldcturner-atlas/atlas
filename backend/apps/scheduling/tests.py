from datetime import date, datetime, time

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.facilities.models import Facility

from .models import ScheduleBlock, ShiftTemplate
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
