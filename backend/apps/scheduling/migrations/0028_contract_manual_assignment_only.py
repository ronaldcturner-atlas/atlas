from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('scheduling', '0027_shiftstatsgroup'),
    ]

    operations = [
        migrations.AddField(
            model_name='contract',
            name='manual_assignment_only',
            field=models.BooleanField(default=False),
        ),
    ]
