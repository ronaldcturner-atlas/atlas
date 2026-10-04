from copy import deepcopy

from .models import Contract, SharedRule


def _without_shared_rule(settings, shared_rule_id):
    settings = deepcopy(settings) if isinstance(settings, dict) else {}
    rules = settings.get('rules')
    if not isinstance(rules, list):
        rules = []
    settings['rules'] = [
        rule for rule in rules
        if not (
            isinstance(rule, dict)
            and int(rule.get('shared_rule_id') or 0) == int(shared_rule_id)
        )
    ]
    return settings


def sync_shared_rule_contract_settings(shared_rule, previous_contract_ids=()):
    """Materialize the active Shared Rule into existing contract JSON rules."""
    links = {
        link.contract_id: link
        for link in shared_rule.contract_links.select_related('contract').all()
    }
    contract_ids = set(previous_contract_ids) | set(links)
    contracts = {
        contract.id: contract
        for contract in Contract.objects.filter(id__in=contract_ids)
        .prefetch_related('facilities')
    }
    template_rows = list(
        shared_rule.shift_templates.values_list('id', 'facility_id')
    )
    for contract_id in contract_ids:
        contract = contracts.get(contract_id)
        if contract is None:
            continue
        settings = _without_shared_rule(
            contract.shift_settings, shared_rule.id,
        )
        link = links.get(contract_id)
        if shared_rule.active and link is not None and link.enabled:
            eligible_facilities = set(
                contract.facilities.values_list('id', flat=True)
            )
            eligible_template_ids = [
                template_id for template_id, facility_id in template_rows
                if facility_id in eligible_facilities
            ]
            settings['rules'].append({
                'shared_rule_id': shared_rule.id,
                'label': shared_rule.name,
                'shift_template_ids': eligible_template_ids,
                'period_rules': [{
                    'shared_rule_id': shared_rule.id,
                    'period_type': shared_rule.period_type,
                    'units': shared_rule.units,
                    'min_value': (
                        str(int(link.min_value))
                        if link.min_value is not None else ''
                    ),
                    'max_value': (
                        str(int(link.max_value))
                        if link.max_value is not None else ''
                    ),
                    'min_penalty_weight': (
                        str(int(link.min_penalty_weight))
                        if link.min_penalty_weight is not None else ''
                    ),
                    'max_penalty_weight': (
                        str(int(link.max_penalty_weight))
                        if link.max_penalty_weight is not None else ''
                    ),
                    'spread_violations': bool(link.spread_violations),
                }],
            })
        contract.shift_settings = settings
        contract.save(update_fields=['shift_settings', 'updated_at'])


def remove_shared_rule_from_contracts(shared_rule_id, contract_ids):
    for contract in Contract.objects.filter(id__in=contract_ids):
        contract.shift_settings = _without_shared_rule(
            contract.shift_settings, shared_rule_id,
        )
        contract.save(update_fields=['shift_settings', 'updated_at'])
