from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('scheduling', '0050_alter_shifttrade_status'),
    ]

    operations = [
        migrations.CreateModel(
            name='ShiftTradeView',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('viewed_at', models.DateTimeField(auto_now_add=True)),
                ('trade', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='user_views', to='scheduling.shifttrade')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='viewed_shift_trades', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'constraints': [models.UniqueConstraint(fields=('trade', 'user'), name='unique_shift_trade_view_per_user')],
            },
        ),
    ]
