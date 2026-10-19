import copy
from calendar import monthrange
from datetime import timedelta
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP

from .contract_overrides import (
    CONTRACT_SETTING_FIELDS,
    _set_path,
    contract_rule_values,
    effective_contract_for_assignment,
    normalize_override_value,
)


VOLUME_BOUNDS = (
    ('min_value', 'max_value'),
    ('min_shifts', 'max_shifts'),
    ('min_volume', 'max_volume'),
)

BLOCK_VALUE_FIELDS = (
    'target_hours', 'minimum_hours', 'maximum_hours',
    'target_shifts', 'minimum_shifts', 'maximum_shifts',
)


def block_month_count(block):
    return (
        (block.end_date.year - block.start_date.year) * 12
        + block.end_date.month - block.start_date.month + 1
    )


def _decimal(value):
    if value in (None, ''):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number.is_finite() else None


def _clean_number(value):
    if value is None:
        return ''
    text = format(Decimal(value), 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def _period_windows(block, period_type):
    cursor = block.start_date
    windows = []
    seen = set()
    while cursor <= block.end_date:
        if period_type == 'WEEK':
            natural_start = cursor - timedelta(days=cursor.weekday())
            natural_end = natural_start + timedelta(days=6)
        elif period_type == 'MONTH':
            natural_start = cursor.replace(day=1)
            natural_end = cursor.replace(day=monthrange(cursor.year, cursor.month)[1])
        else:
            natural_start = block.start_date
            natural_end = block.end_date
        key = (natural_start, natural_end)
        if key not in seen:
            seen.add(key)
            windows.append((
                natural_start,
                natural_end,
                max(natural_start, block.start_date),
                min(natural_end, block.end_date),
            ))
        cursor = min(natural_end, block.end_date) + timedelta(days=1)
    return windows


def _aggregate_bound(value, period_type, block, unavailable_dates, bound_type):
    value = _decimal(value)
    if value is None:
        return ''
    total = Decimal('0')
    for natural_start, natural_end, overlap_start, overlap_end in _period_windows(block, period_type):
        denominator = Decimal((natural_end - natural_start).days + 1)
        eligible = sum(
            1
            for offset in range((overlap_end - overlap_start).days + 1)
            if (overlap_start + timedelta(days=offset)).isoformat() not in unavailable_dates
        )
        total += value * Decimal(eligible) / denominator
    rounding = ROUND_FLOOR if bound_type == 'min' else ROUND_CEILING
    return _clean_number(total.to_integral_value(rounding=rounding))


def _prorate_period_rules(value, block, unavailable_dates, path, adjusted_paths):
    if isinstance(value, list):
        return [
            _prorate_period_rules(child, block, unavailable_dates, f'{path}.{index}', adjusted_paths)
            for index, child in enumerate(value)
        ]
    if not isinstance(value, dict):
        return copy.deepcopy(value)

    result = {
        key: _prorate_period_rules(child, block, unavailable_dates, f'{path}.{key}', adjusted_paths)
        for key, child in value.items()
    }
    period_type = value.get('period_type')
    if period_type in {'WEEK', 'MONTH', 'SCHEDULE_BLOCK'}:
        for minimum_key, maximum_key in VOLUME_BOUNDS:
            if minimum_key in value or maximum_key in value:
                for key, bound_type in ((minimum_key, 'min'), (maximum_key, 'max')):
                    if key not in value:
                        continue
                    adjusted = _aggregate_bound(
                        value.get(key), period_type, block, unavailable_dates, bound_type,
                    )
                    result[key] = adjusted
                    if (
                        normalize_override_value(value.get(key), value.get(key))
                        != normalize_override_value(value.get(key), adjusted)
                        or (unavailable_dates and period_type != 'SCHEDULE_BLOCK')
                    ):
                        adjusted_paths.append(f'{path}.{key}')
                result['period_type'] = 'SCHEDULE_BLOCK'
                break
    return result


def generated_partial_settings(contract, block, unavailable_dates):
    unavailable = set(unavailable_dates or ())
    settings = {}
    adjusted_paths = []
    for field in CONTRACT_SETTING_FIELDS:
        settings[field] = _prorate_period_rules(
            getattr(contract, field, {}) or {}, block, unavailable, field,
            adjusted_paths,
        )
    return settings, sorted(set(adjusted_paths))


def prorated_block_values(values, block, unavailable_dates):
    """Prorate full-block planning values into temporary effective values."""
    unavailable = set(unavailable_dates or ())
    total_days = (block.end_date - block.start_date).days + 1
    eligible_days = sum(
        1
        for offset in range(total_days)
        if (block.start_date + timedelta(days=offset)).isoformat() not in unavailable
    )
    ratio = Decimal(eligible_days) / Decimal(total_days) if total_days else Decimal('0')
    result = {}
    for field in BLOCK_VALUE_FIELDS:
        value = _decimal((values or {}).get(field))
        if value is None:
            continue
        rounding = (
            ROUND_FLOOR if field.startswith('minimum_')
            else ROUND_CEILING if field.startswith('maximum_')
            else ROUND_HALF_UP
        )
        result[field] = _clean_number((value * ratio).to_integral_value(rounding=rounding))
    return result


def apply_manual_temporary_overrides(contract, settings, overrides):
    temporary = copy.copy(contract)
    for field, value in settings.items():
        setattr(temporary, field, copy.deepcopy(value))
    valid_values = contract_rule_values(temporary)
    normalized = {}
    for path, value in (overrides or {}).items():
        if path not in valid_values:
            continue
        normalized_value = normalize_override_value(valid_values[path], value)
        field, *parts = path.split('.')
        if parts and _set_path(settings[field], parts, normalized_value):
            normalized[path] = normalized_value
    return settings, normalized


def effective_contract_with_block_plan(assignment, block):
    contract = effective_contract_for_assignment(assignment)
    plan = (block.user_planning_settings or {}).get(str(assignment.physician_id), {})
    unavailable_dates = set(plan.get('unavailable_dates') or ())
    if unavailable_dates:
        settings, adjusted_paths = generated_partial_settings(
            contract, block, unavailable_dates,
        )
        settings, manual = apply_manual_temporary_overrides(
            contract, settings, plan.get('temporary_rule_overrides') or {},
        )
        for field, value in settings.items():
            setattr(contract, field, value)
        contract.partial_block_adjusted_paths = adjusted_paths
        contract.partial_block_manual_overrides = manual
    contract.block_user_plan = plan
    contract.block_unavailable_dates = unavailable_dates
    return contract


def apply_block_user_plans(assignments, block):
    rows = list(assignments)
    for assignment in rows:
        assignment.contract = effective_contract_with_block_plan(assignment, block)
    return rows


def block_workload_override(plan, units):
    prefix = 'hours' if units == 'HOURS' else 'shifts'
    minimum = _decimal(plan.get(f'minimum_{prefix}'))
    maximum = _decimal(plan.get(f'maximum_{prefix}'))
    if minimum is None and maximum is None:
        return None
    return {'minimum': minimum, 'maximum': maximum}
