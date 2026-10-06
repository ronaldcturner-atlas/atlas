from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('scheduling', '0044_scheduleblock_domain'),
    ]

    operations = [
        migrations.AddField(
            model_name='scheduleblock',
            name='preview_optimizer_run',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='preview_schedule_blocks',
                to='scheduling.optimizerrun',
            ),
        ),
    ]
