import { useEffect, useMemo, useState } from 'react'
import { API_BASE } from '../api'
import { readSessionString, writeSessionSelection } from '../utils/sessionSelection'

type Domain = { id: number; name: string; region: number; region_name: string }
type OverrideRow = {
  id: number
  physician_id: number
  user_name: string
  first_name: string
  last_name: string
  contract_id: number
  contract_name: string
  domain_id: number
  domain_name: string
  region_id: number
  region_name: string
  override_count: number
}
type RuleRow = {
  path: string
  section: string
  label: string
  parent_value: string | number | boolean | null
  effective_value: string | number | boolean | null
  overridden: boolean
}
type OverrideDetail = OverrideRow & {
  rules: RuleRow[]
  facilities: { id: number; name: string }[]
}

type OverrideTab = 'summary' | 'workload_settings' | 'facility_settings' | 'shift_settings' | 'night_settings' | 'weekend_settings' | 'request_settings'

const sectionLabels: Record<string, string> = {
  workload_settings: 'Workload Rules',
  facility_settings: 'Facility Rules',
  shift_settings: 'Shift Rules',
  night_settings: 'Night Rules',
  weekend_settings: 'Weekend Rules',
  request_settings: 'Request Rules',
}

const overrideTabs: { id: OverrideTab; label: string }[] = [
  { id: 'summary', label: 'Summary' },
  { id: 'workload_settings', label: 'Workload' },
  { id: 'facility_settings', label: 'Facilities' },
  { id: 'shift_settings', label: 'Shifts' },
  { id: 'night_settings', label: 'Nights' },
  { id: 'weekend_settings', label: 'Weekends' },
  { id: 'request_settings', label: 'Requests' },
]

async function apiError(response: Response) {
  try {
    const payload = await response.json()
    if (typeof payload?.detail === 'string') return payload.detail
    const first = Object.values(payload || {})[0]
    return Array.isArray(first) ? String(first[0]) : String(first || '')
  } catch {
    return ''
  }
}

function displayValue(value: RuleRow['parent_value']) {
  if (typeof value === 'boolean') return value ? 'Yes' : 'No'
  if (value === '' || value === null || value === undefined) return 'Not set'
  return String(value)
}

function titleCase(value: string) {
  return value.replace(/_/g, ' ').replace(/\b\w/g, (character) => character.toUpperCase())
}

function ruleField(rule: RuleRow) {
  return rule.path.split('.').at(-1) || ''
}

function ruleGroup(rule: RuleRow) {
  const parts = rule.path.split('.')
  const field = parts.at(-1) || ''
  const parentPath = parts.slice(0, -1).join('.')
  if (parentPath.includes('.period_rules.')) {
    return { key: `${parentPath}.range`, title: rule.label.split(' · ')[0] || 'Period Rule' }
  }
  if (rule.section === 'request_settings') {
    if (field.startsWith('allow_')) return { key: `${parentPath}.allowed`, title: 'Allowed Request Types' }
    const requestType = field.match(/^weight_(.+)$/)?.[1]
      || field.match(/^(.+)_request_limit$/)?.[1]
      || (field === 'low_request_unlimited' ? 'low' : '')
    if (requestType) return { key: `${parentPath}.${requestType}`, title: `${titleCase(requestType)} Requests` }
  }
  if (['min_value', 'max_value', 'min_penalty_weight', 'max_penalty_weight'].includes(field)) {
    return { key: `${parentPath}.range`, title: rule.label.split(' · ')[0] || 'Range' }
  }
  let base = field.replace(/_penalty_weight$/, '').replace(/^(min|max)_/, '')
  if (base.startsWith('time_off')) base = 'time_off'
  if (base === 'circadian_enabled') base = 'circadian'
  return { key: `${parentPath}.${base}`, title: titleCase(base) }
}

function compactRuleLabel(rule: RuleRow, groupTitle: string) {
  const field = ruleField(rule)
  if (rule.section === 'request_settings') {
    if (field.startsWith('weight_')) return 'Request Weight'
    if (field.endsWith('_request_limit')) return 'Request Limit'
    if (field === 'low_request_unlimited') return 'Unlimited Requests'
  }
  const contextPrefix = `${groupTitle} · `
  const label = rule.label.startsWith(contextPrefix) ? rule.label.slice(contextPrefix.length) : rule.label
  return label
    .replace(/^Min /, 'Minimum ')
    .replace(/^Max /, 'Maximum ')
    .replace(/ Penalty Weight$/, ' Penalty')
}

function ruleSortValue(rule: RuleRow) {
  const field = ruleField(rule)
  const requestFieldOrder: Record<string, number> = {
    allow_day_off: 0,
    allow_shift_off: 1,
    allow_day_on: 2,
    allow_shift_on: 3,
    low_request_limit: 0,
    low_request_unlimited: 1,
  }
  if (field in requestFieldOrder) return requestFieldOrder[field]
  if (field.startsWith('min_')) return 0
  if (field.startsWith('max_')) return 1
  return 2
}

function ruleGroupSortValue(section: string, title: string) {
  if (section !== 'request_settings') return 0
  const requestGroupOrder = [
    'Allowed Request Types',
    'Low Requests',
    'Medium Requests',
    'High Requests',
    'Fixed Requests',
    'Weekend Requests',
  ]
  const index = requestGroupOrder.indexOf(title)
  return index === -1 ? requestGroupOrder.length : index
}

function isSecondaryRule(rule: RuleRow) {
  const field = ruleField(rule)
  return field.endsWith('_penalty_weight') || (rule.section === 'request_settings' && field.startsWith('weight_'))
}

export function UserOverrideModal({
  assignmentId,
  onClose,
  onSaved,
}: {
  assignmentId: number
  onClose: () => void
  onSaved: () => void
}) {
  const [detail, setDetail] = useState<OverrideDetail | null>(null)
  const [draft, setDraft] = useState<Record<string, string | number | boolean | null>>({})
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [activeTab, setActiveTab] = useState<OverrideTab>('summary')

  const load = async () => {
    setError(null)
    const response = await fetch(`${API_BASE}/contract-user-overrides/${assignmentId}/`, { credentials: 'include' })
    if (!response.ok) throw new Error((await apiError(response)) || 'Unable to load this user contract.')
    const data = await response.json() as OverrideDetail
    setDetail(data)
    setDraft(Object.fromEntries(data.rules.filter((rule) => rule.overridden).map((rule) => [rule.path, rule.effective_value])))
  }

  useEffect(() => {
    load().catch((loadError) => setError(loadError instanceof Error ? loadError.message : 'Unable to load this user contract.'))
  }, [assignmentId])

  const activeRules = useMemo(
    () => detail?.rules.filter((rule) => rule.section === activeTab) || [],
    [activeTab, detail],
  )

  const activeRuleGroups = useMemo(() => {
    const groups = new Map<string, { title: string; rules: RuleRow[] }>()
    activeRules.forEach((rule) => {
      const group = ruleGroup(rule)
      const existing = groups.get(group.key)
      if (existing) existing.rules.push(rule)
      else groups.set(group.key, { title: group.title, rules: [rule] })
    })
    return [...groups.entries()]
      .map(([key, group]) => ({
        key,
        title: group.title,
        valueRules: group.rules.filter((rule) => !isSecondaryRule(rule)).sort((a, b) => ruleSortValue(a) - ruleSortValue(b)),
        penaltyRules: group.rules.filter(isSecondaryRule).sort((a, b) => ruleSortValue(a) - ruleSortValue(b)),
      }))
      .sort((a, b) => ruleGroupSortValue(activeTab, a.title) - ruleGroupSortValue(activeTab, b.title))
  }, [activeRules, activeTab])

  const overrideCountsBySection = useMemo(() => {
    const counts = new Map<string, number>()
    detail?.rules.forEach((rule) => {
      if (Object.prototype.hasOwnProperty.call(draft, rule.path)) counts.set(rule.section, (counts.get(rule.section) || 0) + 1)
    })
    return counts
  }, [detail, draft])

  const setOverride = (rule: RuleRow, value: string | boolean | null) => {
    setDraft((current) => {
      const next = { ...current }
      if (value === null || value === '') delete next[rule.path]
      else next[rule.path] = value
      return next
    })
  }

  const save = async () => {
    try {
      setSaving(true)
      setError(null)
      const response = await fetch(`${API_BASE}/contract-user-overrides/${assignmentId}/`, {
        method: 'PATCH',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ overrides: draft }),
      })
      if (!response.ok) throw new Error((await apiError(response)) || 'Unable to save user overrides.')
      const data = await response.json() as OverrideDetail
      setDetail(data)
      setDraft(Object.fromEntries(data.rules.filter((rule) => rule.overridden).map((rule) => [rule.path, rule.effective_value])))
      onSaved()
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : 'Unable to save user overrides.')
    } finally {
      setSaving(false)
    }
  }

  return <div className="shift-modal-overlay contract-modal-overlay">
    <div className="shift-modal schedule-block-modal contract-modal user-override-modal" onClick={(event) => event.stopPropagation()}>
      <div className="shift-modal-header user-override-header">
        <div className="user-override-identity">
          <span className="user-override-eyebrow">User contract override</span>
          <h2>{detail?.user_name || 'Loading user...'}</h2>
          {detail && <p>Parent contract: <strong>{detail.contract_name}</strong> · {detail.domain_name}</p>}
        </div>
        {detail && <span className="contract-override-badge">{Object.keys(draft).length} {Object.keys(draft).length === 1 ? 'rule' : 'rules'} modified</span>}
      </div>
      {error && <div className="facilities-error">{error}</div>}
      {!detail && !error && <div className="scheduler-loading">Loading user contract...</div>}
      {detail && <>
        <div className="schedule-block-modal-tabs user-override-tabs" role="tablist">
          {overrideTabs.map((tab) => {
            const overrideCount = overrideCountsBySection.get(tab.id) || 0
            return <button
              key={tab.id}
              type="button"
              className={`${activeTab === tab.id ? 'active' : ''}${overrideCount ? ' has-override' : ''}`}
              aria-selected={activeTab === tab.id}
              onClick={() => setActiveTab(tab.id)}
            >{tab.label}{overrideCount > 0 && <span className="user-override-tab-count">{overrideCount}</span>}</button>
          })}
        </div>
        <div className="shift-modal-body user-override-body">
          {activeTab === 'summary' && <div className="contract-section-stack">
            <h3>{detail.user_name}'s Contract</h3>
            <div className="request-existing-note user-override-guidance">
              Only values entered in a User override box replace the parent contract. Blank override fields continue to inherit the gray default shown beside them.
            </div>
            <div className="contract-summary-grid">
              <div className="request-existing-note"><strong>Parent contract</strong><span>{detail.contract_name}</span></div>
              <div className="request-existing-note"><strong>Region</strong><span>{detail.region_name}</span></div>
              <div className="request-existing-note"><strong>Domain</strong><span>{detail.domain_name}</span></div>
              <div className="request-existing-note"><strong>Active overrides</strong><span>{Object.keys(draft).length}</span></div>
              <div className="request-existing-note"><strong>Facilities inherited</strong><span>{detail.facilities.length}</span></div>
            </div>
          </div>
          }
          {activeTab !== 'summary' && <section className="contract-section-stack user-override-section">
            <h3>{sectionLabels[activeTab] || activeTab}</h3>
            <div className="request-existing-note">{activeTab === 'facility_settings' ? `Facility eligibility, periods, and units remain inherited from ${detail.contract_name}. ` : ''}Gray values are the current contract defaults. Enter only the values that should be different for {detail.user_name}.</div>
            <div className="user-override-rule-groups">
              {activeRuleGroups.map((group) => <div key={group.key} className="user-override-rule-group">
                <h4>{group.title}</h4>
                <div className={`user-override-group-columns${group.valueRules.length && group.penaltyRules.length ? '' : ' single-column'}`}>
                  {[group.valueRules, group.penaltyRules].map((rules, columnIndex) => rules.length > 0 && <div key={columnIndex} className="user-override-rule-column">
                    <div className="user-override-column-label">{columnIndex === 0 ? 'Rule values' : activeTab === 'request_settings' ? 'Request weight' : 'Corresponding penalties'}</div>
                    {rules.map((rule) => {
                      const overridden = Object.prototype.hasOwnProperty.call(draft, rule.path)
                      const overrideValue = overridden ? draft[rule.path] : ''
                      const isBoolean = typeof rule.parent_value === 'boolean'
                      return <div key={rule.path} className={`user-override-rule-card${overridden ? ' active' : ''}`}>
                        <div className="user-override-rule-heading">
                          <strong>{compactRuleLabel(rule, group.title)}</strong>
                          {overridden && <span>Override active</span>}
                        </div>
                        <div className="user-override-value-pair">
                          <label className="user-override-default-field">
                            <span>Contract default</span>
                            <div>{displayValue(rule.parent_value)}</div>
                          </label>
                          <label className="facility-field user-override-entry-field">
                            <span>User override</span>
                            {isBoolean
                              ? <select value={overridden ? String(Boolean(overrideValue)) : ''} onChange={(event) => setOverride(rule, event.target.value === '' ? null : event.target.value === 'true')}>
                                <option value="">Use contract default</option>
                                <option value="true">Yes</option>
                                <option value="false">No</option>
                              </select>
                              : <input type="number" min="0" step="any" value={String(overrideValue ?? '')} placeholder={`Use default: ${displayValue(rule.parent_value)}`} onChange={(event) => setOverride(rule, event.target.value)} />}
                          </label>
                        </div>
                      </div>
                    })}
                  </div>)}
                </div>
              </div>)}
            </div>
            {!activeRules.length && <div className="empty-state">No individual override fields are available in this section.</div>}
          </section>}
        </div>
      </>}
      <div className="shift-modal-actions">
        <button type="button" className="secondary" onClick={onClose}>Close</button>
        <button type="button" className="secondary" disabled={!detail || saving} onClick={() => setDraft({})}>Restore All Defaults</button>
        <button type="button" disabled={!detail || saving} onClick={save}>{saving ? 'Saving...' : 'Save User Overrides'}</button>
      </div>
    </div>
  </div>
}

export default function UserOverridesView({
  onShowContracts,
  onShowSharedRules,
}: {
  onShowContracts: () => void
  onShowSharedRules: () => void
}) {
  const [rows, setRows] = useState<OverrideRow[]>([])
  const [domains, setDomains] = useState<Domain[]>([])
  const [regionFilter, setRegionFilter] = useState(() => readSessionString('atlas.contracts.region'))
  const [domainFilter, setDomainFilter] = useState(() => readSessionString('atlas.contracts.domain'))
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [editingId, setEditingId] = useState<number | null>(null)

  const regions = useMemo(() => Array.from(new Map(domains.map((domain) => [domain.region, domain.region_name])).entries()), [domains])
  const domainsForRegion = useMemo(() => domains.filter((domain) => !regionFilter || String(domain.region) === regionFilter), [domains, regionFilter])

  const loadRows = async (domain = domainFilter, region = regionFilter) => {
    const params = new URLSearchParams()
    if (domain) params.set('domain', domain)
    else if (region) params.set('region', region)
    const response = await fetch(`${API_BASE}/contract-user-overrides/?${params}`, { credentials: 'include' })
    if (!response.ok) throw new Error((await apiError(response)) || 'Unable to load user overrides.')
    setRows(await response.json())
  }

  useEffect(() => {
    Promise.all([
      fetch(`${API_BASE}/domains/?active=true&permission=manage_build_workspace`, { credentials: 'include' }).then(async (response) => {
        if (!response.ok) throw new Error('Unable to load domains.')
        setDomains(await response.json())
      }),
      loadRows(),
    ]).catch((loadError) => setError(loadError instanceof Error ? loadError.message : 'Unable to load user overrides.')).finally(() => setLoading(false))
  }, [])

  useEffect(() => {
    if (!loading) loadRows(domainFilter, regionFilter).catch((loadError) => setError(loadError.message))
  }, [domainFilter, regionFilter])
  useEffect(() => writeSessionSelection('atlas.contracts.region', regionFilter), [regionFilter])
  useEffect(() => writeSessionSelection('atlas.contracts.domain', domainFilter), [domainFilter])

  return <div className="facilities-view-card">
    <div className="contract-page-tabs" role="tablist">
      <button type="button" onClick={onShowContracts}>Contracts</button>
      <button type="button" onClick={onShowSharedRules}>Shared Rules</button>
      <button type="button" className="active" aria-selected="true">User Overrides</button>
    </div>
    <div className="facilities-header contracts-header">
      <div><h2>User Overrides</h2><p className="section-help">Users whose rules differ from their parent contract.</p></div>
      <div className="contracts-toolbar">
        {regions.length > 1 && <label className="facility-field contracts-domain-filter"><span>Region</span><select value={regionFilter} onChange={(event) => { setRegionFilter(event.target.value); setDomainFilter('') }}>{regions.map(([id, name]) => <option key={id} value={id}>{name}</option>)}</select></label>}
        {domainsForRegion.length > 1 && <label className="facility-field contracts-domain-filter"><span>Domain</span><select value={domainFilter} onChange={(event) => setDomainFilter(event.target.value)}><option value="">All domains</option>{domainsForRegion.map((domain) => <option key={domain.id} value={domain.id}>{domain.name}</option>)}</select></label>}
      </div>
    </div>
    {error && <div className="facilities-error">{error}</div>}
    {loading ? <div className="scheduler-loading">Loading user overrides...</div> : <div className="scheduler-table-wrap">
      <table className="scheduler-table"><thead><tr><th>User</th><th>Parent Contract</th><th>Domain</th><th>Rules Modified</th></tr></thead>
        <tbody>{rows.map((row) => <tr key={row.id}><td><button type="button" className="link-button" onClick={() => setEditingId(row.id)}>{row.user_name}</button></td><td>{row.contract_name}</td><td>{row.domain_name}</td><td>{row.override_count}</td></tr>)}</tbody>
      </table>
    </div>}
    {!loading && !rows.length && <div className="empty-state">No user overrides in this scope.</div>}
    {editingId !== null && <UserOverrideModal assignmentId={editingId} onClose={() => setEditingId(null)} onSaved={() => loadRows()} />}
  </div>
}

