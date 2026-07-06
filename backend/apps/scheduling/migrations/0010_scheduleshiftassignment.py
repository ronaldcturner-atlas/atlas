from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def copy_placeholder_assignments(apps, schema_editor):
    ScheduleShiftInstance = apps.get_model('scheduling', 'ScheduleShiftInstance')
    ScheduleShiftAssignment = apps.get_model('scheduling', 'ScheduleShiftAssignment')

    assignments = [
        ScheduleShiftAssignment(
            shift_instance_id=instance.id,
            physician_id=instance.assigned_user_id,
        )
        for instance in ScheduleShiftInstance.objects.exclude(assigned_user_id=None)
    ]
    ScheduleShiftAssignment.objects.bulk_create(assignments, ignore_conflicts=True)


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('accounts', '0002_physician_active_physician_clinician_type_and_more'),
        ('scheduling', '0009_scheduleversion_scheduleshiftinstance'),
    ]

    operations = [
        migrations.CreateModel(
            name='ScheduleShiftAssignment',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='created_schedule_shift_assignments', to=settings.AUTH_USER_MODEL)),
                ('physician', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='schedule_shift_assignments', to='accounts.physician')),
                ('shift_instance', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='assignments', to='scheduling.scheduleshiftinstance')),
            ],
            options={
                'ordering': ['physician__user__last_name', 'physician__user__first_name', 'id'],
            },
        ),
        migrations.AddConstraint(
            model_name='scheduleshiftassignment',
            constraint=models.UniqueConstraint(fields=('shift_instance', 'physician'), name='unique_physician_per_schedule_shift_instance'),
        ),
        migrations.RunPython(copy_placeholder_assignments, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name='scheduleshiftinstance',
            name='assigned_user',
        ),
    ]
