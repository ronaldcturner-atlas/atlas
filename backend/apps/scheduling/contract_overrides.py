import copy
from decimal import Decimal, InvalidOperation


CONTRACT_SETTING_FIELDS = (
    'facility_settings',
    'workload_settings',
    'shift_settings',
    'night_settings',
    'weekend_settings',
    'request_settings',
)

STRUCTURAL_RULE_KEYS = {
    'id',
    'label',
    'period_type',
    'units',
    'shared_rule_id',
    'shift_template_ids',
    'facility_id',
    'spread_violations',
    # Retained only for backward-compatible parsing of older contracts.
    'penalty_weight',
    'hard_soft',
    'min_time_off_hard_soft',
    'consecutive_penalty_weight',
}

LEGACY_UNSCORED_RULE_KEYS = {
    'min_same_shifts_in_row',
    'min_same_shifts_in_row_penalty_weight',
}


def _walk_rule_values(value, path=()):
    if isinstance(value, dict):
        for key, child in value.items():
            if key in STRUCTURAL_RULE_KEYS or key in LEGACY_UNSCORED_RULE_KEYS:
                continue
            yield from _walk_rule_values(child, (*path, str(key)))
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_rule_values(child, (*path, str(index)))
        return
    if not path or value is None or isinstance(value, bool):
        if path:
            yield '.'.join(path), value
        return
    if isinstance(value, (int, float)):
        yield '.'.join(path), value
        return
    if isinstance(value, str):
        if value == '':
            yield '.'.join(path), value
            return
        try:
            Decimal(value)
        except InvalidOperation:
            return
        yield '.'.join(path), value


def contract_rule_values(contract):
    """Return every user-overridable scalar rule keyed by a stable JSON path."""
    values = {}
    for field in CONTRACT_SETTING_FIELDS:
        settings = getattr(contract, field, {})
        if isinstance(settings, dict):
            values.update(_walk_rule_values(settings, (field,)))
    return values


def normalize_override_value(parent_value, value):
    if isinstance(parent_value, bool):
        if not isinstance(value, bool):
            raise ValueError('Boolean rules require true or false.')
        return value
    if value in (None, ''):
        return ''
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError('Rule values must be nonnegative numbers.') from exc
    if decimal_value < 0 or not decimal_value.is_finite():
        raise ValueError('Rule values must be nonnegative numbers.')
    normalized = format(decimal_value, 'f')
    if '.' in normalized:
        normalized = normalized.rstrip('0').rstrip('.')
    return normalized or '0'


def normalized_rule_overrides(contract, overrides):
    parent_values = contract_rule_values(contract)
    normalized = {}
    for path, value in (overrides or {}).items():
        if path not in parent_values:
            continue
        parent_value = parent_values[path]
        normalized_value = normalize_override_value(parent_value, value)
        if (
            path == 'workload_settings.min_days_in_row'
            and normalized_value not in ('',)
            and Decimal(normalized_value) < 2
        ):
            raise ValueError(
                'Minimum days in a row must be left blank or be 2 or greater.'
            )
        normalized_parent = normalize_override_value(parent_value, parent_value)
        if normalized_value != normalized_parent:
            normalized[path] = normalized_value
    return normalized


def _set_path(root, parts, value):
    current = root
    for part in parts[:-1]:
        if isinstance(current, list):
            index = int(part)
            if index >= len(current):
                return False
            current = current[index]
        elif isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return False
    final = parts[-1]
    if isinstance(current, list):
        index = int(final)
        if index >= len(current):
            return False
        current[index] = value
        return True
    if isinstance(current, dict) and final in current:
        current[final] = value
        return True
    return False


def effective_contract_for_assignment(assignment):
    """Return a contract-shaped object with this user's sparse overrides applied."""
    contract = copy.copy(assignment.contract)
    settings = {
        field: copy.deepcopy(getattr(assignment.contract, field, {}) or {})
        for field in CONTRACT_SETTING_FIELDS
    }
    valid_overrides = normalized_rule_overrides(
        assignment.contract, assignment.rule_overrides,
    )
    for path, value in valid_overrides.items():
        field, *parts = path.split('.')
        if field in settings and parts:
            _set_path(settings[field], parts, value)
    for field, value in settings.items():
        setattr(contract, field, value)
    contract.user_rule_overrides = valid_overrides
    contract.user_override_assignment_id = assignment.pk
    return contract


def apply_effective_contracts(assignments):
    rows = list(assignments)
    for assignment in rows:
        assignment.contract = effective_contract_for_assignment(assignment)
    return rows


def override_differences(assignment):
    parent_values = contract_rule_values(assignment.contract)
    overrides = normalized_rule_overrides(
        assignment.contract, assignment.rule_overrides,
    )
    return [
        {
            'path': path,
            'parent_value': parent_values[path],
            'override_value': value,
        }
        for path, value in overrides.items()
    ]

