from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('scheduling', '0055_shiftposting_comment'),
    ]

    operations = [
        migrations.AddField(
            model_name='contractuserassignment',
            name='rule_overrides',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name='contractuserassignment',
            name='overrides_updated_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='contractuserassignment',
            name='overrides_updated_by',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='updated_contract_user_overrides',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]

