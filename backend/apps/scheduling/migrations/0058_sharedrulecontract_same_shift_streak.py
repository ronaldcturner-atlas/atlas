from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('scheduling', '0057_contract_facility_settings'),
    ]

    operations = [
        migrations.AddField(
            model_name='sharedrulecontract',
            name='max_same_shifts_in_row',
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='sharedrulecontract',
            name='max_same_shifts_in_row_penalty_weight',
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
    ]
