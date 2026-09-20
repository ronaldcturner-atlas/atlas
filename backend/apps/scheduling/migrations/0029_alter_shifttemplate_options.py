from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ('scheduling', '0028_contract_manual_assignment_only'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='shifttemplate',
            options={
                'ordering': [
                    'facility__sort_order',
                    'facility__name',
                    'start_time',
                    'end_time',
                    'id',
                ],
            },
        ),
    ]
