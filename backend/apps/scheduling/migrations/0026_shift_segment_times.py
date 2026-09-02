from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('scheduling', '0025_shift_trading_and_segments')]
    operations = [
        migrations.AddField(model_name='scheduleshiftinstance', name='segment_start_time', field=models.TimeField(blank=True, null=True)),
        migrations.AddField(model_name='scheduleshiftinstance', name='segment_end_time', field=models.TimeField(blank=True, null=True)),
    ]
