from decimal import Decimal

from django.db import migrations, models


def _runtime_seconds(run):
    if run.status != 'COMPLETED' or run.run_kind == 'COPY':
        return 0.0
    return max(float((run.optimizer_summary or {}).get('runtime_seconds') or 0.0), 0.0)


def _historical_runtime_since_reduction(run, runtime_seconds):
    search = (run.optimizer_summary or {}).get('v2_search') or {}
    checkpoint = search.get('best_authoritative_checkpoint') or {}
    best_elapsed = checkpoint.get('elapsed_seconds')
    if best_elapsed is None:
        best_elapsed = (
            (search.get('production_telemetry') or {})
            .get('best_verified_seconds')
        )
    if best_elapsed is None:
        return 0.0
    search_wall = float(search.get('wall_seconds') or 0.0)
    after_reduction = max(search_wall - float(best_elapsed), 0.0)
    non_search = max(float(runtime_seconds) - search_wall, 0.0)
    bootstrap = float(
        ((run.optimizer_summary or {}).get('fresh_fill_bootstrap') or {})
        .get('runtime_seconds')
        or 0.0
    )
    return after_reduction + max(non_search - bootstrap, 0.0)


def backfill_optimizer_lineage(apps, schema_editor):
    OptimizerRun = apps.get_model('scheduling', 'OptimizerRun')
    for version_id in (
        OptimizerRun.objects.order_by()
        .values_list('schedule_version_id', flat=True)
        .distinct()
    ):
        runs = list(
            OptimizerRun.objects.filter(schedule_version_id=version_id)
            .order_by('run_number', 'id')
        )
        by_id = {}
        for run in runs:
            source = None
            if run.run_kind == 'COPY' and run.copied_from_run_id:
                source = by_id.get(run.copied_from_run_id)
            elif run.started_from_run_id:
                source = by_id.get(run.started_from_run_id)

            if source is None:
                root_run_number = (
                    run.run_number if run.start_mode == 'FRESH_FILL' else None
                )
                cumulative_runtime = 0.0
                runtime_since_reduction = 0.0
                exhaustion_count = 0
            else:
                root_run_number = source.lineage_fresh_fill_run_number
                if root_run_number is None and source.start_mode == 'FRESH_FILL':
                    root_run_number = source.run_number
                cumulative_runtime = float(
                    source.lineage_cumulative_runtime_seconds or 0.0
                )
                runtime_since_reduction = float(
                    source.lineage_runtime_since_penalty_reduction_seconds
                    or 0.0
                )
                exhaustion_count = int(
                    source.lineage_search_exhaustion_count or 0
                )

            own_runtime = _runtime_seconds(run)
            if run.run_kind != 'COPY':
                cumulative_runtime += own_runtime
                initial = run.initial_score
                final = run.completion_score or run.final_score
                penalty_reduced = bool(
                    initial is not None
                    and final is not None
                    and Decimal(str(final))
                    < Decimal(str(initial)) - Decimal('0.0001')
                )
                if penalty_reduced:
                    runtime_since_reduction = (
                        _historical_runtime_since_reduction(
                            run, own_runtime,
                        )
                    )
                elif run.optimization_focus != 'DISTRIBUTION':
                    runtime_since_reduction += own_runtime
                stopped_reason = (
                    (run.optimizer_summary or {}).get('stopped_reason')
                )
                exhaustion_count += int(
                    stopped_reason == 'productivity_exhausted'
                )

            run.lineage_fresh_fill_run_number = root_run_number
            run.lineage_cumulative_runtime_seconds = cumulative_runtime
            run.lineage_runtime_since_penalty_reduction_seconds = (
                runtime_since_reduction
            )
            run.lineage_search_exhaustion_count = exhaustion_count
            run.save(update_fields=[
                'lineage_fresh_fill_run_number',
                'lineage_cumulative_runtime_seconds',
                'lineage_runtime_since_penalty_reduction_seconds',
                'lineage_search_exhaustion_count',
            ])
            by_id[run.id] = run


class Migration(migrations.Migration):

    dependencies = [
        ('scheduling', '0062_schedule_request_groups'),
    ]

    operations = [
        migrations.AddField(
            model_name='optimizerrun',
            name='lineage_cumulative_runtime_seconds',
            field=models.FloatField(default=0.0),
        ),
        migrations.AddField(
            model_name='optimizerrun',
            name='lineage_fresh_fill_run_number',
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='optimizerrun',
            name='lineage_runtime_since_penalty_reduction_seconds',
            field=models.FloatField(default=0.0),
        ),
        migrations.AddField(
            model_name='optimizerrun',
            name='lineage_search_exhaustion_count',
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.RunPython(
            backfill_optimizer_lineage,
            migrations.RunPython.noop,
        ),
    ]
