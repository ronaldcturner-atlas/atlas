from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def _decimal_or_none(value):
    if value in (None, ''):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def seed_optimizer_runs(apps, schema_editor):
    OptimizerRun = apps.get_model('scheduling', 'OptimizerRun')
    ScheduleShiftAssignment = apps.get_model('scheduling', 'ScheduleShiftAssignment')
    ScheduleVersion = apps.get_model('scheduling', 'ScheduleVersion')

    optimizer_versions = set(
        ScheduleShiftAssignment.objects.filter(assignment_source='OPTIMIZER')
        .values_list('shift_instance__schedule_version_id', flat=True)
    )
    summary_versions = set(
        ScheduleVersion.objects.exclude(optimizer_summary={}).values_list('id', flat=True)
    )

    for version in ScheduleVersion.objects.filter(id__in=optimizer_versions | summary_versions):
        summary = version.optimizer_summary if isinstance(version.optimizer_summary, dict) else {}
        optimizer_run = OptimizerRun.objects.create(
            schedule_version=version,
            run_number=1,
            status='COMPLETED',
            initial_score=_decimal_or_none(summary.get('initial_score')),
            final_score=_decimal_or_none(summary.get('final_score') or summary.get('total_score')),
            score_breakdown=summary.get('score_breakdown') or {},
            optimizer_summary=summary,
            optimizer_debug=summary.get('debug') or {},
            is_active=True,
        )
        ScheduleShiftAssignment.objects.filter(
            shift_instance__schedule_version=version,
            assignment_source='OPTIMIZER',
            optimizer_run__isnull=True,
        ).update(optimizer_run=optimizer_run)


def unseed_optimizer_runs(apps, schema_editor):
    ScheduleShiftAssignment = apps.get_model('scheduling', 'ScheduleShiftAssignment')
    OptimizerRun = apps.get_model('scheduling', 'OptimizerRun')
    ScheduleShiftAssignment.objects.filter(assignment_source='OPTIMIZER').update(optimizer_run=None)
    OptimizerRun.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('scheduling', '0013_scheduleversion_optimizer_summary'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='OptimizerRun',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('run_number', models.PositiveIntegerField()),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('status', models.CharField(choices=[('RUNNING', 'Running'), ('COMPLETED', 'Completed'), ('FAILED', 'Failed')], default='RUNNING', max_length=20)),
                ('initial_score', models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True)),
                ('final_score', models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True)),
                ('score_breakdown', models.JSONField(blank=True, default=dict)),
                ('optimizer_summary', models.JSONField(blank=True, default=dict)),
                ('optimizer_debug', models.JSONField(blank=True, default=dict)),
                ('notes', models.TextField(blank=True)),
                ('is_active', models.BooleanField(default=False)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='created_optimizer_runs', to=settings.AUTH_USER_MODEL)),
                ('schedule_version', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='optimizer_runs', to='scheduling.scheduleversion')),
            ],
            options={
                'ordering': ['-run_number', '-created_at'],
            },
        ),
        migrations.AddField(
            model_name='scheduleshiftassignment',
            name='optimizer_run',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='assignments', to='scheduling.optimizerrun'),
        ),
        migrations.RemoveConstraint(
            model_name='scheduleshiftassignment',
            name='unique_physician_per_schedule_shift_instance',
        ),
        migrations.AddConstraint(
            model_name='optimizerrun',
            constraint=models.UniqueConstraint(fields=('schedule_version', 'run_number'), name='unique_optimizer_run_number_per_schedule_version'),
        ),
        migrations.AddConstraint(
            model_name='optimizerrun',
            constraint=models.UniqueConstraint(condition=models.Q(is_active=True), fields=('schedule_version',), name='unique_active_optimizer_run_per_schedule_version'),
        ),
        migrations.RunPython(seed_optimizer_runs, unseed_optimizer_runs),
        migrations.AddConstraint(
            model_name='scheduleshiftassignment',
            constraint=models.UniqueConstraint(condition=models.Q(assignment_source='MANUAL'), fields=('shift_instance', 'physician'), name='unique_manual_physician_per_schedule_shift_instance'),
        ),
        migrations.AddConstraint(
            model_name='scheduleshiftassignment',
            constraint=models.UniqueConstraint(condition=models.Q(assignment_source='OPTIMIZER', optimizer_run__isnull=False), fields=('shift_instance', 'physician', 'optimizer_run'), name='unique_optimizer_run_physician_per_shift_instance'),
        ),
    ]
