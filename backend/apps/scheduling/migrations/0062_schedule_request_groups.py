import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('domains', '0007_fixed_request_permission'),
        ('scheduling', '0061_backfill_partial_block_values'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='ScheduleRequestGroup',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=120)),
                ('normalized_name', models.CharField(max_length=120)),
                ('revision', models.PositiveBigIntegerField(default=1)),
                ('needs_attention', models.BooleanField(default=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='created_schedule_request_groups', to=settings.AUTH_USER_MODEL)),
                ('domain', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='schedule_request_groups', to='domains.domain')),
                ('updated_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='updated_schedule_request_groups', to=settings.AUTH_USER_MODEL)),
            ],
            options={'ordering': ['normalized_name', 'id']},
        ),
        migrations.CreateModel(
            name='ScheduleRequestBulkOperation',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('request_scope', models.CharField(choices=[('USER', 'User'), ('ADMIN', 'Admin')], max_length=10)),
                ('request_type', models.CharField(choices=[('DAY_OFF', 'Day Off'), ('SHIFT_OFF', 'Shift Off'), ('DAY_ON', 'Day On'), ('SHIFT_ON', 'Shift On')], max_length=12)),
                ('weight', models.CharField(choices=[('LOW', 'Low'), ('MEDIUM', 'Medium'), ('HIGH', 'High'), ('FIXED', 'Fixed')], max_length=10)),
                ('group_snapshot', models.JSONField(blank=True, default=list)),
                ('recipient_snapshot', models.JSONField(blank=True, default=list)),
                ('dates', models.JSONField(blank=True, default=list)),
                ('expires_after', models.DateField()),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='schedule_request_bulk_operations', to=settings.AUTH_USER_MODEL)),
                ('schedule_block', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='request_bulk_operations', to='scheduling.scheduleblock')),
            ],
            options={'ordering': ['-created_at', '-id']},
        ),
        migrations.CreateModel(
            name='ScheduleRequestGroupMember',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('domain_membership', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='schedule_request_group_memberships', to='domains.domainmembership')),
                ('group', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='memberships', to='scheduling.schedulerequestgroup')),
            ],
            options={'ordering': ['domain_membership__user__last_name', 'domain_membership__user__first_name', 'id']},
        ),
        migrations.CreateModel(
            name='ScheduleRequestBulkOperationItem',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('physician_name', models.CharField(max_length=200)),
                ('date', models.DateField()),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('operation', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='items', to='scheduling.schedulerequestbulkoperation')),
                ('physician', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='schedule_request_bulk_operation_items', to='accounts.physician')),
                ('schedule_request', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='bulk_operation_items', to='scheduling.schedulerequest')),
            ],
            options={'ordering': ['date', 'physician_name', 'id']},
        ),
        migrations.AddConstraint(
            model_name='schedulerequestgroup',
            constraint=models.UniqueConstraint(fields=('domain', 'normalized_name'), name='unique_schedule_request_group_name_per_domain'),
        ),
        migrations.AddConstraint(
            model_name='schedulerequestgroupmember',
            constraint=models.UniqueConstraint(fields=('group', 'domain_membership'), name='unique_schedule_request_group_member'),
        ),
    ]
