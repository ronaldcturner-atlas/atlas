from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('scheduling', '0022_optimizercontrol'),
    ]

    operations = [
        migrations.AddField(
            model_name='scheduleversion',
            name='shift_template_fingerprint',
            field=models.CharField(blank=True, default='', max_length=64),
        ),
    ]
