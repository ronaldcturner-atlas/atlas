from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('domains', '0007_fixed_request_permission'),
        ('scheduling', '0051_shifttradeview'),
    ]

    operations = [
        migrations.AddField(
            model_name='scheduleblock',
            name='shift_configuration_revision',
            field=models.PositiveBigIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='scheduleversion',
            name='shift_configuration_revision',
            field=models.PositiveBigIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='optimizerrun',
            name='shift_configuration_revision',
            field=models.PositiveBigIntegerField(default=0),
        ),
        migrations.CreateModel(
            name='DomainShiftConfiguration',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('revision', models.PositiveBigIntegerField(default=0)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('domain', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='shift_configuration', to='domains.domain')),
            ],
        ),
        migrations.CreateModel(
            name='ShiftTemplateChange',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('revision', models.PositiveBigIntegerField()),
                ('change_type', models.CharField(choices=[('ADDED', 'Added'), ('UPDATED', 'Updated'), ('DELETED', 'Deleted')], max_length=12)),
                ('before_state', models.JSONField(blank=True, default=dict)),
                ('after_state', models.JSONField(blank=True, default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('changed_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='shift_template_changes_created', to=settings.AUTH_USER_MODEL)),
                ('domain', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='shift_template_changes', to='domains.domain')),
                ('shift_template', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='configuration_changes', to='scheduling.shifttemplate')),
            ],
            options={'ordering': ['revision', 'id']},
        ),
        migrations.AddConstraint(
            model_name='shifttemplatechange',
            constraint=models.UniqueConstraint(fields=('domain', 'revision'), name='unique_shift_configuration_revision_per_domain'),
        ),
        migrations.CreateModel(
            name='ScheduleBlockShiftChange',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('acknowledged_at', models.DateTimeField(blank=True, null=True)),
                ('acknowledged_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='schedule_block_shift_changes_acknowledged', to=settings.AUTH_USER_MODEL)),
                ('change', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='affected_schedule_blocks', to='scheduling.shifttemplatechange')),
                ('schedule_block', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='pending_shift_changes', to='scheduling.scheduleblock')),
            ],
            options={'ordering': ['change__revision', 'id']},
        ),
        migrations.AddConstraint(
            model_name='scheduleblockshiftchange',
            constraint=models.UniqueConstraint(fields=('schedule_block', 'change'), name='unique_shift_change_per_schedule_block'),
        ),
        migrations.CreateModel(
            name='PublishedShiftBackdate',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('start_date', models.DateField()),
                ('end_date', models.DateField()),
                ('shift_template_ids', models.JSONField(default=list)),
                ('changes', models.JSONField(default=list)),
                ('cancelled_trade_ids', models.JSONField(default=list)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='published_shift_backdates_created', to=settings.AUTH_USER_MODEL)),
                ('domain', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='published_shift_backdates', to='domains.domain')),
                ('initiated_from_block', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='published_shift_backdates', to='scheduling.scheduleblock')),
            ],
            options={'ordering': ['-created_at', '-id']},
        ),
    ]
