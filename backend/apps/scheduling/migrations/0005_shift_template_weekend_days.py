from django.db import migrations, models


def migrate_weekend_shift_to_days(apps, schema_editor):
    ShiftTemplate = apps.get_model('scheduling', 'ShiftTemplate')

    for template in ShiftTemplate.objects.all():
        weekend_shift = getattr(template, 'weekend_shift', False)
        active_days = getattr(template, 'active_days_of_week', []) or []

        if weekend_shift:
            template.weekend_days = [
                day for day in ['Friday', 'Saturday', 'Sunday'] if day in active_days
            ]
        else:
            template.weekend_days = []

        template.save(update_fields=['weekend_days'])


class Migration(migrations.Migration):

    dependencies = [
        ('scheduling', '0004_shifttemplate'),
    ]

    operations = [
        migrations.AddField(
            model_name='shifttemplate',
            name='weekend_days',
            field=models.JSONField(default=list),
        ),
        migrations.RunPython(migrate_weekend_shift_to_days, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name='shifttemplate',
            name='weekend_shift',
        ),
    ]
