from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('scheduling', '0037_sharedrule_sharedrulecontract'),
    ]

    operations = [
        migrations.AlterField(
            model_name='sharedrulecontract',
            name='min_value',
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name='sharedrulecontract',
            name='max_value',
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name='sharedrulecontract',
            name='min_penalty_weight',
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name='sharedrulecontract',
            name='max_penalty_weight',
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
    ]
