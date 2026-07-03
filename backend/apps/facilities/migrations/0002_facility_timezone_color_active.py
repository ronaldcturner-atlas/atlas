from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('facilities', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='facility',
            name='active',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='facility',
            name='color',
            field=models.CharField(default='#2563eb', max_length=7),
        ),
        migrations.AddField(
            model_name='facility',
            name='timezone',
            field=models.CharField(default='UTC', max_length=64),
        ),
    ]
