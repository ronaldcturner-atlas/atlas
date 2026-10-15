from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('scheduling', '0056_contract_user_rule_overrides'),
    ]

    operations = [
        migrations.AddField(
            model_name='contract',
            name='facility_settings',
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
