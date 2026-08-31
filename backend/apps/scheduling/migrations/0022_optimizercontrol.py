from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('scheduling', '0021_scheduleversion_workload_hour_overrides'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [migrations.CreateModel(
        name='OptimizerControl',
        fields=[
            ('token', models.UUIDField(primary_key=True, serialize=False)),
            ('stop_requested', models.BooleanField(default=False)),
            ('created_at', models.DateTimeField(auto_now_add=True)),
            ('created_by', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL)),
            ('schedule_version', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, to='scheduling.scheduleversion')),
        ],
    )]
