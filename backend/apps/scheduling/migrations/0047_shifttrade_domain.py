from django.db import migrations, models
import django.db.models.deletion


def scope_existing_trades(apps, schema_editor):
    ShiftTrade = apps.get_model('scheduling', 'ShiftTrade')
    for trade in ShiftTrade.objects.filter(domain__isnull=True).select_related(
        'offered_assignment__shift_instance__schedule_version',
        'requested_assignment__shift_instance__schedule_version',
    ):
        assignment = trade.offered_assignment or trade.requested_assignment
        if assignment is not None:
            trade.domain_id = assignment.shift_instance.schedule_version.domain_id
            trade.save(update_fields=['domain'])


class Migration(migrations.Migration):
    dependencies = [
        ('domains', '0006_role_templates_and_permissions'),
        ('scheduling', '0046_schedulecommentseries_domain'),
    ]

    operations = [
        migrations.AddField(
            model_name='shifttrade',
            name='domain',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='shift_trades',
                to='domains.domain',
            ),
        ),
        migrations.RunPython(scope_existing_trades, migrations.RunPython.noop),
    ]
