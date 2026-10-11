from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Physician
from apps.domains.models import (
    Domain, DomainMembership, Organization, OrganizationMembership, Region,
    RoleTemplate,
)
from apps.domains.permissions import CLINICAL_DEFAULTS, SCHEDULER_DEFAULTS
from apps.facilities.models import Facility

from .models import (
    DomainShiftConfiguration, OptimizerRun, PublishedShiftBackdate,
    ScheduleBlock, ScheduleBlockShiftChange, ScheduleShiftAssignment,
    ScheduleShiftInstance, ScheduleVersion, ShiftTemplate, ShiftTrade,
)


class ShiftBackdateWorkflowTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.organization = Organization.objects.create(name='Backdate Test Organization')
        self.region = Region.objects.create(organization=self.organization, name='Backdate Region')
        self.domain = Domain.objects.create(region=self.region, name='Physician')
        self.facility = Facility.objects.create(
            region=self.region,
            name='Backdate Hospital',
            short_name='BDH',
            timezone='America/New_York',
        )
        self.scheduler = User.objects.create_user('backdate-scheduler', password='x')
        self.owner_user = User.objects.create_user('backdate-owner', password='x')
        self.recipient_user = User.objects.create_user('backdate-recipient', password='x')
        self.owner = Physician.objects.create(user=self.owner_user, display_name='Owner')
        self.recipient = Physician.objects.create(user=self.recipient_user, display_name='Recipient')
        scheduler_role = RoleTemplate.objects.create(
            region=self.region,
            name='Backdate Scheduler',
            system_key='scheduler',
            permissions=sorted(SCHEDULER_DEFAULTS),
        )
        clinical_role = RoleTemplate.objects.create(
            region=self.region,
            name='Backdate Physician',
            system_key='staff_physician',
            permissions=sorted(CLINICAL_DEFAULTS),
        )
        for user in (self.scheduler, self.owner_user, self.recipient_user):
            OrganizationMembership.objects.create(organization=self.organization, user=user)
        DomainMembership.objects.create(
            domain=self.domain,
            user=self.scheduler,
            role=DomainMembership.Role.SCHEDULER,
            role_template=scheduler_role,
            clinically_active=False,
        )
        for user in (self.owner_user, self.recipient_user):
            DomainMembership.objects.create(
                domain=self.domain,
                user=user,
                role=DomainMembership.Role.STAFF_PHYSICIAN,
                role_template=clinical_role,
                clinically_active=True,
            )
        self.client = APIClient()
        self.client.force_authenticate(self.scheduler)
        self.future_date = timezone.localdate() + timedelta(days=10)
        self.template = ShiftTemplate.objects.create(
            domain=self.domain,
            facility=self.facility,
            name='Generated',
            start_time=time(7),
            end_time=time(15),
            active_days_of_week=[self.future_date.strftime('%A')],
            weekend_days=[],
            default_staffing_count=1,
        )

    def _block(self, *, published=False):
        start = self.future_date
        end = start + timedelta(days=20)
        return ScheduleBlock.objects.create(
            domain=self.domain,
            start_date=start,
            end_date=end,
            request_open_datetime=timezone.now(),
            request_close_datetime=timezone.now() + timedelta(hours=1),
            build_status=(
                ScheduleBlock.BuildStatus.ARCHIVE
                if published else ScheduleBlock.BuildStatus.BUILD
            ),
            published_at=timezone.now() if published else None,
        )

    def _published_schedule(self):
        block = self._block(published=True)
        version = ScheduleVersion.objects.create(
            schedule_block=block,
            domain=self.domain,
            version_number=1,
            name='Published',
        )
        run = OptimizerRun.objects.create(
            schedule_version=version,
            run_number=1,
            status=OptimizerRun.Status.COMPLETED,
            is_active=True,
        )
        version.published_optimizer_run = run
        version.save(update_fields=['published_optimizer_run'])
        facility_zone = ZoneInfo(self.facility.timezone)
        start_at = datetime.combine(self.future_date, time(7), tzinfo=facility_zone)
        end_at = datetime.combine(self.future_date, time(15), tzinfo=facility_zone)
        instance = ScheduleShiftInstance.objects.create(
            schedule_version=version,
            schedule_block=block,
            date=self.future_date,
            shift_template=self.template,
            facility=self.facility,
            start_datetime=start_at,
            end_datetime=end_at,
            required_staffing=1,
            status=ScheduleShiftInstance.Status.ASSIGNED,
        )
        assignment = ScheduleShiftAssignment.objects.create(
            shift_instance=instance,
            physician=self.owner,
            assignment_source=ScheduleShiftAssignment.AssignmentSource.OPTIMIZER,
            optimizer_run=run,
        )
        return block, version, run, instance, assignment

    def test_shift_builder_change_blocks_existing_build_until_acknowledged(self):
        block = self._block()
        version = ScheduleVersion.objects.create(
            schedule_block=block,
            domain=self.domain,
            version_number=1,
            name='Build 1',
        )
        run = OptimizerRun.objects.create(
            schedule_version=version,
            run_number=1,
            status=OptimizerRun.Status.COMPLETED,
        )
        ScheduleShiftInstance.objects.create(
            schedule_version=version,
            schedule_block=block,
            date=self.future_date,
            shift_template=self.template,
            facility=self.facility,
            start_datetime=timezone.make_aware(datetime.combine(self.future_date, time(7))),
            end_datetime=timezone.make_aware(datetime.combine(self.future_date, time(15))),
        )

        response = self.client.patch(
            f'/api/shift-templates/{self.template.id}/',
            {'end_time': '17:00:00'},
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ScheduleBlockShiftChange.objects.filter(
            schedule_block=block, acknowledged_at__isnull=True,
        ).count(), 1)
        blocked = self.client.get(f'/api/schedule-blocks/{block.id}/build/')
        self.assertEqual(blocked.status_code, 200)
        self.assertTrue(blocked.json()['entry_blocked_by_shift_changes'])

        acknowledged = self.client.post(
            f'/api/schedule-blocks/{block.id}/build/acknowledge-shift-changes/',
            {},
            format='json',
        )
        self.assertEqual(acknowledged.status_code, 200)
        run.refresh_from_db()
        version.refresh_from_db()
        self.assertTrue(run.score_is_stale)
        self.assertEqual(
            version.shift_configuration_revision,
            DomainShiftConfiguration.objects.get(domain=self.domain).revision,
        )
        self.assertFalse(ScheduleBlockShiftChange.objects.filter(
            schedule_block=block, acknowledged_at__isnull=True,
        ).exists())

    def test_backdate_preview_and_apply_retain_assignment_and_cancel_trade(self):
        block, _version, _run, instance, assignment = self._published_schedule()
        trade = ShiftTrade.objects.create(
            domain=self.domain,
            offered_assignment=assignment,
            requester=self.recipient,
            recipient=self.owner,
            status=ShiftTrade.Status.PENDING_RECIPIENT,
        )
        self.client.patch(
            f'/api/shift-templates/{self.template.id}/',
            {'end_time': '17:00:00'},
            format='json',
        )
        payload = {
            'shift_template_ids': [self.template.id],
            'start_date': self.future_date.isoformat(),
            'end_date': self.future_date.isoformat(),
        }
        preview = self.client.post(
            f'/api/schedule-blocks/{block.id}/backdate-shifts/', payload, format='json',
        )
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.json()['changes'][0]['action'], 'UPDATE')

        applied = self.client.post(
            f'/api/schedule-blocks/{block.id}/backdate-shifts/',
            {**payload, 'apply': True},
            format='json',
        )
        self.assertEqual(applied.status_code, 200)
        instance.refresh_from_db()
        assignment.refresh_from_db()
        trade.refresh_from_db()
        self.assertEqual(
            instance.end_datetime.astimezone(ZoneInfo('America/New_York')).hour,
            17,
        )
        self.assertEqual(assignment.physician_id, self.owner.id)
        self.assertEqual(trade.status, ShiftTrade.Status.CANCELLED)
        self.assertIn('backdated', trade.note)
        self.assertEqual(PublishedShiftBackdate.objects.count(), 1)

        self.client.force_authenticate(self.recipient_user)
        notification = self.client.get('/api/shift-trades/')
        self.assertEqual(notification.status_code, 200)
        cancelled_payload = next(row for row in notification.json() if row['id'] == trade.id)
        self.assertTrue(cancelled_payload['is_unseen'])
        self.assertIn('backdated', cancelled_payload['note'])
        seen = self.client.post('/api/shift-trades/mark-seen/', {}, format='json')
        self.assertEqual(seen.status_code, 200)
        self.assertIn(trade.id, seen.json()['seen_trade_ids'])
        refreshed = self.client.get('/api/shift-trades/')
        refreshed_payload = next(row for row in refreshed.json() if row['id'] == trade.id)
        self.assertFalse(refreshed_payload['is_unseen'])

    def test_new_backdated_shift_is_published_as_open(self):
        block, version, run, _instance, _assignment = self._published_schedule()
        new_template = ShiftTemplate.objects.create(
            domain=self.domain,
            facility=self.facility,
            name='Generated',
            start_time=time(18),
            end_time=time(23),
            active_days_of_week=[self.future_date.strftime('%A')],
            weekend_days=[],
            default_staffing_count=1,
        )
        response = self.client.post(
            f'/api/schedule-blocks/{block.id}/backdate-shifts/',
            {
                'shift_template_ids': [new_template.id],
                'start_date': self.future_date.isoformat(),
                'end_date': self.future_date.isoformat(),
                'apply': True,
            },
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        created = ScheduleShiftInstance.objects.get(
            schedule_version=version,
            date=self.future_date,
            shift_template=new_template,
        )
        run.refresh_from_db()
        self.assertTrue(created.is_locked_open)
        self.assertIn(created.id, run.locked_open_shift_instance_ids)
        self.assertFalse(created.assignments.exists())

    def test_removed_weekday_is_hidden_and_assignment_can_transfer_to_replacement(self):
        block, version, run, old_instance, old_assignment = self._published_schedule()
        block.end_date = self.future_date
        block.save(update_fields=['end_date'])
        removed_day = self.future_date.strftime('%A')
        self.template.active_days_of_week = []
        self.template.save(update_fields=['active_days_of_week'])
        replacement = ShiftTemplate.objects.create(
            domain=self.domain,
            facility=self.facility,
            name='Generated',
            start_time=time(12),
            end_time=time(20),
            active_days_of_week=[removed_day],
            weekend_days=[],
            default_staffing_count=1,
        )
        payload = {
            'shift_template_ids': [self.template.id, replacement.id],
            'start_date': self.future_date.isoformat(),
            'end_date': self.future_date.isoformat(),
        }

        context = self.client.get(
            f'/api/schedule-blocks/{block.id}/backdate-shifts/context/',
        )
        self.assertEqual(context.status_code, 200)
        self.assertEqual(
            {row['id'] for row in context.json()['shift_templates']},
            {self.template.id, replacement.id},
        )

        preview = self.client.post(
            f'/api/schedule-blocks/{block.id}/backdate-shifts/', payload, format='json',
        )
        self.assertEqual(preview.status_code, 200)
        removal = next(row for row in preview.json()['changes'] if row['action'] == 'REMOVE')
        addition = next(row for row in preview.json()['changes'] if row['action'] == 'ADD')
        self.assertEqual(removal['shift_instance_id'], old_instance.id)
        self.assertEqual(
            removal['suggested_replacement_shift_template_id'], replacement.id,
        )
        self.assertEqual(
            [row['shift_template_id'] for row in removal['replacement_options']],
            [replacement.id],
        )

        applied = self.client.post(
            f'/api/schedule-blocks/{block.id}/backdate-shifts/',
            {
                **payload,
                'apply': True,
                'assignment_transfers': {str(old_instance.id): replacement.id},
            },
            format='json',
        )
        self.assertEqual(applied.status_code, 200)
        run.refresh_from_db()
        self.assertIn(old_instance.id, run.suppressed_shift_instance_ids)
        self.assertTrue(ScheduleShiftInstance.objects.filter(id=old_instance.id).exists())
        self.assertTrue(ScheduleShiftAssignment.objects.filter(id=old_assignment.id).exists())
        new_instance = ScheduleShiftInstance.objects.get(
            schedule_version=version,
            date=self.future_date,
            shift_template=replacement,
        )
        self.assertTrue(ScheduleShiftAssignment.objects.filter(
            shift_instance=new_instance,
            optimizer_run=run,
            physician=self.owner,
        ).exists())
        self.assertFalse(new_instance.is_locked_open)

        published = self.client.get(
            f'/api/published-schedule/?domain={self.domain.id}',
        )
        self.assertEqual(published.status_code, 200)
        rows = published.json()
        self.assertFalse(any(row['shift_instance_id'] == old_instance.id for row in rows))
        replacement_row = next(
            row for row in rows if row['shift_instance_id'] == new_instance.id
        )
        self.assertEqual(replacement_row['physician'], self.owner.id)

        refreshed_context = self.client.get(
            f'/api/schedule-blocks/{block.id}/backdate-shifts/context/',
        )
        self.assertEqual(refreshed_context.status_code, 200)
        refreshed_ids = {row['id'] for row in refreshed_context.json()['shift_templates']}
        self.assertNotIn(self.template.id, refreshed_ids)
        self.assertNotIn(replacement.id, refreshed_ids)

    def test_backdate_picker_only_lists_modified_or_new_shifts(self):
        block, _version, _run, _instance, _assignment = self._published_schedule()
        block.end_date = self.future_date
        block.save(update_fields=['end_date'])
        unchanged_template = ShiftTemplate.objects.create(
            domain=self.domain,
            facility=self.facility,
            name='Generated',
            start_time=time(9),
            end_time=time(17),
            active_days_of_week=[self.future_date.strftime('%A')],
            weekend_days=[],
            default_staffing_count=1,
        )
        unchanged_instance = ScheduleShiftInstance.objects.create(
            schedule_version=_version,
            schedule_block=block,
            date=self.future_date,
            shift_template=unchanged_template,
            facility=self.facility,
            start_datetime=datetime.combine(
                self.future_date,
                time(9),
                tzinfo=ZoneInfo(self.facility.timezone),
            ),
            end_datetime=datetime.combine(
                self.future_date,
                time(17),
                tzinfo=ZoneInfo(self.facility.timezone),
            ),
            required_staffing=1,
        )
        new_template = ShiftTemplate.objects.create(
            domain=self.domain,
            facility=self.facility,
            name='Generated',
            start_time=time(18),
            end_time=time(23),
            active_days_of_week=[self.future_date.strftime('%A')],
            weekend_days=[],
            default_staffing_count=1,
        )

        initial = self.client.get(
            f'/api/schedule-blocks/{block.id}/backdate-shifts/context/',
        )
        self.assertEqual(initial.status_code, 200)
        initial_ids = {row['id'] for row in initial.json()['shift_templates']}
        self.assertNotIn(self.template.id, initial_ids)
        self.assertNotIn(unchanged_template.id, initial_ids)
        self.assertIn(new_template.id, initial_ids)

        self.template.end_time = time(17)
        self.template.save(update_fields=['end_time'])
        modified = self.client.get(
            f'/api/schedule-blocks/{block.id}/backdate-shifts/context/',
        )
        modified_ids = {row['id'] for row in modified.json()['shift_templates']}
        self.assertIn(self.template.id, modified_ids)
        self.assertNotIn(unchanged_template.id, modified_ids)
        self.assertIn(new_template.id, modified_ids)

        unchanged_instance.delete()

    def test_new_shift_template_notifies_only_existing_build_blocks(self):
        existing_block = self._block()
        response = self.client.post(
            '/api/shift-templates/',
            {
                'domain': self.domain.id,
                'facility': self.facility.id,
                'name': 'Generated',
                'start_time': '16:00:00',
                'end_time': '23:00:00',
                'active_days_of_week': [self.future_date.strftime('%A')],
                'weekend_days': [],
                'default_staffing_count': 1,
                'active': True,
            },
            format='json',
        )
        self.assertEqual(response.status_code, 201)
        self.assertTrue(ScheduleBlockShiftChange.objects.filter(
            schedule_block=existing_block,
            acknowledged_at__isnull=True,
        ).exists())

        revision = DomainShiftConfiguration.objects.get(domain=self.domain).revision
        new_block_response = self.client.post(
            '/api/schedule-blocks/',
            {
                'domain': self.domain.id,
                'start_date': (self.future_date + timedelta(days=30)).isoformat(),
                'end_date': (self.future_date + timedelta(days=40)).isoformat(),
                'request_open_datetime': timezone.now().isoformat(),
                'request_close_datetime': (timezone.now() + timedelta(hours=1)).isoformat(),
            },
            format='json',
        )
        self.assertEqual(new_block_response.status_code, 201)
        new_block = ScheduleBlock.objects.get(id=new_block_response.json()['id'])
        self.assertEqual(new_block.shift_configuration_revision, revision)
        self.assertFalse(ScheduleBlockShiftChange.objects.filter(
            schedule_block=new_block,
            acknowledged_at__isnull=True,
        ).exists())

    def test_pending_shift_change_blocks_optimizer_and_stale_run_preview_publish(self):
        block = self._block()
        version = ScheduleVersion.objects.create(
            schedule_block=block,
            domain=self.domain,
            version_number=1,
            name='Build 1',
        )
        run = OptimizerRun.objects.create(
            schedule_version=version,
            run_number=1,
            status=OptimizerRun.Status.COMPLETED,
            is_active=True,
        )
        changed = self.client.patch(
            f'/api/shift-templates/{self.template.id}/',
            {'end_time': '17:00:00'},
            format='json',
        )
        self.assertEqual(changed.status_code, 200)
        blocked = self.client.post(
            f'/api/schedule-versions/{version.id}/optimizer-preflight/',
            {'start_mode': OptimizerRun.StartMode.FRESH_FILL},
            format='json',
        )
        self.assertEqual(blocked.status_code, 409)
        self.assertTrue(blocked.json()['entry_blocked_by_shift_changes'])

        acknowledged = self.client.post(
            f'/api/schedule-blocks/{block.id}/build/acknowledge-shift-changes/',
            {},
            format='json',
        )
        self.assertEqual(acknowledged.status_code, 200)
        run.refresh_from_db()
        self.assertTrue(run.score_is_stale)
        preview = self.client.post(
            f'/api/schedule-blocks/{block.id}/enter-preview/',
            {'optimizer_run_id': run.id},
            format='json',
        )
        self.assertEqual(preview.status_code, 409)

        block.build_status = ScheduleBlock.BuildStatus.PREVIEW
        block.preview_optimizer_run = run
        block.save(update_fields=['build_status', 'preview_optimizer_run'])
        publish = self.client.post(
            f'/api/schedule-blocks/{block.id}/publish/',
            {'optimizer_run_id': run.id},
            format='json',
        )
        self.assertEqual(publish.status_code, 409)

    def test_backdate_requires_permission_and_rejects_end_after_published_horizon(self):
        block, _version, _run, _instance, _assignment = self._published_schedule()
        payload = {
            'shift_template_ids': [self.template.id],
            'start_date': self.future_date.isoformat(),
            'end_date': (block.end_date + timedelta(days=1)).isoformat(),
        }
        too_late = self.client.post(
            f'/api/schedule-blocks/{block.id}/backdate-shifts/', payload, format='json',
        )
        self.assertEqual(too_late.status_code, 400)
        self.assertIn('end_date', too_late.json())

        self.client.force_authenticate(self.owner_user)
        forbidden = self.client.get(
            f'/api/schedule-blocks/{block.id}/backdate-shifts/context/',
        )
        self.assertEqual(forbidden.status_code, 403)

    def test_backdate_skips_shift_that_has_already_started(self):
        today = timezone.localdate()
        started_template = ShiftTemplate.objects.create(
            domain=self.domain,
            facility=self.facility,
            name='Started',
            start_time=time(0),
            end_time=time(23, 59),
            active_days_of_week=[today.strftime('%A')],
            weekend_days=[],
            default_staffing_count=1,
        )
        block = ScheduleBlock.objects.create(
            domain=self.domain,
            start_date=today,
            end_date=today,
            request_open_datetime=timezone.now() - timedelta(days=1),
            request_close_datetime=timezone.now() - timedelta(hours=1),
            build_status=ScheduleBlock.BuildStatus.ARCHIVE,
            published_at=timezone.now(),
        )
        version = ScheduleVersion.objects.create(
            schedule_block=block,
            domain=self.domain,
            version_number=1,
            name='Published',
        )
        run = OptimizerRun.objects.create(
            schedule_version=version,
            run_number=1,
            status=OptimizerRun.Status.COMPLETED,
            is_active=True,
        )
        version.published_optimizer_run = run
        version.save(update_fields=['published_optimizer_run'])
        response = self.client.post(
            f'/api/schedule-blocks/{block.id}/backdate-shifts/',
            {
                'shift_template_ids': [started_template.id],
                'start_date': today.isoformat(),
                'end_date': today.isoformat(),
            },
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['changes'], [])
        self.assertEqual(len(response.json()['skipped_started_shifts']), 1)

    def test_backdate_uses_later_authoritative_published_block(self):
        first_block, _first_version, _first_run, _instance, _assignment = self._published_schedule()
        second_date = self.future_date + timedelta(days=7)
        second_block = ScheduleBlock.objects.create(
            domain=self.domain,
            start_date=second_date,
            end_date=second_date,
            request_open_datetime=timezone.now() - timedelta(days=1),
            request_close_datetime=timezone.now() - timedelta(hours=1),
            build_status=ScheduleBlock.BuildStatus.ARCHIVE,
            published_at=timezone.now() + timedelta(seconds=1),
        )
        second_version = ScheduleVersion.objects.create(
            schedule_block=second_block,
            domain=self.domain,
            version_number=1,
            name='Published 2',
        )
        second_run = OptimizerRun.objects.create(
            schedule_version=second_version,
            run_number=1,
            status=OptimizerRun.Status.COMPLETED,
            is_active=True,
        )
        second_version.published_optimizer_run = second_run
        second_version.save(update_fields=['published_optimizer_run'])
        self.template.active_days_of_week = [
            self.future_date.strftime('%A'),
            second_date.strftime('%A'),
        ]
        self.template.end_time = time(17)
        self.template.save(update_fields=['active_days_of_week', 'end_time'])
        response = self.client.post(
            f'/api/schedule-blocks/{first_block.id}/backdate-shifts/',
            {
                'shift_template_ids': [self.template.id],
                'start_date': second_date.isoformat(),
                'end_date': second_date.isoformat(),
                'apply': True,
            },
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        created = ScheduleShiftInstance.objects.get(
            schedule_version=second_version,
            date=second_date,
            shift_template=self.template,
        )
        self.assertEqual(created.schedule_block_id, second_block.id)
        self.assertFalse(ScheduleShiftInstance.objects.filter(
            schedule_version=_first_version,
            date=second_date,
            shift_template=self.template,
        ).exists())

