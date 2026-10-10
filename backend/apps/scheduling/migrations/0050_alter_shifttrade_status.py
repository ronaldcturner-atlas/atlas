from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('scheduling', '0049_optimizerrun_manual_edit_snapshot'),
    ]

    operations = [
        migrations.AlterField(
            model_name='shifttrade',
            name='status',
            field=models.CharField(
                choices=[
                    ('PENDING_RECIPIENT', 'Pending recipient'),
                    ('PENDING_SCHEDULER', 'Pending scheduler'),
                    ('DECLINED', 'Declined'),
                    ('APPROVED', 'Approved'),
                    ('CANCELLED', 'Cancelled'),
                    ('EXPIRED', 'Expired'),
                ],
                default='PENDING_RECIPIENT',
                max_length=24,
            ),
        ),
    ]
