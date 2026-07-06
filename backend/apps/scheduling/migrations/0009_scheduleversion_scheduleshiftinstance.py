from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0002_physician_active_physician_clinician_type_and_more'),
        ('domains', '0001_initial'),
        ('facilities', '0003_facility_short_name'),
        ('scheduling', '0008_contract_and_assignment'),
    ]

    operations = [
        migrations.CreateModel(
            name='ScheduleVersion',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('version_number', models.PositiveIntegerField(default=1)),
                ('name', models.CharField(max_length=120)),
                ('status', models.CharField(choices=[('BUILD', 'Build'), ('PREVIEW', 'Preview'), ('ARCHIVED', 'Archived')], default='BUILD', max_length=20)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('domain', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='schedule_versions', to='domains.domain')),
                ('schedule_block', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='schedule_versions', to='scheduling.scheduleblock')),
            ],
            options={
                'ordering': ['-version_number', '-created_at'],
            },
        ),
        migrations.CreateModel(
            name='ScheduleShiftInstance',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('date', models.DateField()),
                ('start_datetime', models.DateTimeField()),
                ('end_datetime', models.DateTimeField()),
                ('required_staffing', models.PositiveIntegerField(default=1)),
                ('status', models.CharField(choices=[('OPEN', 'Open'), ('ASSIGNED', 'Assigned')], default='OPEN', max_length=20)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('assigned_user', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='schedule_shift_instances', to='accounts.physician')),
                ('facility', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='schedule_shift_instances', to='facilities.facility')),
                ('schedule_block', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='shift_instances', to='scheduling.scheduleblock')),
                ('schedule_version', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='shift_instances', to='scheduling.scheduleversion')),
                ('shift_template', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='shift_instances', to='scheduling.shifttemplate')),
            ],
            options={
                'ordering': ['date', 'start_datetime', 'facility__name', 'id'],
            },
        ),
        migrations.AddConstraint(
            model_name='scheduleversion',
            constraint=models.UniqueConstraint(fields=('schedule_block', 'domain', 'version_number'), name='unique_schedule_version_number_per_block_domain'),
        ),
        migrations.AddConstraint(
            model_name='scheduleshiftinstance',
            constraint=models.UniqueConstraint(fields=('schedule_version', 'date', 'shift_template'), name='unique_shift_template_date_per_schedule_version'),
        ),
    ]
