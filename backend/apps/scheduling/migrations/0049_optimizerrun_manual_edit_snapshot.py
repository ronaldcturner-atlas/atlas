from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('scheduling', '0048_original_published_schedule'),
    ]

    operations = [
        migrations.AddField(
            model_name='optimizerrun',
            name='manual_edit_snapshot',
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
