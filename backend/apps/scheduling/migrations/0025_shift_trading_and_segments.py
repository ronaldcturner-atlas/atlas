from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [('scheduling', '0024_optimizercontrol_background_job'), migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [
        migrations.RemoveConstraint(model_name='scheduleshiftinstance', name='unique_shift_template_date_per_schedule_version'),
        migrations.AddField(model_name='scheduleshiftinstance', name='split_parent', field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='split_segments', to='scheduling.scheduleshiftinstance')),
        migrations.AddConstraint(model_name='scheduleshiftinstance', constraint=models.UniqueConstraint(fields=('schedule_version', 'date', 'shift_template', 'start_datetime'), name='unique_shift_segment_per_schedule_version')),
        migrations.CreateModel(name='ShiftPosting', fields=[
            ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('mode', models.CharField(choices=[('PICKUP', 'Available for pickup'), ('TRADE_ONLY', 'Trade only')], max_length=16)),
            ('active', models.BooleanField(default=True)), ('created_at', models.DateTimeField(auto_now_add=True)), ('updated_at', models.DateTimeField(auto_now=True)),
            ('assignment', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='posting', to='scheduling.scheduleshiftassignment')),
            ('posted_by', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='shift_postings', to=settings.AUTH_USER_MODEL)),
        ]),
        migrations.CreateModel(name='ShiftTrade', fields=[
            ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('trade_type', models.CharField(choices=[('PICKUP', 'Pickup'), ('TRADE', 'Trade')], default='TRADE', max_length=12)),
            ('status', models.CharField(choices=[('PENDING_RECIPIENT', 'Pending recipient'), ('PENDING_SCHEDULER', 'Pending scheduler'), ('DECLINED', 'Declined'), ('APPROVED', 'Approved'), ('CANCELLED', 'Cancelled')], default='PENDING_RECIPIENT', max_length=24)),
            ('note', models.TextField(blank=True)), ('created_at', models.DateTimeField(auto_now_add=True)), ('updated_at', models.DateTimeField(auto_now=True)),
            ('responded_at', models.DateTimeField(blank=True, null=True)), ('reviewed_at', models.DateTimeField(blank=True, null=True)),
            ('offered_assignment', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='trades_offered', to='scheduling.scheduleshiftassignment')),
            ('requested_assignment', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='trades_requested', to='scheduling.scheduleshiftassignment')),
            ('recipient', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='shift_trades_received', to='accounts.physician')),
            ('requester', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='shift_trades_requested', to='accounts.physician')),
            ('reviewed_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='shift_trades_reviewed', to=settings.AUTH_USER_MODEL)),
        ], options={'ordering': ['-created_at', '-id']}),
        migrations.CreateModel(name='ShiftTradePolicy', fields=[
            ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('require_scheduler_approval', models.BooleanField(default=True)),
            ('updated_at', models.DateTimeField(auto_now=True)),
            ('updated_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='shift_trade_policy_updates', to=settings.AUTH_USER_MODEL)),
        ]),
    ]
