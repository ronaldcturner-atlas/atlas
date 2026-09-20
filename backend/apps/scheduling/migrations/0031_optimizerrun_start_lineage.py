from django.db import migrations, models
import django.db.models.deletion


def backfill_start_lineage(apps, schema_editor):
    OptimizerRun = apps.get_model('scheduling', 'OptimizerRun')
    current_runs = OptimizerRun.objects.filter(start_mode='CURRENT_SCHEDULE')
    for run in current_runs.iterator():
        debug = run.optimizer_debug if isinstance(run.optimizer_debug, dict) else {}
        source_id = debug.get('source_optimizer_run_id')
        if not source_id:
            summary = run.optimizer_summary if isinstance(run.optimizer_summary, dict) else {}
            summary_debug = summary.get('debug') if isinstance(summary.get('debug'), dict) else {}
            source_id = summary_debug.get('source_optimizer_run_id')
        source = OptimizerRun.objects.filter(
            id=source_id,
            schedule_version_id=run.schedule_version_id,
        ).first()
        if source is None:
            continue
        run.started_from_run_id = source.id
        run.started_from_run_number = source.run_number
        run.save(update_fields=['started_from_run', 'started_from_run_number'])


class Migration(migrations.Migration):

    dependencies = [
        ('scheduling', '0030_optimizerrun_max_runtime_seconds'),
    ]

    operations = [
        migrations.AddField(
            model_name='optimizerrun',
            name='started_from_run',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='derived_runs',
                to='scheduling.optimizerrun',
            ),
        ),
        migrations.AddField(
            model_name='optimizerrun',
            name='started_from_run_number',
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.RunPython(backfill_start_lineage, migrations.RunPython.noop),
    ]
