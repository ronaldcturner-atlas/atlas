from datetime import date, datetime, time

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Physician
from apps.domains.models import Domain
from apps.facilities.models import Facility
from .models import (
    Contract, ContractUserAssignment, OptimizerRun, ScheduleBlock,
    ScheduleShiftAssignment, ScheduleShiftInstance, ScheduleVersion,
    ShiftPosting, ShiftStatsGroup, ShiftTemplate, ShiftTrade, ShiftTradePolicy,
)


class ShiftTradeApiTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner_user = User.objects.create_user('owner', password='x')
        self.requester_user = User.objects.create_user('requester', password='x')
        self.scheduler_user = User.objects.create_user('scheduler', password='x')
        scheduler_group, _ = Group.objects.get_or_create(name='Scheduler')
        self.scheduler_user.groups.add(scheduler_group)
        self.owner = Physician.objects.create(user=self.owner_user, display_name='Owner')
        self.requester = Physician.objects.create(user=self.requester_user, display_name='Requester')
        self.facility = Facility.objects.create(name='Hospital', short_name='H')
        self.domain = Domain.objects.create(name='Physician', active=True)
        contract = Contract.objects.create(name='Test', domain=self.domain, active=True)
        contract.facilities.add(self.facility)
        ContractUserAssignment.objects.create(contract=contract, physician=self.owner)
        ContractUserAssignment.objects.create(contract=contract, physician=self.requester)
        self.block = ScheduleBlock.objects.create(
            start_date=date(2026, 9, 1), end_date=date(2026, 9, 30),
            request_open_datetime=timezone.now(), request_close_datetime=timezone.now(),
            build_status=ScheduleBlock.BuildStatus.ARCHIVE, published_at=timezone.now(),
        )
        self.version = ScheduleVersion.objects.create(schedule_block=self.block, domain=self.domain, version_number=1, name='Published')
        self.template = ShiftTemplate.objects.create(
            facility=self.facility, name='Day', start_time=time(7), end_time=time(16),
            active_days_of_week=['Tuesday'], weekend_days=[], default_staffing_count=1,
        )
        self.run = OptimizerRun.objects.create(schedule_version=self.version, run_number=1, status=OptimizerRun.Status.COMPLETED, is_active=True)
        instance = ScheduleShiftInstance.objects.create(
            schedule_version=self.version, schedule_block=self.block, date=date(2026, 9, 1),
            shift_template=self.template, facility=self.facility,
            start_datetime=timezone.make_aware(datetime(2026, 9, 1, 7)),
            end_datetime=timezone.make_aware(datetime(2026, 9, 1, 16)), status=ScheduleShiftInstance.Status.ASSIGNED,
        )
        self.assignment = ScheduleShiftAssignment.objects.create(
            shift_instance=instance, physician=self.owner,
            assignment_source=ScheduleShiftAssignment.AssignmentSource.OPTIMIZER, optimizer_run=self.run,
        )
        self.client = APIClient()

    def test_owner_can_post_and_split_shift(self):
        self.client.force_authenticate(self.owner_user)
        posted = self.client.post(f'/api/schedule-assignments/{self.assignment.id}/posting/', {'mode': 'PICKUP'}, format='json')
        self.assertEqual(posted.status_code, 200)
        self.assertEqual(posted.json()['posting_mode'], 'PICKUP')
        split = self.client.post(f'/api/schedule-assignments/{self.assignment.id}/split/', {'split_time': '12:00'}, format='json')
        self.assertEqual(split.status_code, 200)
        self.assertEqual(ScheduleShiftInstance.objects.filter(schedule_version=self.version).count(), 2)
        self.assertEqual(ScheduleShiftAssignment.objects.filter(physician=self.owner).count(), 2)
        self.assertFalse(ShiftPosting.objects.get(assignment=self.assignment).active)
        unsplit = self.client.post(f'/api/schedule-assignments/{self.assignment.id}/unsplit/', {}, format='json')
        self.assertEqual(unsplit.status_code, 200)
        self.assertEqual(ScheduleShiftInstance.objects.filter(schedule_version=self.version).count(), 1)
        self.assignment.shift_instance.refresh_from_db()
        self.assertIsNone(self.assignment.shift_instance.segment_start_time)
        self.assertIsNone(self.assignment.shift_instance.segment_end_time)

    def test_pickup_auto_approves_after_owner_accepts(self):
        ShiftPosting.objects.create(assignment=self.assignment, posted_by=self.owner_user, mode=ShiftPosting.Mode.PICKUP)
        ShiftTradePolicy.objects.create(pk=1, require_scheduler_approval=False)
        self.client.force_authenticate(self.requester_user)
        created = self.client.post('/api/shift-trades/', {'target_assignment_id': self.assignment.id}, format='json')
        self.assertEqual(created.status_code, 201)
        self.client.force_authenticate(self.owner_user)
        accepted = self.client.post(f"/api/shift-trades/{created.json()['id']}/accept/", {}, format='json')
        self.assertEqual(accepted.status_code, 200)
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.physician, self.requester)
        self.assertEqual(accepted.json()['status'], 'APPROVED')

    def test_only_scheduler_can_change_approval_policy(self):
        self.client.force_authenticate(self.owner_user)
        denied = self.client.patch('/api/shift-trade-policy/', {'require_scheduler_approval': False}, format='json')
        self.assertEqual(denied.status_code, 403)
        self.client.force_authenticate(self.scheduler_user)
        updated = self.client.patch('/api/shift-trade-policy/', {'require_scheduler_approval': False}, format='json')
        self.assertEqual(updated.status_code, 200)
        self.assertFalse(updated.json()['require_scheduler_approval'])

    def test_owner_can_propose_direct_trade_from_conflict_free_options(self):
        target_instance = ScheduleShiftInstance.objects.create(
            schedule_version=self.version, schedule_block=self.block, date=date(2026, 9, 2),
            shift_template=self.template, facility=self.facility,
            start_datetime=timezone.make_aware(datetime(2026, 9, 2, 7)),
            end_datetime=timezone.make_aware(datetime(2026, 9, 2, 16)), status=ScheduleShiftInstance.Status.ASSIGNED,
        )
        target = ScheduleShiftAssignment.objects.create(
            shift_instance=target_instance, physician=self.requester,
            assignment_source=ScheduleShiftAssignment.AssignmentSource.OPTIMIZER, optimizer_run=self.run,
        )
        self.client.force_authenticate(self.owner_user)
        options = self.client.get(f'/api/schedule-assignments/{self.assignment.id}/trade-options/')
        self.assertEqual(options.status_code, 200)
        self.assertEqual([option['id'] for option in options.json()], [target.id])
        proposed = self.client.post('/api/shift-trades/', {
            'offered_assignment_id': self.assignment.id,
            'target_assignment_id': target.id,
            'note': 'Would you swap?',
        }, format='json')
        self.assertEqual(proposed.status_code, 201)
        self.assertEqual(proposed.json()['trade_type'], 'TRADE')
        self.assertEqual(proposed.json()['requested_assignment']['id'], self.assignment.id)

    def test_accepting_trade_cancels_competing_offers_for_same_shift(self):
        User = get_user_model()
        second_user = User.objects.create_user('second', password='x')
        second = Physician.objects.create(user=second_user, display_name='Second')
        contract = Contract.objects.get(name='Test')
        ContractUserAssignment.objects.create(contract=contract, physician=second)

        targets = []
        for day, physician in ((2, self.requester), (3, second)):
            instance = ScheduleShiftInstance.objects.create(
                schedule_version=self.version, schedule_block=self.block, date=date(2026, 9, day),
                shift_template=self.template, facility=self.facility,
                start_datetime=timezone.make_aware(datetime(2026, 9, day, 7)),
                end_datetime=timezone.make_aware(datetime(2026, 9, day, 16)),
                status=ScheduleShiftInstance.Status.ASSIGNED,
            )
            targets.append(ScheduleShiftAssignment.objects.create(
                shift_instance=instance, physician=physician,
                assignment_source=ScheduleShiftAssignment.AssignmentSource.OPTIMIZER,
                optimizer_run=self.run,
            ))
        self.client.force_authenticate(self.owner_user)
        trade_ids = []
        for target in targets:
            response = self.client.post('/api/shift-trades/', {
                'offered_assignment_id': self.assignment.id,
                'target_assignment_id': target.id,
            }, format='json')
            self.assertEqual(response.status_code, 201)
            trade_ids.append(response.json()['id'])
        self.client.force_authenticate(self.requester_user)
        accepted = self.client.post(f'/api/shift-trades/{trade_ids[0]}/accept/', {}, format='json')
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.json()['status'], ShiftTrade.Status.PENDING_SCHEDULER)
        self.assertEqual(ShiftTrade.objects.get(id=trade_ids[1]).status, ShiftTrade.Status.CANCELLED)

    def test_deleting_block_handles_manual_and_optimizer_assignment_collision(self):
        ScheduleShiftAssignment.objects.create(
            shift_instance=self.assignment.shift_instance,
            physician=self.owner,
            assignment_source=ScheduleShiftAssignment.AssignmentSource.MANUAL,
            optimizer_run=None,
        )
        self.block.published_at = None
        self.block.build_status = ScheduleBlock.BuildStatus.BUILD
        self.block.save(update_fields=['published_at', 'build_status'])
        self.client.force_authenticate(self.scheduler_user)

        response = self.client.delete(f'/api/schedule-blocks/{self.block.id}/')

        self.assertEqual(response.status_code, 204)
        self.assertFalse(ScheduleBlock.objects.filter(id=self.block.id).exists())

    def test_scheduler_manages_stats_groups_and_published_rows_identify_template(self):
        self.client.force_authenticate(self.owner_user)
        denied = self.client.post('/api/stats-groups/', {
            'name': 'Evenings', 'shift_template_ids': [self.template.id],
        }, format='json')
        self.assertEqual(denied.status_code, 403)

        self.client.force_authenticate(self.scheduler_user)
        created = self.client.post('/api/stats-groups/', {
            'name': 'Evenings', 'shift_template_ids': [self.template.id],
        }, format='json')
        self.assertEqual(created.status_code, 201)
        group_id = created.json()['id']
        self.assertEqual(created.json()['shift_template_ids'], [self.template.id])
        self.assertTrue(ShiftStatsGroup.objects.filter(id=group_id).exists())

        schedule = self.client.get('/api/published-schedule/')
        assignment_row = next(row for row in schedule.json() if row['id'] == self.assignment.id)
        self.assertEqual(assignment_row['shift_template_id'], self.template.id)

        updated = self.client.patch(f'/api/stats-groups/{group_id}/', {
            'name': 'Premium evenings', 'shift_template_ids': [self.template.id],
        }, format='json')
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()['name'], 'Premium evenings')
        deleted = self.client.delete(f'/api/stats-groups/{group_id}/')
        self.assertEqual(deleted.status_code, 204)
        self.assertFalse(ShiftStatsGroup.objects.filter(id=group_id).exists())

    def test_scheduler_changes_actual_instance_times_without_changing_template(self):
        self.client.force_authenticate(self.owner_user)
        denied = self.client.patch(
            f'/api/shift-instances/{self.assignment.shift_instance_id}/times/',
            {'start_time': '07:00', 'end_time': '17:00'}, format='json',
        )
        self.assertEqual(denied.status_code, 403)

        self.client.force_authenticate(self.scheduler_user)
        updated = self.client.patch(
            f'/api/shift-instances/{self.assignment.shift_instance_id}/times/',
            {'start_time': '07:00', 'end_time': '17:00'}, format='json',
        )
        self.assertEqual(updated.status_code, 200)
        self.template.refresh_from_db()
        self.assertEqual(self.template.end_time, time(16))
        schedule = self.client.get('/api/published-schedule/').json()
        row = next(item for item in schedule if item['id'] == self.assignment.id)
        self.assertEqual(row['start_time'], '07:00:00')
        self.assertEqual(row['end_time'], '17:00:00')
