from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('scheduling', '0012_mark_existing_assignments_optimizer_source'),
    ]

    operations = [
        migrations.AddField(
            model_name='scheduleversion',
            name='optimizer_summary',
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
