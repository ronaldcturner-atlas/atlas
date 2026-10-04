from django.db import migrations, models
import django.db.models.deletion


def assignment_snapshot(assignment):
    if assignment is None:
        return {}
    instance = assignment.shift_instance
    physician = assignment.physician
    return {
        'id': assignment.id,
        'physician_id': physician.id,
        'physician_name': (
            physician.display_name
            or physician.user.get_full_name()
            or physician.user.username
        ),
        'date': instance.date.isoformat(),
        'facility': instance.facility.short_name,
        'shift': instance.shift_template.name,
        'start_time': instance.start_datetime.isoformat(),
        'end_time': instance.end_datetime.isoformat(),
    }


def preserve_existing_trade_assignments(apps, schema_editor):
    ShiftTrade = apps.get_model('scheduling', 'ShiftTrade')
    trades = ShiftTrade.objects.select_related(
        'offered_assignment__physician__user',
        'offered_assignment__shift_instance__facility',
        'offered_assignment__shift_instance__shift_template',
        'requested_assignment__physician__user',
        'requested_assignment__shift_instance__facility',
        'requested_assignment__shift_instance__shift_template',
    )
    for trade in trades.iterator():
        trade.offered_assignment_snapshot = assignment_snapshot(
            trade.offered_assignment,
        )
        trade.requested_assignment_snapshot = assignment_snapshot(
            trade.requested_assignment,
        )
        trade.save(update_fields=[
            'offered_assignment_snapshot',
            'requested_assignment_snapshot',
        ])


class Migration(migrations.Migration):

    dependencies = [
        ('scheduling', '0038_sharedrulecontract_whole_numbers'),
    ]

    operations = [
        migrations.AddField(
            model_name='shifttrade',
            name='offered_assignment_snapshot',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name='shifttrade',
            name='requested_assignment_snapshot',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.RunPython(
            preserve_existing_trade_assignments,
            migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name='shifttrade',
            name='offered_assignment',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='trades_offered',
                to='scheduling.scheduleshiftassignment',
            ),
        ),
        migrations.AlterField(
            model_name='shifttrade',
            name='requested_assignment',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='trades_requested',
                to='scheduling.scheduleshiftassignment',
            ),
        ),
    ]
