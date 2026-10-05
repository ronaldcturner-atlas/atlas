from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('scheduling', '0042_shifttemplate_domain'),
    ]

    operations = [
        migrations.AddField(
            model_name='sharedrule',
            name='reference_contract_settings',
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name='sharedrule',
            name='reference_shift_templates',
            field=models.JSONField(blank=True, default=list),
        ),
    ]
