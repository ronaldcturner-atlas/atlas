from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('scheduling', '0054_optimizerrun_backdated_template_revisions'),
    ]

    operations = [
        migrations.AddField(
            model_name='shiftposting',
            name='comment',
            field=models.TextField(blank=True),
        ),
    ]
