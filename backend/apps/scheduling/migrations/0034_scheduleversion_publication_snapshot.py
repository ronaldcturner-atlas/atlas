from django.db import migrations, models
import django.db.models.deletion


def backfill_published_runs(apps, schema_editor):
    ScheduleVersion = apps.get_model('scheduling', 'ScheduleVersion')
    OptimizerRun = apps.get_model('scheduling', 'OptimizerRun')

    for version in ScheduleVersion.objects.filter(
        schedule_block__published_at__isnull=False,
    ).iterator():
        published_run = (
            OptimizerRun.objects.filter(
                schedule_version_id=version.id,
                status='COMPLETED',
                is_active=True,
            ).order_by('-run_number').first()
            or OptimizerRun.objects.filter(
                schedule_version_id=version.id,
                status='COMPLETED',
            ).order_by('-run_number').first()
        )
        version.published_optimizer_run_id = getattr(published_run, 'id', None)
        version.score_is_stale = False
        version.save(update_fields=['published_optimizer_run', 'score_is_stale'])
        if published_run is not None and published_run.score_is_stale:
            published_run.score_is_stale = False
            published_run.save(update_fields=['score_is_stale'])


class Migration(migrations.Migration):

    dependencies = [
        ('scheduling', '0033_optimizercontrol_parallel_runs'),
    ]

    operations = [
        migrations.AddField(
            model_name='scheduleversion',
            name='published_optimizer_run',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='published_schedule_versions',
                to='scheduling.optimizerrun',
            ),
        ),
        migrations.AddField(
            model_name='scheduleversion',
            name='published_violation_report',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.RunPython(backfill_published_runs, migrations.RunPython.noop),
    ]
