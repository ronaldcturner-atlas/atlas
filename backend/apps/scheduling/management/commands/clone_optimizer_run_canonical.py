from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.scheduling.models import (
    OptimizerRun,
    ScheduleShiftAssignment,
    ScheduleVersion,
)
from apps.scheduling.optimizer import (
    build_violation_report,
    canonical_assignment_snapshot,
)
from apps.scheduling.run_state import visible_assignment_filter


def clone_optimizer_run_canonical(*, version, source_run):
    """Persist an isolated, scored canonical copy without changing its source."""
    with transaction.atomic():
        locked_version = (
            ScheduleVersion.objects.select_for_update()
            .select_related('schedule_block', 'domain')
            .get(id=version.id)
        )
        locked_source = (
            OptimizerRun.objects.select_for_update()
            .get(
                id=source_run.id,
                schedule_version=locked_version,
                status=OptimizerRun.Status.COMPLETED,
            )
        )
        instances = list(
            locked_version.shift_instances
            .filter(
                date__gte=locked_version.schedule_block.start_date,
                date__lte=locked_version.schedule_block.end_date,
            )
            .select_related('facility', 'shift_template')
            .order_by('date', 'id')
        )
        source_owned_assignments = list(
            ScheduleShiftAssignment.objects.filter(
                optimizer_run=locked_source,
                shift_instance__schedule_version=locked_version,
                shift_instance__date__gte=locked_version.schedule_block.start_date,
                shift_instance__date__lte=locked_version.schedule_block.end_date,
            )
            .select_related('shift_instance', 'physician')
            .order_by('id')
        )
        source_owned_ids = {row.id for row in source_owned_assignments}
        visible_assignment_ids = set(
            ScheduleShiftAssignment.objects.filter(
                visible_assignment_filter(locked_source),
                shift_instance__schedule_version=locked_version,
                shift_instance__date__gte=locked_version.schedule_block.start_date,
                shift_instance__date__lte=locked_version.schedule_block.end_date,
            ).values_list('id', flat=True)
        )
        visible_only_ids = visible_assignment_ids - source_owned_ids
        canonical_assignments, normalization = canonical_assignment_snapshot(
            source_owned_assignments,
            instances,
            selected_run=locked_source,
        )

        latest_run_number = (
            locked_version.optimizer_runs.order_by('-run_number')
            .values_list('run_number', flat=True)
            .first()
            or 0
        )
        cloned_run = OptimizerRun.objects.create(
            schedule_version=locked_version,
            run_number=latest_run_number + 1,
            created_by=locked_source.created_by,
            status=OptimizerRun.Status.COMPLETED,
            seed=locked_source.seed,
            initial_score=None,
            final_score=None,
            score_breakdown={},
            optimizer_summary={},
            optimizer_debug={},
            notes=f'Canonical benchmark source cloned from Run {locked_source.run_number}.',
            is_active=False,
            score_is_stale=False,
            copied_from_run=locked_source,
            run_kind='COPY',
            locked_open_shift_instance_ids=list(
                locked_source.locked_open_shift_instance_ids or []
            ),
            start_mode=locked_source.start_mode,
        )
        ScheduleShiftAssignment.objects.bulk_create([
            ScheduleShiftAssignment(
                shift_instance_id=row.shift_instance_id,
                physician_id=row.physician_id,
                created_by=row.created_by,
                assignment_source=row.assignment_source,
                optimizer_run=cloned_run,
                is_locked=row.is_locked,
            )
            for row in canonical_assignments
        ])

        report = build_violation_report(locked_version, optimizer_run=cloned_run)
        canonical_score = Decimal(str(report['total_score']))
        debug = {
            **report['debug'],
            'score_audit': report['score_audit'],
            'canonical_clone': {
                'source_run_id': locked_source.id,
                'source_owned_assignment_count': len(source_owned_assignments),
                'visible_assignment_count_before': len(visible_assignment_ids),
                'canonical_assignment_count': len(canonical_assignments),
                'duplicate_rows_excluded': normalization['duplicate_rows_discarded'],
                'over_capacity_rows_excluded': normalization['excess_rows_discarded'],
                'visible_only_rows_excluded': len(visible_only_ids),
                'discarded_assignment_ids': normalization['discarded_assignment_ids'],
                'visible_only_assignment_ids': sorted(visible_only_ids),
            },
        }
        summary = {
            'optimizer_run_id': cloned_run.id,
            'optimizer_run_number': cloned_run.run_number,
            'total_score': report['total_score'],
            'final_score': report['total_score'],
            'score_breakdown': report['score_breakdown'],
            'debug': debug,
        }
        cloned_run.initial_score = canonical_score
        cloned_run.final_score = canonical_score
        cloned_run.score_breakdown = report['score_breakdown']
        cloned_run.optimizer_summary = summary
        cloned_run.optimizer_debug = debug
        cloned_run.save(update_fields=[
            'initial_score', 'final_score', 'score_breakdown',
            'optimizer_summary', 'optimizer_debug',
        ])

    return cloned_run, {
        'source_run_id': source_run.id,
        'new_run_id': cloned_run.id,
        'source_stored_score': (
            float(source_run.final_score) if source_run.final_score is not None else None
        ),
        'source_recomputed_canonical_score': float(cloned_run.final_score),
        'new_run_score': float(cloned_run.final_score),
        'assignment_count_before': len(visible_assignment_ids),
        'source_owned_assignment_count': len(source_owned_assignments),
        'assignment_count_after': len(canonical_assignments),
        'duplicate_rows_excluded': normalization['duplicate_rows_discarded'],
        'over_capacity_rows_excluded': normalization['excess_rows_discarded'],
        'visible_only_rows_excluded': len(visible_only_ids),
    }


class Command(BaseCommand):
    help = 'Create a scored, isolated canonical copy of a completed optimizer run.'

    def add_arguments(self, parser):
        parser.add_argument('--schedule-block-id', type=int, required=True)
        parser.add_argument('--domain', required=True)
        parser.add_argument('--source-run-id', type=int, required=True)

    def handle(self, *args, **options):
        versions = ScheduleVersion.objects.select_related(
            'schedule_block', 'domain',
        ).filter(
            schedule_block_id=options['schedule_block_id'],
            domain__name__iexact=options['domain'],
            status=ScheduleVersion.Status.BUILD,
        )
        if versions.count() != 1:
            raise CommandError(
                'Expected exactly one BUILD Schedule Version for the block and domain.'
            )
        version = versions.get()
        source_run = OptimizerRun.objects.filter(
            id=options['source_run_id'],
            schedule_version=version,
            status=OptimizerRun.Status.COMPLETED,
        ).first()
        if source_run is None:
            raise CommandError(
                '--source-run-id must identify a completed run in the selected Schedule Version.'
            )

        _cloned_run, result = clone_optimizer_run_canonical(
            version=version,
            source_run=source_run,
        )
        for key, value in result.items():
            self.stdout.write(f'{key}: {value}')
