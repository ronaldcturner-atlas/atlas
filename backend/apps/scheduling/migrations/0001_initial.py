# Generated migration

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('facilities', '0001_initial'),
        ('accounts', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='Shift',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('role', models.CharField(max_length=100)),
                ('start_datetime', models.DateTimeField()),
                ('end_datetime', models.DateTimeField()),
                ('status', models.CharField(choices=[('scheduled', 'Scheduled'), ('completed', 'Completed'), ('cancelled', 'Cancelled')], default='scheduled', max_length=20)),
                ('facility', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='shifts', to='facilities.facility')),
                ('physician', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='shifts', to='accounts.physician')),
            ],
            options={
                'ordering': ['-start_datetime'],
            },
        ),
    ]
