from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('scheduling', '0010_scheduleshiftassignment'),
    ]

    operations = [
        migrations.AddField(
            model_name='scheduleshiftassignment',
            name='assignment_source',
            field=models.CharField(
                choices=[
                    ('MANUAL', 'Manual'),
                    ('OPTIMIZER', 'Optimizer'),
                ],
                default='MANUAL',
                max_length=20,
            ),
        ),
    ]
