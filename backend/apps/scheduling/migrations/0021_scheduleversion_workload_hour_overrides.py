from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('scheduling', '0020_optimizer_run_start_mode')]

    operations = [
        migrations.AddField(
            model_name='scheduleversion',
            name='workload_hour_overrides',
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
