from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('scheduling', '0005_shift_template_weekend_days'),
    ]

    operations = [
        migrations.CreateModel(
            name='ScheduleBlock',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('start_date', models.DateField()),
                ('end_date', models.DateField()),
                ('request_open_datetime', models.DateTimeField()),
                ('request_close_datetime', models.DateTimeField()),
                ('build_status', models.CharField(choices=[('PRE_BUILD', 'PRE_BUILD'), ('BUILD', 'BUILD'), ('PREVIEW', 'PREVIEW'), ('ARCHIVE', 'ARCHIVE')], default='PRE_BUILD', editable=False, max_length=20)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('published_at', models.DateTimeField(blank=True, null=True)),
            ],
            options={
                'ordering': ['-created_at', '-id'],
            },
        ),
    ]
