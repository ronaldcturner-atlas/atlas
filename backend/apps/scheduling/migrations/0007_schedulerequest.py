from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('accounts', '0002_physician_active_physician_clinician_type_and_more'),
        ('scheduling', '0006_scheduleblock'),
    ]

    operations = [
        migrations.CreateModel(
            name='ScheduleRequest',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('date', models.DateField()),
                ('request_scope', models.CharField(choices=[('USER', 'User'), ('ADMIN', 'Admin')], default='USER', max_length=10)),
                ('request_type', models.CharField(choices=[('DAY_OFF', 'Day Off'), ('SHIFT_OFF', 'Shift Off'), ('DAY_ON', 'Day On'), ('SHIFT_ON', 'Shift On')], max_length=12)),
                ('weight', models.CharField(choices=[('LOW', 'Low'), ('MEDIUM', 'Medium'), ('HIGH', 'High'), ('FIXED', 'Fixed')], max_length=10)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='created_schedule_requests', to=settings.AUTH_USER_MODEL)),
                ('physician', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='schedule_requests', to='accounts.physician')),
                ('schedule_block', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='requests', to='scheduling.scheduleblock')),
                ('shift_templates', models.ManyToManyField(blank=True, related_name='schedule_requests', to='scheduling.shifttemplate')),
            ],
            options={
                'ordering': ['date', 'physician__user__last_name', 'physician__user__first_name', 'request_scope'],
            },
        ),
        migrations.AddConstraint(
            model_name='schedulerequest',
            constraint=models.UniqueConstraint(condition=models.Q(request_scope='USER'), fields=('schedule_block', 'physician', 'date'), name='unique_user_request_per_block_physician_date'),
        ),
        migrations.AddConstraint(
            model_name='schedulerequest',
            constraint=models.UniqueConstraint(condition=models.Q(request_scope='ADMIN'), fields=('schedule_block', 'physician', 'date'), name='unique_admin_request_per_block_physician_date'),
        ),
    ]
