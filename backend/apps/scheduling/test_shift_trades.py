from datetime import date, datetime, time

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Physician
from apps.domains.models import Domain, DomainMembership, OrganizationMembership, RoleTemplate
from apps.domains.permissions import CLINICAL_DEFAULTS, SCHEDULER_DEFAULTS
from apps.facilities.models import Facility
from .models import (
    Contract, ContractUserAssignment, OptimizerRun, ScheduleBlock,
    ScheduleCommentSeries, ScheduleCommentSeriesException, ScheduleDateComment,
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
        OrganizationMembership.objects.create(
            organization=self.domain.region.organization,
            user=self.scheduler_user,
        )
        scheduler_role = RoleTemplate.objects.create(
            region=self.domain.region,
            name='Test Scheduler',
            system_key='scheduler',
            permissions=sorted(SCHEDULER_DEFAULTS),
        )
        clinical_role = RoleTemplate.objects.create(
            region=self.domain.region,
            name='Test Staff Physician',
            system_key='staff_physician',
            permissions=sorted(CLINICAL_DEFAULTS),
        )
        DomainMembership.objects.create(
            domain=self.domain,
            user=self.scheduler_user,
            role=DomainMembership.Role.SCHEDULER,
            role_template=scheduler_role,
            clinically_active=False,
        )
        for clinical_user in (self.owner_user, self.requester_user):
            OrganizationMembership.objects.create(
                organization=self.domain.region.organization,
                user=clinical_user,
            )
            DomainMembership.objects.create(
                domain=self.domain,
                user=clinical_user,
                role=DomainMembership.Role.STAFF_PHYSICIAN,
                role_template=clinical_role,
                clinically_active=True,
            )
        contract = Contract.objects.create(name='Test', domain=self.domain, active=True)
        contract.facilities.add(self.facility)
        ContractUserAssignment.objects.create(contract=contract, physician=self.owner)
        ContractUserAssignment.objects.create(contract=contract, physician=self.requester)
        self.block = ScheduleBlock.objects.create(
            domain=self.domain,
            start_date=date(2026, 9, 1), end_date=date(2026, 9, 30),
            request_open_datetime=timezone.now(), request_close_datetime=timezone.now(),
            build_status=ScheduleBlock.BuildStatus.ARCHIVE, published_at=timezone.now(),
        )
        self.version = ScheduleVersion.objects.create(schedule_block=self.block, domain=self.domain, version_number=1, name='Published')
        self.template = ShiftTemplate.objects.create(
            domain=self.domain, facility=self.facility, name='Day', start_time=time(7), end_time=time(16),
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

    def test_owner_can_unsplit_after_cancelled_trade_history(self):
        self.client.force_authenticate(self.owner_user)
        split = self.client.post(
            f'/api/schedule-assignments/{self.assignment.id}/split/',
            {'split_time': '12:00'},
            format='json',
        )
        self.assertEqual(split.status_code, 200)
        derived_assignment = ScheduleShiftAssignment.objects.exclude(
            id=self.assignment.id,
        ).get(physician=self.owner)
        historical_trade = ShiftTrade.objects.create(
            offered_assignment=derived_assignment,
            requested_assignment=self.assignment,
            requester=self.owner,
            recipient=self.owner,
            trade_type=ShiftTrade.TradeType.TRADE,
            status=ShiftTrade.Status.CANCELLED,
        )

        unsplit = self.client.post(
            f'/api/schedule-assignments/{self.assignment.id}/unsplit/',
            {},
            format='json',
        )

        self.assertEqual(unsplit.status_code, 200)
        self.assertEqual(
            ScheduleShiftInstance.objects.filter(schedule_version=self.version).count(),
            1,
        )
        historical_trade.refresh_from_db()
        self.assertEqual(historical_trade.offered_assignment_id, self.assignment.id)
        self.assertEqual(historical_trade.requested_assignment_id, self.assignment.id)
        self.assertEqual(historical_trade.status, ShiftTrade.Status.CANCELLED)

    def test_mixed_split_owners_require_scheduler_selection_and_override(self):
        self.client.force_authenticate(self.owner_user)
        split = self.client.post(
            f'/api/schedule-assignments/{self.assignment.id}/split/',
            {'split_time': '12:00'},
            format='json',
        )
        self.assertEqual(split.status_code, 200)
        derived_assignment = ScheduleShiftAssignment.objects.exclude(
            id=self.assignment.id,
        ).get(physician=self.owner)
        derived_assignment.physician = self.requester
        derived_assignment.save(update_fields=['physician'])

        denied = self.client.post(
            f'/api/schedule-assignments/{self.assignment.id}/unsplit/',
            {},
            format='json',
        )
        self.assertEqual(denied.status_code, 403)
        self.assertIn('Contact an administrator or scheduler', denied.json()['detail'])

        self.client.force_authenticate(self.scheduler_user)
        selection_required = self.client.post(
            f'/api/schedule-assignments/{self.assignment.id}/unsplit/',
            {},
            format='json',
        )
        self.assertEqual(selection_required.status_code, 409)
        self.assertTrue(selection_required.json()['requires_physician_selection'])
        warning = self.client.post(
            f'/api/schedule-assignments/{self.assignment.id}/unsplit/',
            {'physician_id': self.requester.id},
            format='json',
        )
        self.assertEqual(warning.status_code, 409)
        self.assertTrue(warning.json()['requires_confirmation'])
        completed = self.client.post(
            f'/api/schedule-assignments/{self.assignment.id}/unsplit/',
            {'physician_id': self.requester.id, 'force': True},
            format='json',
        )
        self.assertEqual(completed.status_code, 200)
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.physician, self.requester)
        self.assertEqual(
            ScheduleShiftInstance.objects.filter(schedule_version=self.version).count(),
            1,
        )

    def test_scheduler_can_confirm_overlapping_reassignment(self):
        overlap_instance = ScheduleShiftInstance.objects.create(
            schedule_version=self.version,
            schedule_block=self.block,
            date=date(2026, 9, 1),
            shift_template=self.template,
            facility=self.facility,
            start_datetime=timezone.make_aware(datetime(2026, 9, 1, 8)),
            end_datetime=timezone.make_aware(datetime(2026, 9, 1, 12)),
            status=ScheduleShiftInstance.Status.ASSIGNED,
        )
        ScheduleShiftAssignment.objects.create(
            shift_instance=overlap_instance,
            physician=self.requester,
            assignment_source=ScheduleShiftAssignment.AssignmentSource.MANUAL,
            optimizer_run=self.run,
        )
        self.client.force_authenticate(self.scheduler_user)

        warning = self.client.post(
            f'/api/schedule-assignments/{self.assignment.id}/reassign/',
            {'physician_id': self.requester.id},
            format='json',
        )
        self.assertEqual(warning.status_code, 409)
        self.assertTrue(warning.json()['requires_confirmation'])
        completed = self.client.post(
            f'/api/schedule-assignments/{self.assignment.id}/reassign/',
            {'physician_id': self.requester.id, 'force': True},
            format='json',
        )
        self.assertEqual(completed.status_code, 200)
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.physician, self.requester)

    def test_scheduler_can_open_shift_without_losing_trade_history(self):
        historical_trade = ShiftTrade.objects.create(
            offered_assignment=self.assignment,
            requester=self.requester,
            recipient=self.owner,
            trade_type=ShiftTrade.TradeType.PICKUP,
            status=ShiftTrade.Status.CANCELLED,
        )
        ShiftPosting.objects.create(
            assignment=self.assignment,
            posted_by=self.owner_user,
            mode=ShiftPosting.Mode.PICKUP,
        )
        self.client.force_authenticate(self.scheduler_user)

        opened = self.client.post(
            f'/api/schedule-assignments/{self.assignment.id}/open/',
            {},
            format='json',
        )

        self.assertEqual(opened.status_code, 200)
        self.assertFalse(ScheduleShiftAssignment.objects.filter(
            id=self.assignment.id,
        ).exists())
        historical_trade.refresh_from_db()
        self.assertIsNone(historical_trade.offered_assignment_id)
        self.assertEqual(
            historical_trade.offered_assignment_snapshot['physician_name'],
            'Owner',
        )
        schedule = self.client.get('/api/published-schedule/').json()
        open_row = next(
            row for row in schedule
            if row['shift_instance_id'] == self.assignment.shift_instance_id
        )
        self.assertEqual(open_row['status'], 'open')
        trades = self.client.get('/api/shift-trades/').json()
        payload = next(row for row in trades if row['id'] == historical_trade.id)
        self.assertEqual(payload['offered_assignment']['physician_name'], 'Owner')

    def test_scheduler_can_fill_open_shift_with_overlap_override(self):
        overlap_instance = ScheduleShiftInstance.objects.create(
            schedule_version=self.version,
            schedule_block=self.block,
            date=date(2026, 9, 1),
            shift_template=self.template,
            facility=self.facility,
            start_datetime=timezone.make_aware(datetime(2026, 9, 1, 8)),
            end_datetime=timezone.make_aware(datetime(2026, 9, 1, 12)),
            status=ScheduleShiftInstance.Status.ASSIGNED,
        )
        ScheduleShiftAssignment.objects.create(
            shift_instance=overlap_instance,
            physician=self.requester,
            assignment_source=ScheduleShiftAssignment.AssignmentSource.MANUAL,
            optimizer_run=self.run,
        )
        instance_id = self.assignment.shift_instance_id
        self.client.force_authenticate(self.scheduler_user)
        opened = self.client.post(
            f'/api/schedule-assignments/{self.assignment.id}/open/',
            {},
            format='json',
        )
        self.assertEqual(opened.status_code, 200)

        warning = self.client.post(
            f'/api/shift-instances/{instance_id}/assign/',
            {'physician_id': self.requester.id},
            format='json',
        )
        self.assertEqual(warning.status_code, 409)
        self.assertTrue(warning.json()['requires_confirmation'])
        assigned = self.client.post(
            f'/api/shift-instances/{instance_id}/assign/',
            {'physician_id': self.requester.id, 'force': True},
            format='json',
        )
        self.assertEqual(assigned.status_code, 200)
        replacement = ScheduleShiftAssignment.objects.get(
            shift_instance_id=instance_id,
            optimizer_run=self.run,
        )
        self.assertEqual(replacement.physician, self.requester)
        self.assertTrue(replacement.is_locked)
        replacement.shift_instance.refresh_from_db()
        self.assertFalse(replacement.shift_instance.is_locked_open)

    def test_scheduler_can_directly_swap_users_with_conflict_override(self):
        target_instance = ScheduleShiftInstance.objects.create(
            schedule_version=self.version,
            schedule_block=self.block,
            date=date(2026, 9, 2),
            shift_template=self.template,
            facility=self.facility,
            start_datetime=timezone.make_aware(datetime(2026, 9, 2, 7)),
            end_datetime=timezone.make_aware(datetime(2026, 9, 2, 16)),
            status=ScheduleShiftInstance.Status.ASSIGNED,
        )
        target = ScheduleShiftAssignment.objects.create(
            shift_instance=target_instance,
            physician=self.requester,
            assignment_source=ScheduleShiftAssignment.AssignmentSource.OPTIMIZER,
            optimizer_run=self.run,
        )
        owner_overlap_instance = ScheduleShiftInstance.objects.create(
            schedule_version=self.version,
            schedule_block=self.block,
            date=date(2026, 9, 2),
            shift_template=self.template,
            facility=self.facility,
            start_datetime=timezone.make_aware(datetime(2026, 9, 2, 8)),
            end_datetime=timezone.make_aware(datetime(2026, 9, 2, 12)),
            status=ScheduleShiftInstance.Status.ASSIGNED,
        )
        ScheduleShiftAssignment.objects.create(
            shift_instance=owner_overlap_instance,
            physician=self.owner,
            assignment_source=ScheduleShiftAssignment.AssignmentSource.MANUAL,
            optimizer_run=self.run,
        )
        self.client.force_authenticate(self.scheduler_user)

        warning = self.client.post(
            f'/api/schedule-assignments/{self.assignment.id}/swap/',
            {'target_assignment_id': target.id},
            format='json',
        )
        self.assertEqual(warning.status_code, 409)
        completed = self.client.post(
            f'/api/schedule-assignments/{self.assignment.id}/swap/',
            {'target_assignment_id': target.id, 'force': True},
            format='json',
        )
        self.assertEqual(completed.status_code, 200)
        self.assignment.refresh_from_db()
        target.refresh_from_db()
        self.assertEqual(self.assignment.physician, self.requester)
        self.assertEqual(target.physician, self.owner)
        self.assertTrue(ShiftTrade.objects.filter(
            offered_assignment=target,
            requested_assignment=self.assignment,
            status=ShiftTrade.Status.APPROVED,
            reviewed_by=self.scheduler_user,
        ).exists())

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

    def test_published_pickup_is_allowed_at_contract_excluded_facility(self):
        excluded_facility = Facility.objects.create(
            name='Excluded Hospital', short_name='EX',
        )
        excluded_template = ShiftTemplate.objects.create(
            facility=excluded_facility, name='Excluded Day',
            start_time=time(7), end_time=time(16),
            active_days_of_week=['Wednesday'], weekend_days=[],
            default_staffing_count=1,
        )
        excluded_instance = ScheduleShiftInstance.objects.create(
            schedule_version=self.version, schedule_block=self.block,
            date=date(2026, 9, 2), shift_template=excluded_template,
            facility=excluded_facility,
            start_datetime=timezone.make_aware(datetime(2026, 9, 2, 7)),
            end_datetime=timezone.make_aware(datetime(2026, 9, 2, 16)),
            status=ScheduleShiftInstance.Status.ASSIGNED,
        )
        excluded_assignment = ScheduleShiftAssignment.objects.create(
            shift_instance=excluded_instance, physician=self.owner,
            assignment_source=(
                ScheduleShiftAssignment.AssignmentSource.MANUAL
            ),
            optimizer_run=self.run,
        )
        ShiftPosting.objects.create(
            assignment=excluded_assignment, posted_by=self.owner_user,
            mode=ShiftPosting.Mode.PICKUP,
        )
        ShiftTradePolicy.objects.create(
            pk=1, require_scheduler_approval=False,
        )

        self.client.force_authenticate(self.requester_user)
        created = self.client.post('/api/shift-trades/', {
            'target_assignment_id': excluded_assignment.id,
        }, format='json')
        self.assertEqual(created.status_code, 201)

        self.client.force_authenticate(self.owner_user)
        accepted = self.client.post(
            f"/api/shift-trades/{created.json()['id']}/accept/", {},
            format='json',
        )
        self.assertEqual(accepted.status_code, 200)
        excluded_assignment.refresh_from_db()
        self.assertEqual(excluded_assignment.physician, self.requester)

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

    def test_trade_options_include_contract_excluded_facilities(self):
        excluded_facility = Facility.objects.create(
            name='Excluded Hospital', short_name='EX',
        )
        excluded_template = ShiftTemplate.objects.create(
            facility=excluded_facility, name='Excluded Day',
            start_time=time(7), end_time=time(16),
            active_days_of_week=['Wednesday'], weekend_days=[],
            default_staffing_count=1,
        )
        target_instance = ScheduleShiftInstance.objects.create(
            schedule_version=self.version, schedule_block=self.block,
            date=date(2026, 9, 2), shift_template=excluded_template,
            facility=excluded_facility,
            start_datetime=timezone.make_aware(datetime(2026, 9, 2, 7)),
            end_datetime=timezone.make_aware(datetime(2026, 9, 2, 16)),
            status=ScheduleShiftInstance.Status.ASSIGNED,
        )
        target = ScheduleShiftAssignment.objects.create(
            shift_instance=target_instance, physician=self.requester,
            assignment_source=(
                ScheduleShiftAssignment.AssignmentSource.MANUAL
            ),
            optimizer_run=self.run,
        )

        self.client.force_authenticate(self.owner_user)
        options = self.client.get(
            f'/api/schedule-assignments/{self.assignment.id}/trade-options/'
        )

        self.assertEqual(options.status_code, 200)
        self.assertEqual([option['id'] for option in options.json()], [target.id])

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

    def test_scheduler_manages_published_date_comments_and_users_can_view_them(self):
        comment_date = '2026-09-21'
        self.client.force_authenticate(self.owner_user)
        denied = self.client.post('/api/published-schedule-comments/', {
            'date': comment_date,
            'title': 'Department meeting',
            'details': 'Conference room at 8:00 AM.',
        }, format='json')
        self.assertEqual(denied.status_code, 403)

        self.client.force_authenticate(self.scheduler_user)
        created = self.client.post('/api/published-schedule-comments/', {
            'date': comment_date,
            'title': 'Department meeting',
            'details': 'Conference room at 8:00 AM.',
        }, format='json')
        self.assertEqual(created.status_code, 200)
        self.assertEqual(ScheduleDateComment.objects.count(), 1)

        updated = self.client.post('/api/published-schedule-comments/', {
            'date': comment_date,
            'title': 'Meeting moved',
            'details': 'Conference room at 9:00 AM.',
        }, format='json')
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(ScheduleDateComment.objects.count(), 1)

        self.client.force_authenticate(self.owner_user)
        visible = self.client.get('/api/published-schedule-comments/')
        self.assertEqual(visible.status_code, 200)
        self.assertEqual(visible.json()[0]['title'], 'Meeting moved')

        self.client.force_authenticate(self.scheduler_user)
        deleted = self.client.delete(
            f'/api/published-schedule-comments/{comment_date}/',
        )
        self.assertEqual(deleted.status_code, 204)
        self.assertFalse(ScheduleDateComment.objects.exists())

    def test_recurring_comment_without_end_appears_only_as_schedules_publish(self):
        self.client.force_authenticate(self.scheduler_user)
        created = self.client.post('/api/published-schedule-comments/', {
            'date': '2026-09-02',
            'title': 'Operations meeting',
            'details': 'Main conference room.',
            'recurrence_type': 'WEEKLY',
            'interval': 3,
            'end_type': 'NEVER',
        }, format='json')
        self.assertEqual(created.status_code, 201)
        series_id = created.json()['series_id']
        self.assertTrue(ScheduleCommentSeries.objects.filter(id=series_id).exists())

        september = self.client.get('/api/published-schedule-comments/').json()
        self.assertEqual(
            [row['date'] for row in september],
            ['2026-09-02', '2026-09-23'],
        )

        October_block = ScheduleBlock.objects.create(
            start_date=date(2026, 10, 1),
            end_date=date(2026, 10, 31),
            request_open_datetime=timezone.now(),
            request_close_datetime=timezone.now(),
            build_status=ScheduleBlock.BuildStatus.ARCHIVE,
            published_at=timezone.now(),
        )
        october = self.client.get('/api/published-schedule-comments/').json()
        self.assertIn('2026-10-14', [row['date'] for row in october])

        changed = self.client.patch(
            f'/api/published-schedule-comment-series/{series_id}/occurrences/2026-09-23/',
            {
                'scope': 'THIS',
                'title': 'Meeting moved',
                'details': 'Use conference room B.',
            },
            format='json',
        )
        self.assertEqual(changed.status_code, 200)
        self.assertTrue(ScheduleCommentSeriesException.objects.filter(
            series_id=series_id,
            date=date(2026, 9, 23),
            title='Meeting moved',
        ).exists())

        removed = self.client.delete(
            f'/api/published-schedule-comment-series/{series_id}/occurrences/2026-09-23/',
            {'scope': 'THIS'},
            format='json',
        )
        self.assertEqual(removed.status_code, 204)
        dates = [
            row['date']
            for row in self.client.get('/api/published-schedule-comments/').json()
        ]
        self.assertNotIn('2026-09-23', dates)

    def test_recurring_comment_can_change_this_and_future_occurrences(self):
        self.client.force_authenticate(self.scheduler_user)
        created = self.client.post('/api/published-schedule-comments/', {
            'date': '2026-09-02',
            'title': 'Weekly huddle',
            'details': '',
            'recurrence_type': 'WEEKLY',
            'interval': 1,
            'end_type': 'NEVER',
        }, format='json')
        self.assertEqual(created.status_code, 201)
        original_series_id = created.json()['series_id']

        changed = self.client.patch(
            f'/api/published-schedule-comment-series/{original_series_id}/occurrences/2026-09-16/',
            {
                'scope': 'FUTURE',
                'title': 'Clinical huddle',
                'details': 'New agenda begins this week.',
            },
            format='json',
        )
        self.assertEqual(changed.status_code, 200)
        self.assertNotEqual(changed.json()['series_id'], original_series_id)
        comments = {
            row['date']: row
            for row in self.client.get('/api/published-schedule-comments/').json()
        }
        self.assertEqual(comments['2026-09-09']['title'], 'Weekly huddle')
        self.assertEqual(comments['2026-09-16']['title'], 'Clinical huddle')
        self.assertEqual(comments['2026-09-23']['title'], 'Clinical huddle')

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
