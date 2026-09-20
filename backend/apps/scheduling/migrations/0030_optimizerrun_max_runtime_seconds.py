from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('scheduling', '0029_alter_shifttemplate_options')]

    operations = [
        migrations.AddField(
            model_name='optimizerrun',
            name='max_runtime_seconds',
            field=models.PositiveIntegerField(default=900),
        ),
    ]
