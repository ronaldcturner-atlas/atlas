from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0002_physician_active_physician_clinician_type_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='physician',
            name='phone_number',
            field=models.CharField(blank=True, max_length=30),
        ),
    ]
