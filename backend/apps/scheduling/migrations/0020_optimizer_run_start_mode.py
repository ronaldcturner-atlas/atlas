from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('scheduling', '0019_optimizer_run_copy_metadata')]

    operations = [
        migrations.AddField(
            model_name='optimizerrun',
            name='start_mode',
            field=models.CharField(
                choices=[
                    ('CURRENT_SCHEDULE', 'Current schedule'),
                    ('FRESH_FILL', 'Fresh fill'),
                ],
                default='FRESH_FILL',
                max_length=24,
            ),
        ),
    ]
