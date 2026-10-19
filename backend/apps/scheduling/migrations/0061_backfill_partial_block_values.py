from datetime import timedelta
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP

from django.db import migrations


FIELDS = (
    'target_hours', 'minimum_hours', 'maximum_hours',
    'target_shifts', 'minimum_shifts', 'maximum_shifts',
)


def _number(value):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number.is_finite() else None


def _text(value):
    text = format(value, 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def forwards(apps, schema_editor):
    ScheduleBlock = apps.get_model('scheduling', 'ScheduleBlock')
    ScheduleVersion = apps.get_model('scheduling', 'ScheduleVersion')
    OptimizerRun = apps.get_model('scheduling', 'OptimizerRun')
    for block in ScheduleBlock.objects.exclude(user_planning_settings={}):
        settings = dict(block.user_planning_settings or {})
        changed = False
        total_days = (block.end_date - block.start_date).days + 1
        for physician_id, raw_plan in list(settings.items()):
            plan = dict(raw_plan or {})
            unavailable = set(plan.get('unavailable_dates') or ())
            if not unavailable or 'partial_block_base_values' in plan:
                continue
            base = {
                field: plan[field]
                for field in FIELDS
                if plan.get(field) not in (None, '')
            }
            plan['partial_block_base_values'] = base
            eligible_days = sum(
                1
                for offset in range(total_days)
                if (block.start_date + timedelta(days=offset)).isoformat() not in unavailable
            )
            ratio = Decimal(eligible_days) / Decimal(total_days) if total_days else Decimal('0')
            for field in FIELDS:
                number = _number(base.get(field))
                if number is None:
                    plan.pop(field, None)
                    continue
                rounding = (
                    ROUND_FLOOR if field.startswith('minimum_')
                    else ROUND_CEILING if field.startswith('maximum_')
                    else ROUND_HALF_UP
                )
                plan[field] = _text((number * ratio).to_integral_value(rounding=rounding))
            settings[str(physician_id)] = plan
            changed = True
        if changed:
            block.user_planning_settings = settings
            block.save(update_fields=['user_planning_settings'])
            ScheduleVersion.objects.filter(schedule_block_id=block.id).update(score_is_stale=True)
            OptimizerRun.objects.filter(schedule_version__schedule_block_id=block.id).update(score_is_stale=True)


def backwards(apps, schema_editor):
    ScheduleBlock = apps.get_model('scheduling', 'ScheduleBlock')
    ScheduleVersion = apps.get_model('scheduling', 'ScheduleVersion')
    OptimizerRun = apps.get_model('scheduling', 'OptimizerRun')
    for block in ScheduleBlock.objects.exclude(user_planning_settings={}):
        settings = dict(block.user_planning_settings or {})
        changed = False
        for physician_id, raw_plan in list(settings.items()):
            plan = dict(raw_plan or {})
            if 'partial_block_base_values' not in plan:
                continue
            base = plan.pop('partial_block_base_values') or {}
            for field in FIELDS:
                if field in base:
                    plan[field] = base[field]
                else:
                    plan.pop(field, None)
            settings[str(physician_id)] = plan
            changed = True
        if changed:
            block.user_planning_settings = settings
            block.save(update_fields=['user_planning_settings'])
            ScheduleVersion.objects.filter(schedule_block_id=block.id).update(score_is_stale=True)
            OptimizerRun.objects.filter(schedule_version__schedule_block_id=block.id).update(score_is_stale=True)


class Migration(migrations.Migration):
    dependencies = [
        ('scheduling', '0060_scheduleblock_user_planning'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
