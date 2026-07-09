from django.db import migrations, models


def backfill_optimizer_run_seeds(apps, schema_editor):
    OptimizerRun = apps.get_model('scheduling', 'OptimizerRun')
    for optimizer_run in OptimizerRun.objects.filter(seed__isnull=True).order_by('id'):
        optimizer_run.seed = optimizer_run.id
        optimizer_run.save(update_fields=['seed'])


def clear_backfilled_optimizer_run_seeds(apps, schema_editor):
    OptimizerRun = apps.get_model('scheduling', 'OptimizerRun')
    OptimizerRun.objects.update(seed=None)


class Migration(migrations.Migration):

    dependencies = [
        ('scheduling', '0014_optimizer_run_history'),
    ]

    operations = [
        migrations.AddField(
            model_name='optimizerrun',
            name='seed',
            field=models.BigIntegerField(blank=True, null=True),
        ),
        migrations.RunPython(backfill_optimizer_run_seeds, clear_backfilled_optimizer_run_seeds),
    ]
