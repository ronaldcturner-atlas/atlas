from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('scheduling', '0032_optimizercontrol_live_progress'),
    ]

    operations = [
        migrations.AlterField(
            model_name='optimizercontrol',
            name='schedule_version',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name='optimizer_controls',
                to='scheduling.scheduleversion',
            ),
        ),
        migrations.AddIndex(
            model_name='optimizercontrol',
            index=models.Index(
                fields=['schedule_version', 'started_at', 'created_at'],
                name='sched_opt_control_claim_idx',
            ),
        ),
    ]
