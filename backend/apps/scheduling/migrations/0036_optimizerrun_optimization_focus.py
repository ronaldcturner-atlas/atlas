from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('scheduling', '0035_alter_optimizerrun_max_runtime_seconds'),
    ]

    operations = [
        migrations.AddField(
            model_name='optimizerrun',
            name='optimization_focus',
            field=models.CharField(
                choices=[
                    ('STANDARD', 'Standard optimization'),
                    ('DISTRIBUTION', 'Facility/Shift distribution focus'),
                ],
                default='STANDARD',
                max_length=24,
            ),
        ),
    ]
