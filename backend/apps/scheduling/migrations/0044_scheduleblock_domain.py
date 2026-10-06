from django.db import migrations, models
import django.db.models.deletion
import apps.domains.models


def assign_schedule_block_domains(apps, schema_editor):
    ScheduleBlock = apps.get_model('scheduling', 'ScheduleBlock')
    ScheduleVersion = apps.get_model('scheduling', 'ScheduleVersion')
    Domain = apps.get_model('domains', 'Domain')

    fallback_domain_id = Domain.objects.order_by('id').values_list('id', flat=True).first()
    for block in ScheduleBlock.objects.all().iterator():
        domain_id = (
            ScheduleVersion.objects.filter(schedule_block_id=block.id)
            .order_by('created_at', 'id')
            .values_list('domain_id', flat=True)
            .first()
        ) or fallback_domain_id
        if domain_id is not None:
            ScheduleBlock.objects.filter(id=block.id).update(domain_id=domain_id)


class Migration(migrations.Migration):
    dependencies = [
        ('domains', '0005_region_hierarchy'),
        ('scheduling', '0043_sharedrule_reference_contract_settings'),
    ]

    operations = [
        migrations.AddField(
            model_name='scheduleblock',
            name='domain',
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='schedule_blocks',
                to='domains.domain',
            ),
        ),
        migrations.RunPython(assign_schedule_block_domains, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='scheduleblock',
            name='domain',
            field=models.ForeignKey(
                default=apps.domains.models.get_default_domain_id,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='schedule_blocks',
                to='domains.domain',
            ),
        ),
    ]
