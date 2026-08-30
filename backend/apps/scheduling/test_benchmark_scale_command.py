from datetime import date
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from apps.domains.models import Domain
from .management.commands.benchmark_optimizer_scale import Command, SCALE_PROFILES, _end_of_profile
from .models import OptimizerRun, ScheduleBlock, ScheduleShiftInstance, ScheduleVersion


class OptimizerScaleBenchmarkCommandTests(TestCase):
    def test_mixed_profile_preserves_fixed_nights_and_contracts(self):
        result = Command()._run_profile(
            SCALE_PROFILES[1], seed=73001, time_limit=5,
            shifts_per_day=2, staffing_per_shift=1, scenario='mixed',
        )
        self.assertEqual(result['fixed_assignments'], 2)
        self.assertEqual(result['requests'], 10)
        self.assertTrue(all(result['quality_gates'].values()), result['quality_gates'])

    def test_failed_quality_gate_raises_only_after_rollback(self):
        before = ScheduleVersion.objects.count()
        with self.assertRaisesMessage(CommandError, 'quality gates failed'):
            call_command(
                'benchmark_optimizer_scale', profile=['1m-5p'],
                time_limit_seconds=0.000000001, fail_on_gate=True, stdout=StringIO(),
            )
        self.assertEqual(ScheduleVersion.objects.count(), before)
        self.assertFalse(Domain.objects.filter(name='Scale 1m-5p').exists())

    def test_profiles_cover_required_product_scale(self):
        self.assertEqual(
            [(row['months'], row['physicians']) for row in SCALE_PROFILES],
            [(1, 5), (2, 10), (3, 25), (6, 50), (12, 100), (12, 150)],
        )
        self.assertEqual(_end_of_profile(date(2032, 1, 1), 1), date(2032, 1, 31))
        self.assertEqual(_end_of_profile(date(2032, 1, 1), 12), date(2032, 12, 31))

    def test_small_profile_runs_real_optimizer_and_rolls_back_fixture(self):
        counts_before = {
            'domains': Domain.objects.count(),
            'blocks': ScheduleBlock.objects.count(),
            'versions': ScheduleVersion.objects.count(),
            'instances': ScheduleShiftInstance.objects.count(),
            'runs': OptimizerRun.objects.count(),
        }
        stdout = StringIO()

        call_command(
            'benchmark_optimizer_scale',
            profile=['1m-5p'],
            time_limit_seconds=1,
            seed=73001,
            shifts_per_day=1,
            fail_on_gate=True,
            stdout=stdout,
        )

        output = stdout.getvalue()
        self.assertIn('1m-5p: COMPLETED', output)
        self.assertIn('coverage=31/31', output)
        self.assertIn('no synthetic data was retained', output)
        self.assertEqual(Domain.objects.count(), counts_before['domains'])
        self.assertEqual(ScheduleBlock.objects.count(), counts_before['blocks'])
        self.assertEqual(ScheduleVersion.objects.count(), counts_before['versions'])
        self.assertEqual(ScheduleShiftInstance.objects.count(), counts_before['instances'])
        self.assertEqual(OptimizerRun.objects.count(), counts_before['runs'])
