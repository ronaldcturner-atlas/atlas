from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('scheduling', '0047_shifttrade_domain'),
    ]

    operations = [
        migrations.AddField(
            model_name='scheduleversion',
            name='original_published_snapshot',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AlterField(
            model_name='optimizerrun',
            name='start_mode',
            field=models.CharField(
                choices=[
                    ('CURRENT_SCHEDULE', 'Current schedule'),
                    (
                        'ORIGINAL_PUBLISHED_SCHEDULE',
                        'Original published schedule',
                    ),
                    ('FRESH_FILL', 'Fresh fill'),
                ],
                default='FRESH_FILL',
                max_length=32,
            ),
        ),
    ]
