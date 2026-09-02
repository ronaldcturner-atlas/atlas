from django.db import migrations, models


def initialize_sort_order(apps, schema_editor):
    Facility = apps.get_model('facilities', 'Facility')
    for sort_order, facility in enumerate(Facility.objects.order_by('name', 'id'), start=1):
        facility.sort_order = sort_order
        facility.save(update_fields=['sort_order'])


class Migration(migrations.Migration):
    dependencies = [
        ('facilities', '0003_facility_short_name'),
    ]

    operations = [
        migrations.AddField(
            model_name='facility',
            name='sort_order',
            field=models.PositiveIntegerField(db_index=True, default=0),
        ),
        migrations.RunPython(initialize_sort_order, migrations.RunPython.noop),
        migrations.AlterModelOptions(
            name='facility',
            options={'ordering': ['sort_order', 'name', 'id']},
        ),
    ]
