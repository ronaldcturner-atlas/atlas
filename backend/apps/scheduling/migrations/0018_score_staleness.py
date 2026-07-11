from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('scheduling', '0017_alter_shift_notes')]

    operations = [
        migrations.AddField(
            model_name='scheduleversion',
            name='score_is_stale',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='optimizerrun',
            name='score_is_stale',
            field=models.BooleanField(default=False),
        ),
    ]
