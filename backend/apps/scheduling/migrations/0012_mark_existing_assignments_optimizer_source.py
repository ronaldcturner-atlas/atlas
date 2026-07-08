from django.db import migrations


def mark_existing_assignments_optimizer(apps, schema_editor):
    ScheduleShiftAssignment = apps.get_model('scheduling', 'ScheduleShiftAssignment')
    ScheduleShiftAssignment.objects.filter(assignment_source='MANUAL').update(
        assignment_source='OPTIMIZER',
    )


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('scheduling', '0011_scheduleshiftassignment_assignment_source'),
    ]

    operations = [
        migrations.RunPython(mark_existing_assignments_optimizer, noop_reverse),
    ]
