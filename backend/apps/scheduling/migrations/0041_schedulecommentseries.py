from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('scheduling', '0040_scheduledatecomment'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='ScheduleCommentSeries',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('title', models.CharField(max_length=100)),
                ('details', models.TextField(blank=True)),
                ('start_date', models.DateField()),
                ('recurrence_type', models.CharField(choices=[('WEEKLY', 'Weekly interval'), ('MONTHLY', 'Monthly weekday')], max_length=12)),
                ('interval', models.PositiveSmallIntegerField(default=1)),
                ('weekday', models.PositiveSmallIntegerField()),
                ('monthly_ordinal', models.SmallIntegerField(blank=True, null=True)),
                ('end_type', models.CharField(choices=[('NEVER', 'Does not end'), ('ON_DATE', 'End on date'), ('AFTER_COUNT', 'End after occurrences')], default='NEVER', max_length=16)),
                ('end_date', models.DateField(blank=True, null=True)),
                ('occurrence_count', models.PositiveIntegerField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='schedule_comment_series_created', to=settings.AUTH_USER_MODEL)),
                ('updated_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='schedule_comment_series_updated', to=settings.AUTH_USER_MODEL)),
            ],
            options={'ordering': ['start_date', 'id']},
        ),
        migrations.CreateModel(
            name='ScheduleCommentSeriesException',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('date', models.DateField()),
                ('is_cancelled', models.BooleanField(default=False)),
                ('title', models.CharField(blank=True, max_length=100, null=True)),
                ('details', models.TextField(blank=True, null=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('series', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='exceptions', to='scheduling.schedulecommentseries')),
                ('updated_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='schedule_comment_series_exceptions_updated', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['date', 'id'],
                'constraints': [models.UniqueConstraint(fields=('series', 'date'), name='unique_schedule_comment_series_exception')],
            },
        ),
    ]
