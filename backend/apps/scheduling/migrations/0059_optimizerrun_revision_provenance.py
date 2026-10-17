from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('scheduling', '0058_sharedrulecontract_same_shift_streak'),
    ]

    operations = [
        migrations.AddField(
            model_name='optimizerrun',
            name='completion_score',
            field=models.DecimalField(
                blank=True, decimal_places=2, max_digits=12, null=True,
            ),
        ),
        migrations.AddField(
            model_name='optimizerrun',
            name='completion_score_breakdown',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name='optimizerrun',
            name='completion_scoring_revision',
            field=models.CharField(blank=True, default='', max_length=64),
        ),
        migrations.AddField(
            model_name='optimizerrun',
            name='configuration_digest',
            field=models.CharField(blank=True, default='', max_length=64),
        ),
        migrations.AddField(
            model_name='optimizerrun',
            name='optimizer_commit',
            field=models.CharField(blank=True, default='', max_length=128),
        ),
        migrations.AddField(
            model_name='optimizerrun',
            name='optimizer_revision',
            field=models.CharField(blank=True, default='', max_length=64),
        ),
        migrations.AddField(
            model_name='optimizerrun',
            name='scoring_revision',
            field=models.CharField(blank=True, default='', max_length=64),
        ),
    ]
