from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [('scheduling', '0018_score_staleness')]

    operations = [
        migrations.AddField(
            model_name='optimizerrun', name='copied_from_run',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='copies', to='scheduling.optimizerrun'),
        ),
        migrations.AddField(
            model_name='optimizerrun', name='run_kind',
            field=models.CharField(default='OPTIMIZER', max_length=20),
        ),
        migrations.AddField(
            model_name='optimizerrun', name='locked_open_shift_instance_ids',
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.RemoveConstraint(
            model_name='scheduleshiftassignment', name='unique_manual_physician_per_schedule_shift_instance',
        ),
        migrations.AddConstraint(
            model_name='scheduleshiftassignment',
            constraint=models.UniqueConstraint(condition=models.Q(assignment_source='MANUAL', optimizer_run__isnull=True), fields=('shift_instance', 'physician'), name='unique_legacy_manual_physician_per_shift_instance'),
        ),
        migrations.AddConstraint(
            model_name='scheduleshiftassignment',
            constraint=models.UniqueConstraint(condition=models.Q(assignment_source='MANUAL', optimizer_run__isnull=False), fields=('shift_instance', 'physician', 'optimizer_run'), name='unique_manual_run_physician_per_shift_instance'),
        ),
    ]
