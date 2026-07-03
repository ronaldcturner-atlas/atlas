from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('facilities', '0002_facility_timezone_color_active'),
        ('scheduling', '0003_remove_legacy_shift_datetimes'),
    ]

    operations = [
        migrations.CreateModel(
            name='ShiftTemplate',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=120)),
                ('start_time', models.TimeField()),
                ('end_time', models.TimeField()),
                ('active_days_of_week', models.JSONField(default=list)),
                ('weekend_shift', models.BooleanField(default=False)),
                ('night_shift', models.BooleanField(default=False)),
                ('default_staffing_count', models.PositiveIntegerField(default=1)),
                ('active', models.BooleanField(default=True)),
                ('facility', models.ForeignKey(on_delete=models.deletion.CASCADE, related_name='shift_templates', to='facilities.facility')),
            ],
            options={
                'ordering': ['facility__name', 'name', 'start_time'],
            },
        ),
    ]
