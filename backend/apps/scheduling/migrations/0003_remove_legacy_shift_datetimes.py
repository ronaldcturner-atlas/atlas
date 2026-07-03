from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('scheduling', '0002_shift_management_schema'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='shift',
            name='start_datetime',
        ),
        migrations.RemoveField(
            model_name='shift',
            name='end_datetime',
        ),
    ]
