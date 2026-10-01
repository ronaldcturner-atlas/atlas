"""Rollback-only equivalence gate for the Atlas Fresh Fill boundary."""

import hashlib
import json

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Max

from apps.scheduling.initial_schedule import construct_complete_initial_schedule
from apps.scheduling.models import (
    OptimizerRun,
    ScheduleBlock,
    ScheduleShiftAssignment,
    ScheduleVersion,
)
from apps.scheduling.optimizer import optimize_schedule_version
from apps.scheduling.optimizer_v2_runner import V2_RUN_KIND


VALIDITY_KEYS = (
    'unfilled_shift_count',
    'final_overlap_violations',
    'final_rest_violations',
    'final_duplicate_violations',
    'final_overstaffed_violations',
    'final_inactive_physician_violations',
    'final_facility_ineligible_violations',
)


def _assignment_snapshot(run):
    return list(
        ScheduleShiftAssignment.objects.filter(optimizer_run=run)
        .order_by('shift_instance_id', 'physician_id', 'assignment_source', 'id')
        .values_list(
            'shift_instance_id',
            'physician_id',
            'assignment_source',
            'is_locked',
        )
    )


def _result_snapshot(summary, assignments):
    return {
        'assignments': assignments,
        'final_score': summary.get('final_score'),
        'score_breakdown': summary.get('score_breakdown'),
        'validity': {
            key: int(summary.get(key) or 0)
            for key in VALIDITY_KEYS
        },
    }


def _snapshot_hash(snapshot):
    payload = json.dumps(
        snapshot,
        sort_keys=True,
        separators=(',', ':'),
        default=str,
    ).encode('utf-8')
    return hashlib.sha256(payload).hexdigest()


def _snapshot_differences(baseline, boundary):
    differences = []
    if baseline['assignments'] != boundary['assignments']:
        baseline_pairs = set(map(tuple, baseline['assignments']))
        boundary_pairs = set(map(tuple, boundary['assignments']))
        differences.append({
            'field': 'assignments',
            'baseline_only': sorted(baseline_pairs - boundary_pairs)[:20],
            'boundary_only': sorted(boundary_pairs - baseline_pairs)[:20],
            'baseline_count': len(baseline['assignments']),
            'boundary_count': len(boundary['assignments']),
        })
    for field in ('final_score', 'score_breakdown', 'validity'):
        if baseline[field] != boundary[field]:
            differences.append({
                'field': field,
                'baseline': baseline[field],
                'boundary': boundary[field],
            })
    return differences


class Command(BaseCommand):
    help = (
        'Compare the legacy Fresh Fill constructor with the engine-neutral '
        'boundary using the same seed, then roll back every temporary row.'
    )

    def add_arguments(self, parser):
        parser.add_argument('schedule_version_id', type=int)
        parser.add_argument('--seed', type=int, default=9183)
        parser.add_argument('--max-runtime-seconds', type=int, default=300)
        parser.add_argument(
            '--expected-hash',
            help=(
                'Require both snapshots to match a previously captured '
                'reference fingerprint.'
            ),
        )

    def handle(self, *args, **options):
        if OptimizerRun.objects.filter(status=OptimizerRun.Status.RUNNING).exists():
            raise CommandError(
                'Equivalence verification requires both optimizer slots to be idle.'
            )

        schedule_version = (
            ScheduleVersion.objects.select_related('schedule_block')
            .filter(id=options['schedule_version_id'])
            .first()
        )
        if schedule_version is None:
            raise CommandError('Schedule Version does not exist.')
        if (
            schedule_version.status != ScheduleVersion.Status.BUILD
            or schedule_version.schedule_block.build_status
            != ScheduleBlock.BuildStatus.BUILD
        ):
            raise CommandError(
                'Equivalence verification requires a BUILD Schedule Version '
                'inside a BUILD Schedule Block.'
            )
        runtime_seconds = int(options['max_runtime_seconds'])
        if not 1 <= runtime_seconds <= 4 * 60 * 60:
            raise CommandError('Maximum runtime must be between 1 and 14400 seconds.')

        result = None
        differences = None
        with transaction.atomic():
            next_run_number = (
                OptimizerRun.objects.filter(schedule_version=schedule_version)
                .aggregate(value=Max('run_number'))['value']
                or 0
            ) + 1
            run_defaults = {
                'schedule_version': schedule_version,
                'status': OptimizerRun.Status.RUNNING,
                'seed': options['seed'],
                'start_mode': OptimizerRun.StartMode.FRESH_FILL,
                'run_kind': V2_RUN_KIND,
                'max_runtime_seconds': runtime_seconds,
                'optimization_focus': OptimizerRun.OptimizationFocus.STANDARD,
            }
            baseline_run = OptimizerRun.objects.create(
                run_number=next_run_number,
                **run_defaults,
            )
            boundary_run = OptimizerRun.objects.create(
                run_number=next_run_number + 1,
                **run_defaults,
            )

            baseline_summary = optimize_schedule_version(
                schedule_version,
                optimizer_run=baseline_run,
                seed=baseline_run.seed,
                start_mode=OptimizerRun.StartMode.FRESH_FILL,
                source_run=None,
                max_runtime_seconds=baseline_run.max_runtime_seconds,
                optimization_focus=baseline_run.optimization_focus,
                adaptive_runtime=True,
                isolated_run=True,
                finalize_run=False,
                construction_only=True,
            )
            boundary_summary = construct_complete_initial_schedule(
                schedule_version,
                optimizer_run=boundary_run,
            )
            baseline = _result_snapshot(
                baseline_summary,
                _assignment_snapshot(baseline_run),
            )
            boundary = _result_snapshot(
                boundary_summary,
                _assignment_snapshot(boundary_run),
            )
            differences = _snapshot_differences(baseline, boundary)
            result = {
                'equivalent': not differences,
                'schedule_version_id': schedule_version.id,
                'seed': options['seed'],
                'assignment_count': len(boundary['assignments']),
                'final_score': boundary['final_score'],
                'validity': boundary['validity'],
                'baseline_hash': _snapshot_hash(baseline),
                'boundary_hash': _snapshot_hash(boundary),
                'differences': differences,
                'persistence_mode': 'rollback',
            }
            expected_hash = options.get('expected_hash')
            if expected_hash and (
                result['baseline_hash'] != expected_hash
                or result['boundary_hash'] != expected_hash
            ):
                differences.append({
                    'field': 'expected_hash',
                    'expected': expected_hash,
                    'baseline': result['baseline_hash'],
                    'boundary': result['boundary_hash'],
                })
                result['equivalent'] = False
                result['differences'] = differences
            transaction.set_rollback(True)

        if differences:
            raise CommandError(json.dumps(result, sort_keys=True, default=str))
        self.stdout.write(json.dumps(result, sort_keys=True, default=str))
