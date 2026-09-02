from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('scheduling', '0023_scheduleversion_shift_template_fingerprint'),
    ]

    operations = [
        migrations.AddField(
            model_name='optimizercontrol',
            name='optimizer_run',
            field=models.OneToOneField(
                blank=True, null=True, on_delete=django.db.models.deletion.CASCADE,
                related_name='control', to='scheduling.optimizerrun',
            ),
        ),
        migrations.AddField(
            model_name='optimizercontrol',
            name='source_run',
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name='queued_from_controls', to='scheduling.optimizerrun',
            ),
        ),
        migrations.AddField(
            model_name='optimizercontrol',
            name='started_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
