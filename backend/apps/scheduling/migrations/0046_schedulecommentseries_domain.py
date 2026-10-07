from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('domains', '0006_role_templates_and_permissions'),
        ('scheduling', '0045_scheduleblock_preview_optimizer_run'),
    ]

    operations = [
        migrations.AddField(
            model_name='schedulecommentseries',
            name='domain',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='schedule_comment_series',
                to='domains.domain',
            ),
        ),
    ]
