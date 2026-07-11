from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('scheduling', '0015_optimizer_run_seed')]

    operations = [
        migrations.AddField(
            model_name='scheduleshiftassignment',
            name='is_locked',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='scheduleshiftinstance',
            name='is_locked_open',
            field=models.BooleanField(default=False),
        ),
    ]
