from datetime import date, time

from django.db import migrations, models


def backfill_shift_date_and_time(apps, schema_editor):
    Shift = apps.get_model('scheduling', 'Shift')

    for shift in Shift.objects.all():
        start_datetime = getattr(shift, 'start_datetime', None)
        end_datetime = getattr(shift, 'end_datetime', None)

        if start_datetime:
            shift.date = start_datetime.date()
            shift.start_time = start_datetime.time().replace(microsecond=0)

        if end_datetime:
            shift.end_time = end_datetime.time().replace(microsecond=0)

        shift.save(update_fields=['date', 'start_time', 'end_time'])


class Migration(migrations.Migration):

    dependencies = [
        ('scheduling', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='shift',
            name='date',
            field=models.DateField(default=date.today),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='shift',
            name='start_time',
            field=models.TimeField(default=time(7, 0)),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='shift',
            name='end_time',
            field=models.TimeField(default=time(19, 0)),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='shift',
            name='shift_type',
            field=models.CharField(
                choices=[
                    ('clinical', 'Clinical'),
                    ('administrative', 'Administrative'),
                    ('vacation', 'Vacation'),
                    ('cme', 'CME'),
                    ('meeting', 'Meeting'),
                    ('holiday', 'Holiday'),
                ],
                default='clinical',
                max_length=30,
            ),
        ),
        migrations.AddField(
            model_name='shift',
            name='notes',
            field=models.TextField(blank=True, default=''),
        ),
        migrations.AlterField(
            model_name='shift',
            name='role',
            field=models.CharField(
                choices=[
                    ('physician', 'Physician'),
                    ('fast_track', 'Fast Track'),
                    ('triage', 'Triage'),
                    ('swing', 'Swing'),
                    ('night', 'Night'),
                    ('backup', 'Backup'),
                    ('administrative', 'Administrative'),
                ],
                default='physician',
                max_length=30,
            ),
        ),
        migrations.AlterField(
            model_name='shift',
            name='status',
            field=models.CharField(
                choices=[
                    ('scheduled', 'Scheduled'),
                    ('requested', 'Requested'),
                    ('approved', 'Approved'),
                    ('completed', 'Completed'),
                    ('cancelled', 'Cancelled'),
                ],
                default='scheduled',
                max_length=20,
            ),
        ),
        migrations.RunPython(backfill_shift_date_and_time, migrations.RunPython.noop),
        migrations.AlterModelOptions(
            name='shift',
            options={'ordering': ['-date', 'start_time']},
        ),
    ]
