import { useEffect, useMemo, useState } from 'react'

const API_BASE = 'http://localhost:8000/api'

type Contract = { id: number; domain: number; domain_name: string; name: string; active: boolean; facility_ids: number[] }
type ShiftTemplate = { id: number; facility: number; name: string; facility_name: string; active: boolean }
type ContractSetting = { id: number; name: string; active: boolean; enabled: boolean; min_value: string; max_value: string; min_penalty_weight: string; max_penalty_weight: string; spread_violations: boolean }
type SharedRule = {
  id: number; domain: number; domain_name: string; name: string; active: boolean
  period_type: 'WEEK' | 'MONTH' | 'SCHEDULE_BLOCK'; units: 'HOURS' | 'SHIFTS'; shift_template_ids: number[]
  shift_templates: Array<{ id: number; name: string; facility_id: number; facility_name: string }>
  contracts: ContractSetting[]
}
type SettingDraft = { contract_id: number; enabled: boolean; min_value: string; max_value: string; min_penalty_weight: string; max_penalty_weight: string; spread_violations: boolean }
type FormState = {
  name: string; active: boolean; contract_settings: SettingDraft[]; shift_template_ids: number[]
  period_type: 'WEEK' | 'MONTH' | 'SCHEDULE_BLOCK'; units: 'HOURS' | 'SHIFTS'; spread_violations: boolean
}

const emptyForm = (): FormState => ({ name: '', active: true, contract_settings: [], shift_template_ids: [], period_type: 'SCHEDULE_BLOCK', units: 'SHIFTS', spread_violations: true })

async function apiError(response: Response) {
  try {
    const payload = await response.json()
    if (typeof payload?.detail === 'string') return payload.detail
    const first = Object.values(payload || {})[0]
    return Array.isArray(first) ? String(first[0]) : String(first || '')
  } catch { return '' }
}

export default function SharedRulesView({ onShowContracts }: { onShowContracts: () => void }) {
  const [rules, setRules] = useState<SharedRule[]>([])
  const [contracts, setContracts] = useState<Contract[]>([])
  const [templates, setTemplates] = useState<ShiftTemplate[]>([])
  const [view, setView] = useState<'active' | 'inactive'>('active')
  const [domainFilter, setDomainFilter] = useState('')
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [editingId, setEditingId] = useState<number | null>(null)
  const [modalOpen, setModalOpen] = useState(false)
  const [form, setForm] = useState<FormState>(emptyForm)

  const domains = useMemo(() => {
    const rows = new Map<number, string>()
    contracts.forEach((contract) => rows.set(contract.domain, contract.domain_name))
    return [...rows.entries()].sort((a, b) => a[1].localeCompare(b[1]))
  }, [contracts])
  const selectedContracts = useMemo(() => form.contract_settings.map((setting) => contracts.find((contract) => contract.id === setting.contract_id)).filter((contract): contract is Contract => Boolean(contract)), [contracts, form.contract_settings])
  const selectedDomain = selectedContracts[0]?.domain ?? null
  const availableTemplates = useMemo(() => {
    if (!selectedDomain || selectedContracts.length < 2) return []
    const facilities = new Set(selectedContracts.flatMap((contract) => contract.facility_ids))
    return templates.filter((template) => template.active && facilities.has(template.facility))
  }, [selectedContracts, selectedDomain, templates])
  const compatibility = useMemo(() => {
    const selected = templates.filter((template) => form.shift_template_ids.includes(template.id))
    return selectedContracts.map((contract) => ({
      contract,
      eligibleCount: selected.filter((template) => contract.facility_ids.includes(template.facility)).length,
      totalCount: selected.length,
    }))
  }, [form.shift_template_ids, selectedContracts, templates])

  const loadRules = async (nextView = view, nextDomain = domainFilter) => {
    const params = new URLSearchParams({ status: nextView })
    if (nextDomain) params.set('domain', nextDomain)
    const response = await fetch(`${API_BASE}/shared-rules/?${params}`, { credentials: 'include' })
    if (!response.ok) throw new Error((await apiError(response)) || 'Unable to load Shared Rules.')
    setRules(await response.json())
  }
  const loadAll = async () => {
    try {
      setLoading(true); setError(null)
      const [contractsResponse, templatesResponse] = await Promise.all([
        fetch(`${API_BASE}/contracts/?include_inactive=true`, { credentials: 'include' }),
        fetch(`${API_BASE}/shift-templates/?active=true`, { credentials: 'include' }),
      ])
      if (!contractsResponse.ok || !templatesResponse.ok) throw new Error('Unable to load Shared Rule options.')
      const [contractRows, templateRows] = await Promise.all([contractsResponse.json(), templatesResponse.json()])
      setContracts(contractRows)
      setTemplates(templateRows.map((template: ShiftTemplate) => ({ ...template, id: Number(template.id), facility: Number(template.facility), active: template.active === true || template.active === 1 })))
      await loadRules()
    } catch (loadError) { setError(loadError instanceof Error ? loadError.message : 'Unable to load Shared Rules.') }
    finally { setLoading(false) }
  }
  useEffect(() => { loadAll() }, [])
  useEffect(() => { if (!loading) loadRules(view, domainFilter).catch((loadError) => setError(loadError.message)) }, [view, domainFilter])

  const toggleContract = (contract: Contract) => setForm((current) => {
    const exists = current.contract_settings.some((row) => row.contract_id === contract.id)
    if (exists) return { ...current, contract_settings: current.contract_settings.filter((row) => row.contract_id !== contract.id), shift_template_ids: [] }
    const currentDomain = contracts.find((row) => row.id === current.contract_settings[0]?.contract_id)?.domain
    if (currentDomain && currentDomain !== contract.domain) return current
    return { ...current, contract_settings: [...current.contract_settings, { contract_id: contract.id, enabled: true, min_value: '', max_value: '', min_penalty_weight: '', max_penalty_weight: '', spread_violations: current.spread_violations }], shift_template_ids: [] }
  })
  const updateSetting = (contractId: number, values: Partial<SettingDraft>) => setForm((current) => ({ ...current, contract_settings: current.contract_settings.map((row) => row.contract_id === contractId ? { ...row, ...values } : row) }))
  const openCreate = () => { setEditingId(null); setForm(emptyForm()); setError(null); setNotice(null); setModalOpen(true) }
  const openEdit = (rule: SharedRule) => {
    setEditingId(rule.id)
    setForm({ name: rule.name, active: rule.active, period_type: rule.period_type, units: rule.units, shift_template_ids: rule.shift_templates.map((template) => template.id), spread_violations: rule.contracts.every((contract) => contract.spread_violations), contract_settings: rule.contracts.map((contract) => ({ contract_id: contract.id, enabled: contract.enabled, min_value: contract.min_value, max_value: contract.max_value, min_penalty_weight: contract.min_penalty_weight, max_penalty_weight: contract.max_penalty_weight, spread_violations: contract.spread_violations })) })
    setError(null); setNotice(null); setModalOpen(true)
  }
  const save = async () => {
    if (form.contract_settings.length < 2) return setError('Select at least two contracts first.')
    if (!form.name.trim()) return setError('Enter a Shared Rule name.')
    if (!form.shift_template_ids.length) return setError('Select at least one shift.')
    const incompatible = compatibility.filter((row) => row.eligibleCount === 0)
    if (incompatible.length) return setError(`The selected shifts contradict: ${incompatible.map((row) => row.contract.name).join(', ')}.`)
    try {
      setSaving(true); setError(null)
      const response = await fetch(editingId ? `${API_BASE}/shared-rules/${editingId}/` : `${API_BASE}/shared-rules/`, {
        method: editingId ? 'PUT' : 'POST', headers: { 'Content-Type': 'application/json' }, credentials: 'include',
        body: JSON.stringify({ domain: selectedDomain, name: form.name.trim(), active: form.active, period_type: form.period_type, units: form.units, shift_template_ids: form.shift_template_ids, contract_settings: form.contract_settings.map((row) => ({ ...row, spread_violations: form.spread_violations })) }),
      })
      if (!response.ok) throw new Error((await apiError(response)) || 'Unable to save Shared Rule.')
      setModalOpen(false); setNotice(editingId ? 'Shared Rule updated.' : 'Shared Rule created.'); await loadRules()
    } catch (saveError) { setError(saveError instanceof Error ? saveError.message : 'Unable to save Shared Rule.') }
    finally { setSaving(false) }
  }
  const setActive = async (rule: SharedRule, active: boolean) => {
    const response = await fetch(`${API_BASE}/shared-rules/${rule.id}/`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, credentials: 'include', body: JSON.stringify({ active }) })
    if (!response.ok) return setError((await apiError(response)) || 'Unable to update Shared Rule.')
    setNotice(active ? `${rule.name} reactivated.` : `${rule.name} deactivated.`); await loadRules()
  }
  const deleteRule = async (rule: SharedRule) => {
    const names = rule.contracts.map((contract) => contract.name).join(', ')
    if (!window.confirm(`Delete "${rule.name}"?\n\nIt will be removed from: ${names}.\n\nThis cannot be undone.`)) return
    const response = await fetch(`${API_BASE}/shared-rules/${rule.id}/`, { method: 'DELETE', credentials: 'include' })
    if (!response.ok) return setError((await apiError(response)) || 'Unable to delete Shared Rule.')
    setNotice(`Deleted ${rule.name}.`); await loadRules()
  }

  return <div className="facilities-view-card">
    <div className="contract-page-tabs" role="tablist"><button type="button" onClick={onShowContracts}>Contracts</button><button type="button" className="active" aria-selected="true">Shared Rules</button></div>
    <div className="facilities-header contracts-header"><div><h2>Shared Rules</h2><p className="section-help">Custom rules used across multiple contracts.</p></div><div className="contracts-toolbar">
      <div className="shared-rule-status-filter" role="radiogroup" aria-label="Shared Rule status"><label><input type="radio" checked={view === 'active'} onChange={() => setView('active')} /> Active</label><label><input type="radio" checked={view === 'inactive'} onChange={() => setView('inactive')} /> Inactive</label></div>
      <label className="facility-field contracts-domain-filter"><span>Domain</span><select value={domainFilter} onChange={(event) => setDomainFilter(event.target.value)}><option value="">All domains</option>{domains.map(([id, name]) => <option key={id} value={id}>{name}</option>)}</select></label>
      <button type="button" className="primary-action" onClick={openCreate}>Add Shared Rule</button>
    </div></div>
    {error && <div className="facilities-error">{error}</div>}{notice && <div className="contract-saved-banner">{notice}</div>}
    {loading ? <div className="scheduler-loading">Loading Shared Rules...</div> : <div className="scheduler-table-wrap"><table className="scheduler-table"><thead><tr><th>Rule</th><th>Domain</th><th>Period</th><th>Contracts using rule</th><th>Actions</th></tr></thead><tbody>{rules.map((rule) => <tr key={rule.id}>
      <td><strong>{rule.name}</strong><div className="table-subtext">{rule.shift_templates.map((row) => row.name).join(', ')}</div></td><td>{rule.domain_name}</td><td>{rule.period_type === 'SCHEDULE_BLOCK' ? 'Schedule Block' : rule.period_type === 'MONTH' ? 'Month' : 'Week'} · {rule.units === 'HOURS' ? 'Hours' : 'Shifts'}</td>
      <td><div className="shared-rule-contract-chips">{rule.contracts.map((contract) => <span key={contract.id}>{contract.name}</span>)}</div></td><td><div className="facility-actions"><button type="button" onClick={() => openEdit(rule)}>Edit</button><button type="button" onClick={() => setActive(rule, !rule.active)}>{rule.active ? 'Deactivate' : 'Reactivate'}</button><button type="button" className="danger" onClick={() => deleteRule(rule)}>Delete</button></div></td>
    </tr>)}</tbody></table>{!rules.length && <div className="empty-state">No {view} Shared Rules</div>}</div>}

    {modalOpen && <div className="shift-modal-overlay contract-modal-overlay"><div className="shift-modal schedule-block-modal shared-rule-modal" onClick={(event) => event.stopPropagation()}>
      <div className="shift-modal-header"><h2>{editingId ? 'Edit Shared Rule' : 'Add Shared Rule'}</h2></div>{error && <div className="facilities-error">{error}</div>}
      <section className="shared-rule-step"><h3>1. Select contracts</h3><p className="section-help">Select the contracts that will share this rule. All selected contracts must belong to one domain.</p><div className="shared-rule-contract-grid">{contracts.filter((contract) => contract.active).map((contract) => {
        const selected = form.contract_settings.some((row) => row.contract_id === contract.id); const disabled = Boolean(selectedDomain && selectedDomain !== contract.domain)
        return <label key={contract.id} className={disabled ? 'disabled' : ''}><input type="checkbox" checked={selected} disabled={disabled} onChange={() => toggleContract(contract)} /><span><strong>{contract.name}</strong><small>{contract.domain_name}</small></span></label>
      })}</div></section>
      <section className="shared-rule-step"><h3>2. Define the shared rule</h3><div className="shared-rule-definition-grid">
        <label className="facility-field"><span>Rule name</span><input value={form.name} disabled={selectedContracts.length < 2} onChange={(event) => setForm((current) => ({ ...current, name: event.target.value }))} /></label>
        <label className="facility-field"><span>Period</span><select value={form.period_type} disabled={selectedContracts.length < 2} onChange={(event) => setForm((current) => ({ ...current, period_type: event.target.value as FormState['period_type'] }))}><option value="WEEK">Week</option><option value="MONTH">Month</option><option value="SCHEDULE_BLOCK">Schedule Block</option></select></label>
        <label className="facility-field"><span>Units</span><select value={form.units} disabled={selectedContracts.length < 2} onChange={(event) => setForm((current) => ({ ...current, units: event.target.value as FormState['units'] }))}><option value="SHIFTS">Shifts</option><option value="HOURS">Hours</option></select></label>
        <label className="inline-checkbox-field"><span>Spread</span><input type="checkbox" checked={form.spread_violations} disabled={selectedContracts.length < 2} onChange={(event) => setForm((current) => ({ ...current, spread_violations: event.target.checked }))} /></label>
      </div><div className="shared-rule-template-grid">{availableTemplates.map((template) => <label key={template.id}><input type="checkbox" checked={form.shift_template_ids.includes(template.id)} onChange={(event) => setForm((current) => ({ ...current, shift_template_ids: event.target.checked ? [...current.shift_template_ids, template.id] : current.shift_template_ids.filter((id) => id !== template.id) }))} /><span><strong>{template.name}</strong><small>{template.facility_name}</small></span></label>)}</div>
      {form.shift_template_ids.length > 0 && <div className="shared-rule-compatibility">{compatibility.map((row) => <div key={row.contract.id} className={row.eligibleCount ? (row.eligibleCount === row.totalCount ? 'compatible' : 'partial') : 'incompatible'}><strong>{row.contract.name}</strong><span>{row.eligibleCount === 0 ? 'Incompatible' : row.eligibleCount === row.totalCount ? 'Compatible' : `Compatible with ${row.eligibleCount} of ${row.totalCount} shifts`}</span></div>)}</div>}</section>
      {selectedContracts.length >= 2 && <section className="shared-rule-step"><h3>3. Contract limits</h3><div className="scheduler-table-wrap"><table className="scheduler-table compact"><thead><tr><th>Contract</th><th>Minimum</th><th>Maximum</th><th>Min penalty</th><th>Max penalty</th></tr></thead><tbody>{form.contract_settings.map((setting) => {
        const contract = contracts.find((row) => row.id === setting.contract_id)
        return <tr key={setting.contract_id}><td>{contract?.name}</td><td><input type="number" min="0" step="1" value={setting.min_value} onChange={(event) => updateSetting(setting.contract_id, { min_value: event.target.value })} /></td><td><input type="number" min="0" step="1" value={setting.max_value} onChange={(event) => updateSetting(setting.contract_id, { max_value: event.target.value })} /></td><td><input type="number" min="0" step="1" value={setting.min_penalty_weight} onChange={(event) => updateSetting(setting.contract_id, { min_penalty_weight: event.target.value })} /></td><td><input type="number" min="0" step="1" value={setting.max_penalty_weight} onChange={(event) => updateSetting(setting.contract_id, { max_penalty_weight: event.target.value })} /></td></tr>
      })}</tbody></table></div></section>}
      <div className="shift-modal-actions"><button type="button" onClick={() => setModalOpen(false)}>Cancel</button><button type="button" className="primary-action" disabled={saving} onClick={save}>{saving ? 'Saving...' : 'Save Shared Rule'}</button></div>
    </div></div>}
  </div>
}
