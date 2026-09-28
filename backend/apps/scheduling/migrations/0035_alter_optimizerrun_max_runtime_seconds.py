from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('scheduling', '0034_scheduleversion_publication_snapshot')]

    operations = [
        migrations.AlterField(
            model_name='optimizerrun',
            name='max_runtime_seconds',
            field=models.PositiveIntegerField(default=7200),
        ),
    ]
