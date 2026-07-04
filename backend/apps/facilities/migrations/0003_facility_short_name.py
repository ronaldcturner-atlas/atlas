from django.db import migrations, models


def _derive_short_name(full_name: str) -> str:
    name = (full_name or '').strip()
    if not name:
        return ''

    # Explicit V1 requirement for existing Berkeley Hospital facility.
    if name.lower() == 'berkeley hospital':
        return 'Berkeley'

    suffixes = [
        ' medical center',
        ' health center',
        ' healthcare center',
        ' hospital',
        ' clinic',
        ' center',
    ]

    lowered = name.lower()
    for suffix in suffixes:
        if lowered.endswith(suffix):
            candidate = name[: -len(suffix)].strip(' ,-/')
            return candidate or name

    return name


def backfill_facility_short_names(apps, schema_editor):
    Facility = apps.get_model('facilities', 'Facility')

    for facility in Facility.objects.all().only('id', 'name', 'short_name'):
        facility.short_name = _derive_short_name(facility.name)
        facility.save(update_fields=['short_name'])


class Migration(migrations.Migration):

    dependencies = [
        ('facilities', '0002_facility_timezone_color_active'),
    ]

    operations = [
        migrations.AddField(
            model_name='facility',
            name='short_name',
            field=models.CharField(default='', max_length=120),
            preserve_default=False,
        ),
        migrations.RunPython(backfill_facility_short_names, migrations.RunPython.noop),
    ]
