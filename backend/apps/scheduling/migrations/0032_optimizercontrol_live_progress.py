from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('scheduling', '0031_optimizerrun_start_lineage'),
    ]

    operations = [
        migrations.AddField(
            model_name='optimizercontrol',
            name='live_best_score',
            field=models.DecimalField(
                blank=True, decimal_places=2, max_digits=12, null=True,
            ),
        ),
        migrations.AddField(
            model_name='optimizercontrol',
            name='progress_updated_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
