from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('scheduling', '0053_optimizerrun_suppressed_shift_instances'),
    ]

    operations = [
        migrations.AddField(
            model_name='optimizerrun',
            name='backdated_shift_template_revisions',
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
