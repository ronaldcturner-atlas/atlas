from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('scheduling', '0052_shift_backdate_workflow'),
    ]

    operations = [
        migrations.AddField(
            model_name='optimizerrun',
            name='suppressed_shift_instance_ids',
            field=models.JSONField(blank=True, default=list),
        ),
    ]
