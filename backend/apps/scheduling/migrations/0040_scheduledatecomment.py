from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('scheduling', '0039_shifttrade_assignment_snapshots'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='ScheduleDateComment',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('date', models.DateField()),
                ('title', models.CharField(max_length=100)),
                ('details', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='schedule_date_comments_created', to=settings.AUTH_USER_MODEL)),
                ('schedule_block', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='date_comments', to='scheduling.scheduleblock')),
                ('updated_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='schedule_date_comments_updated', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['date', 'id'],
                'constraints': [models.UniqueConstraint(fields=('schedule_block', 'date'), name='unique_schedule_date_comment_per_block')],
            },
        ),
    ]
