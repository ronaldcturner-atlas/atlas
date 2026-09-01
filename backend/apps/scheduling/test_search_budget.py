from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from django.db import transaction, close_old_connections
from concurrent.futures import ThreadPoolExecutor
from rest_framework.test import APIRequestFactory, force_authenticate

from .search_budget import SearchBudget
from .management.commands.benchmark_optimizer_scale import Command, SCALE_PROFILES
from .models import ContractUserAssignment, OptimizerControl, OptimizerRun, ScheduleShiftAssignment
from .optimizer import optimize_schedule_version, build_violation_report
from .run_state import assignments_for_viewed_run
from . import api
from . import optimizer as optimizer_module


class SearchBudgetTests(SimpleTestCase):
    def test_coverage_renews_stall_until_complete_but_not_overall_limit(self):
        now = [0]
        budget = SearchBudget(clock=lambda: now[0], stall_seconds=10, total_seconds=30)
        budget.observe_coverage(0)
        now[0] = 9
        self.assertTrue(budget.observe_coverage(1))
        now[0] = 18
        self.assertFalse(budget.observe_coverage(1))
        self.assertFalse(budget.observe_coverage(0))
        self.assertIsNone(budget.reason())
        budget.observe_coverage(2)
        now[0] = 27
        budget.observe_coverage(3)
        now[0] = 30
        self.assertEqual(budget.reason(), 'overall_runtime_limit')

    def test_completion_switches_from_coverage_to_score_progress(self):
        now = [0]
        budget = SearchBudget(clock=lambda: now[0], stall_seconds=10)
        budget.observe_coverage(0)
        now[0] = 8
        budget.observe(100, valid=True)
        now[0] = 17
        self.assertFalse(budget.observe_coverage(1000))
        self.assertFalse(budget.observe(100, valid=True))
        now[0] = 18
        self.assertEqual(budget.reason(), 'stall_limit')

    def setUp(self):
        self.now = 0
        self.stop = False
        self.budget = SearchBudget(clock=lambda: self.now, stop_requested=lambda: self.stop)
        self.budget.observe(100, valid=True)

    def test_new_best_resets_stall_but_equal_worse_or_invalid_does_not(self):
        self.now = 50
        self.assertTrue(self.budget.observe(90, valid=True))
        self.now = 100
        for score, valid in [(90, True), (95, True), (80, False)]:
            self.assertFalse(self.budget.observe(score, valid=valid))
        self.assertIsNone(self.budget.reason())
        self.now = 110
        self.assertEqual(self.budget.reason(), 'stall_limit')

    def test_total_cap_cannot_be_reset_by_improvement(self):
        for second in range(50, 600, 50):
            self.now = second
            self.budget.observe(100 - second / 10, valid=True)
        self.now = 600
        self.assertEqual(self.budget.reason(), 'overall_runtime_limit')

    def test_stop_and_zero_finish_early(self):
        self.stop = True
        self.assertEqual(self.budget.reason(), 'user_stop')
        self.stop = False
        self.budget.observe(0, valid=True)
        self.assertEqual(self.budget.reason(), 'score_zero')


class SearchControlTests(TestCase):
    def assert_construction_uses_adaptive_budget(self, partial=False):
        source = self.source() if partial else None
        if source:
            ids = list(source.assignments.order_by('id').values_list('id', flat=True))
            source.assignments.filter(id__in=ids[1::2]).delete()
        def short_budget(**kwargs):
            return SearchBudget(**kwargs, stall_seconds=.5, total_seconds=1)
        with patch.object(optimizer_module, 'MAX_RUNTIME_SECONDS', 0), \
             patch.object(optimizer_module, 'SearchBudget', side_effect=short_budget):
            summary = optimize_schedule_version(self.version, seed=54, source_run=source,
                start_mode='CURRENT_SCHEDULE' if source else 'FRESH_FILL', adaptive_runtime=True)
        result = OptimizerRun.objects.get(pk=summary['optimizer_run_id'])
        self.assertEqual(summary['unfilled_shift_count'], 0)
        self.assertEqual(result.status, OptimizerRun.Status.COMPLETED)
        self.assertAlmostEqual(float(result.final_score),
            float(build_violation_report(self.version, result)['total_score']), places=2)

    def test_fresh_construction_uses_adaptive_budget_not_legacy_cap(self):
        self.assert_construction_uses_adaptive_budget()

    def test_partial_construction_uses_adaptive_budget_not_legacy_cap(self):
        self.assert_construction_uses_adaptive_budget(partial=True)

    def test_nonadaptive_construction_keeps_legacy_cap(self):
        with patch.object(optimizer_module, 'MAX_RUNTIME_SECONDS', 0):
            summary = optimize_schedule_version(self.version, seed=55)
        self.assertGreater(summary['unfilled_shift_count'], 0)
        self.assertTrue(summary['debug']['timed_out'])

    def setUp(self):
        self.version = Command()._build_fixture(SCALE_PROFILES[0], shifts_per_day=1)
        self.user = User.objects.create_user('search-owner', is_staff=True)
        self.factory = APIRequestFactory()

    def stop_request(self, token, user=None):
        request = self.factory.post('/', {'search_token': str(token)}, format='json')
        force_authenticate(request, user=user or self.user)
        return api.schedule_version_stop_optimizer(request, self.version.pk)

    def test_stop_is_scoped_to_owner_version_and_token(self):
        control = OptimizerControl.objects.create(token=uuid4(), schedule_version=self.version, created_by=self.user)
        other = User.objects.create_user('other-scheduler', is_staff=True)
        with patch('apps.scheduling.api._can_manage_build_workspace', return_value=True):
            self.assertEqual(self.stop_request(uuid4()).status_code, 409)
            self.assertEqual(self.stop_request(control.token, other).status_code, 409)
            self.assertEqual(self.stop_request(control.token).status_code, 200)
        control.refresh_from_db()
        self.assertTrue(control.stop_requested)

    def test_stop_rejects_unprivileged_user(self):
        with patch('apps.scheduling.api._can_manage_build_workspace', return_value=False):
            self.assertEqual(self.stop_request(uuid4()).status_code, 403)

    def test_api_cleans_control_on_failure_and_enables_cooperative_stop(self):
        request = self.factory.post('/', {'search_token': str(uuid4())}, format='json')
        request = api.schedule_version_run_optimizer.cls().initialize_request(request)
        request.user = self.user
        def fail(version, **kwargs):
            self.assertTrue(kwargs['adaptive_runtime'])
            OptimizerControl.objects.filter(schedule_version=version).update(stop_requested=True)
            self.assertTrue(kwargs['stop_requested']())
            raise ValueError('test failure')
        with patch('apps.scheduling.api.optimize_schedule_version', side_effect=fail):
            response = api._run_optimizer_response(request, self.version)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(OptimizerControl.objects.exists())

    def source(self, concentrated=False):
        source = OptimizerRun.objects.create(schedule_version=self.version, run_number=1,
                                            status=OptimizerRun.Status.COMPLETED, is_active=True)
        physicians = list(ContractUserAssignment.objects.filter(domain=self.version.domain).values_list('physician_id', flat=True))
        for index, instance in enumerate(self.version.shift_instances.order_by('date')):
            ScheduleShiftAssignment.objects.create(
                shift_instance=instance, physician_id=physicians[0 if concentrated else index % len(physicians)], optimizer_run=source,
                assignment_source=ScheduleShiftAssignment.AssignmentSource.OPTIMIZER,
                is_locked=index == 0)
        return source

    def test_stop_keeps_complete_source_and_locks(self):
        source = self.source()
        before = list(assignments_for_viewed_run(self.version, source).values_list('shift_instance_id', 'physician_id'))
        summary = optimize_schedule_version(self.version, source_run=source,
            start_mode=OptimizerRun.StartMode.CURRENT_SCHEDULE, seed=51,
            adaptive_runtime=True, stop_requested=lambda: True)
        result = OptimizerRun.objects.get(pk=summary['optimizer_run_id'])
        self.assertEqual(result.status, OptimizerRun.Status.COMPLETED)
        self.assertEqual(summary['debug']['adaptive_runtime']['stopped_reason'], 'user_stop')
        self.assertCountEqual(before, assignments_for_viewed_run(self.version, result).values_list('shift_instance_id', 'physician_id'))
        self.assertEqual(summary['unfilled_shift_count'], 0)

    def test_continuation_renews_repairs_and_returns_no_worse_than_source(self):
        source = self.source(concentrated=True)
        before = build_violation_report(self.version, source)['total_score']
        def short_budget(**kwargs):
            return SearchBudget(**kwargs, stall_seconds=1, total_seconds=2)
        with patch('apps.scheduling.optimizer.MAX_RUNTIME_SECONDS', 0), \
             patch('apps.scheduling.optimizer.SearchBudget', side_effect=short_budget):
            summary = optimize_schedule_version(self.version, source_run=source,
                start_mode=OptimizerRun.StartMode.CURRENT_SCHEDULE, seed=52, adaptive_runtime=True)
        self.assertLessEqual(summary['final_score'], before)
        self.assertGreater(summary['debug']['adaptive_runtime']['cycles'], 0)
        repair_stats = summary['debug']['adaptive_runtime']['repair_stats']
        self.assertTrue(repair_stats)
        for stats in repair_stats.values():
            self.assertGreaterEqual(stats['calls'], 1)
            self.assertGreaterEqual(stats['attempts'], stats['accepts'])
            self.assertGreaterEqual(stats['attempts'], stats['legal_candidates'])
            self.assertGreaterEqual(stats['legal_candidates'], stats['scored_candidates'])
            self.assertGreaterEqual(stats['runtime_seconds'], 0)
        self.assertEqual(summary['unfilled_shift_count'], 0)
        self.assertEqual(OptimizerRun.objects.get(pk=summary['optimizer_run_id']).status, OptimizerRun.Status.COMPLETED)

    def test_stop_after_improvement_keeps_that_improvement(self):
        source = self.source(concentrated=True)
        before = build_violation_report(self.version, source)['total_score']
        stopped = [False]
        real_repair = optimizer_module._repair_workload_transfers
        def repair(**kwargs):
            observe = kwargs['on_improvement']
            def accepted(state, scoring):
                observe(state, scoring)
                stopped[0] = True
            kwargs['on_improvement'] = accepted
            return real_repair(**kwargs)
        with patch.object(optimizer_module, 'MAX_RUNTIME_SECONDS', 0), \
             patch.object(optimizer_module, '_repair_workload_transfers', side_effect=repair):
            summary = optimize_schedule_version(self.version, source_run=source,
                start_mode=OptimizerRun.StartMode.CURRENT_SCHEDULE, seed=53,
                adaptive_runtime=True, stop_requested=lambda: stopped[0])
        self.assertTrue(stopped[0])
        self.assertLess(summary['final_score'], before)
        self.assertEqual(summary['debug']['adaptive_runtime']['stopped_reason'], 'user_stop')
        self.assertEqual(summary['unfilled_shift_count'], 0)


class ConcurrentStopTests(TransactionTestCase):
    def test_stop_record_can_update_while_schedule_transaction_is_locked(self):
        version = Command()._build_fixture(SCALE_PROFILES[0], shifts_per_day=1)
        user = User.objects.create_user('concurrent-search-owner')
        control = OptimizerControl.objects.create(token=uuid4(), schedule_version=version, created_by=user)
        def request_stop():
            close_old_connections()
            try:
                return OptimizerControl.objects.filter(token=control.token).update(stop_requested=True)
            finally:
                close_old_connections()
        pool = ThreadPoolExecutor(max_workers=1)
        try:
            with transaction.atomic():
                type(version).objects.select_for_update().get(pk=version.pk)
                self.assertEqual(pool.submit(request_stop).result(timeout=3), 1)
                self.assertTrue(OptimizerControl.objects.get(token=control.token).stop_requested)
        finally:
            pool.shutdown(wait=True)
