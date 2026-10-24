import React, { useEffect, useMemo, useState } from 'react'
import { useLocation } from 'react-router-dom'
import { API_BASE } from '../api'

type PlanRow = {
  physician_id: number
  name: string
  contract_name: string
  contract_updated_at: string
  previous_hours: string
  previous_shifts: number | ''
  target_hours?: string
  min_hours_adjustment?: string
  max_hours_adjustment?: string
  minimum_hours?: string
  maximum_hours?: string
  target_shifts?: string
  min_shifts_adjustment?: string
  max_shifts_adjustment?: string
  minimum_shifts?: string
  maximum_shifts?: string
  normalization_note?: string
  partial_block: boolean
  excluded: boolean
  parent_contract_changed?: boolean
  parent_defaults?: Partial<Record<NumericPlanKey, string>>
  workload_adjusted?: boolean
  workload_adjustment_base_values?: Partial<Record<NumericPlanKey, string>>
  apply_workload_adjustment?: boolean
  clear_workload_adjustment?: boolean
  carried_from_partial_block?: boolean
  carry_confirmation_required?: boolean
  carry_forward_confirmed?: boolean
}

type WorkloadRecommendation = {
  direction: 'increase_maximum' | 'decrease_minimum'
  required_adjustment_hours: number
  recommended_target_change_percent: number | null
  adjustment_hours_per_fte: number | null
  proposals: Array<{
    physician_id: number
    physician: string
    current_target_hours: number
    current_minimum_hours: number
    current_maximum_hours: number | null
    adjustment_hours: number
    proposed_target_hours: number
    proposed_minimum_hours: number
    proposed_maximum_hours: number | null
  }>
}

type BlockPlanResponse = {
  schedule_block: {
    id: number
    name?: string
    generated_name?: string
    start_date: string
    end_date: string
    build_status: string
  }
  read_only: boolean
  previous_block: null | { id: number; name: string; months: number }
  planning_source_block: null | { id: number; name: string; months: number }
  current_block_months: number
  users: PlanRow[]
}

type PartialRule = {
  path: string
  section: string
  label: string
  parent_value: string | number | boolean | null
  effective_value: string | number | boolean | null
  atlas_adjusted: boolean
  overridden: boolean
}

type PartialResponse = {
  physician_id: number
  user_name: string
  contract_name: string
  schedule_block: BlockPlanResponse['schedule_block']
  read_only: boolean
  unavailable_dates: string[]
  block_values: Record<string, string>
  rules: PartialRule[]
  adjusted_sections: string[]
  manual_override_count: number
}

type Props = { blockId: number; onBack: () => void }
type NumericPlanKey = 'target_hours' | 'min_hours_adjustment' | 'max_hours_adjustment' | 'minimum_hours' | 'maximum_hours' | 'target_shifts' | 'min_shifts_adjustment' | 'max_shifts_adjustment' | 'minimum_shifts' | 'maximum_shifts'

const numericKeys: NumericPlanKey[] = [
  'target_hours', 'min_hours_adjustment', 'max_hours_adjustment', 'minimum_hours', 'maximum_hours',
  'target_shifts', 'min_shifts_adjustment', 'max_shifts_adjustment', 'minimum_shifts', 'maximum_shifts',
]

function displayError(error: unknown) {
  return error instanceof Error ? error.message : 'Unable to complete the request.'
}

async function jsonOrError(response: Response) {
  const data = await response.json().catch(() => ({}))
  if (!response.ok) {
    const detail = data?.detail || Object.entries(data).map(([key, value]) => `${key}: ${value}`).join(' ')
    throw new Error(detail || 'Unable to complete the request.')
  }
  return data
}

function isoDates(start: string, end: string) {
  const dates: string[] = []
  const cursor = new Date(`${start}T00:00:00Z`)
  const last = new Date(`${end}T00:00:00Z`)
  while (cursor <= last) {
    dates.push(cursor.toISOString().slice(0, 10))
    cursor.setUTCDate(cursor.getUTCDate() + 1)
  }
  return dates
}

function monthLabel(value: string) {
  return new Date(`${value.slice(0, 7)}-01T00:00:00Z`).toLocaleDateString('en-US', { month: 'long', year: 'numeric', timeZone: 'UTC' })
}

function formatNumber(value: string | number | undefined) {
  return value === '' || value === undefined ? '—' : String(value)
}

function sectionLabel(section: string) {
  return ({
    workload_settings: 'Workload', facility_settings: 'Facilities',
    shift_settings: 'Shifts', night_settings: 'Nights',
    weekend_settings: 'Weekends', request_settings: 'Requests',
  } as Record<string, string>)[section] || section.replace(/_/g, ' ')
}

export default function ScheduleBlockUsersView({ blockId, onBack }: Props) {
  const location = useLocation()
  const [data, setData] = useState<BlockPlanResponse | null>(null)
  const [rows, setRows] = useState<PlanRow[]>([])
  const [selected, setSelected] = useState<Set<number>>(new Set())
  const [tab, setTab] = useState<'hours' | 'partial'>('hours')
  const [adjustments, setAdjustments] = useState({ minHours: '5', maxHours: '5', minShifts: '1', maxShifts: '1' })
  const [partial, setPartial] = useState<PartialResponse | null>(null)
  const [partialTab, setPartialTab] = useState('availability')
  const [unavailable, setUnavailable] = useState<Set<string>>(new Set())
  const [overrides, setOverrides] = useState<Record<string, string | number | boolean | null>>({})
  const [partialBlockValues, setPartialBlockValues] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [workloadRecommendation, setWorkloadRecommendation] = useState<WorkloadRecommendation | null>(null)
  const [workloadRecommendationStaged, setWorkloadRecommendationStaged] = useState(false)

  const load = async () => {
    setError('')
    const response = await fetch(`${API_BASE}/schedule-blocks/${blockId}/users/`)
    const result = await jsonOrError(response) as BlockPlanResponse
    setData(result)
    setRows(result.users)
    setSelected(new Set(result.users.map((row) => row.physician_id)))
  }

  useEffect(() => { void load().catch((reason) => setError(displayError(reason))) }, [blockId])

  useEffect(() => {
    const params = new URLSearchParams(location.search)
    const versionId = params.get('workload_version_id')
    if (!versionId) {
      setWorkloadRecommendation(null)
      return
    }
    const optimizerRunId = params.get('optimizer_run_id')
    const query = optimizerRunId ? `?optimizer_run_id=${encodeURIComponent(optimizerRunId)}` : ''
    void fetch(`${API_BASE}/schedule-versions/${versionId}/workload-feasibility/${query}`)
      .then(jsonOrError)
      .then((result) => setWorkloadRecommendation(result.fte_adjustment_preview || null))
      .catch((reason) => setError(displayError(reason)))
  }, [location.search])

  const updateRow = (physicianId: number, key: NumericPlanKey, value: string) => {
    setRows((current) => current.map((row) => row.physician_id === physicianId ? {
      ...row,
      [key]: value,
      ...(row.carry_confirmation_required && key.includes('hours')
        ? { carry_forward_confirmed: true, carry_confirmation_required: false }
        : {}),
    } : row))
  }

  const confirmCarriedHours = (physicianId: number) => {
    setRows((current) => current.map((row) => row.physician_id === physicianId ? {
      ...row,
      carry_forward_confirmed: true,
      carry_confirmation_required: false,
    } : row))
  }

  const resetRowToParent = (physicianId: number) => {
    setRows((current) => current.map((row) => row.physician_id === physicianId ? { ...row, ...(row.parent_defaults || {}), parent_contract_changed: false, carry_forward_confirmed: true, carry_confirmation_required: false } : row))
  }

  const autofill = (kind: 'hours' | 'shifts' | 'both') => {
    setError('')
    try {
      const next = rows.map((row) => {
        if (!selected.has(row.physician_id)) return row
        const copy = { ...row }
        const fill = (prefix: 'hours' | 'shifts', lowerText: string, upperText: string) => {
          const target = Number(copy[`target_${prefix}`])
          const lower = Number(lowerText)
          const upper = Number(upperText)
          if (![target, lower, upper].every(Number.isFinite) || target < 0 || lower < 0 || upper < 0 || target - lower < 0) {
            throw new Error(`${copy.name}: enter a ${prefix} target at least as large as the selected lower range.`)
          }
          copy[`min_${prefix}_adjustment`] = String(lower)
          copy[`max_${prefix}_adjustment`] = String(upper)
          copy[`minimum_${prefix}`] = String(target - lower)
          copy[`maximum_${prefix}`] = String(target + upper)
        }
        if (kind === 'hours' || kind === 'both') fill('hours', adjustments.minHours, adjustments.maxHours)
        if (kind === 'shifts' || kind === 'both') fill('shifts', adjustments.minShifts, adjustments.maxShifts)
        if ((kind === 'hours' || kind === 'both') && copy.carry_confirmation_required) {
          copy.carry_forward_confirmed = true
          copy.carry_confirmation_required = false
        }
        return copy
      })
      setRows(next)
    } catch (reason) { setError(displayError(reason)) }
  }

  const stageWorkloadRecommendation = () => {
    if (!workloadRecommendation) return
    const proposals = new Map(
      workloadRecommendation.proposals.map((proposal) => [proposal.physician_id, proposal]),
    )
    setRows((current) => current.map((row) => {
      const proposal = proposals.get(row.physician_id)
      if (!proposal || !selected.has(row.physician_id)) return row
      const base = row.workload_adjustment_base_values && Object.keys(row.workload_adjustment_base_values).length
        ? row.workload_adjustment_base_values
        : Object.fromEntries(([
          'target_hours', 'min_hours_adjustment', 'max_hours_adjustment',
          'minimum_hours', 'maximum_hours',
        ] as NumericPlanKey[]).map((key) => [key, row[key] ?? '']))
      const target = String(Math.round(proposal.proposed_target_hours))
      const minimum = String(Math.round(proposal.proposed_minimum_hours))
      const maximum = proposal.proposed_maximum_hours === null
        ? ''
        : String(Math.round(proposal.proposed_maximum_hours))
      return {
        ...row,
        target_hours: target,
        minimum_hours: minimum,
        maximum_hours: maximum,
        min_hours_adjustment: String(Number(target) - Number(minimum)),
        max_hours_adjustment: maximum === '' ? '' : String(Number(maximum) - Number(target)),
        workload_adjusted: true,
        workload_adjustment_base_values: base,
        apply_workload_adjustment: true,
        clear_workload_adjustment: false,
        carry_forward_confirmed: true,
        carry_confirmation_required: false,
      }
    }))
    setWorkloadRecommendationStaged(true)
  }

  const resetWorkloadAdjustment = (physicianId: number) => {
    setRows((current) => current.map((row) => {
      if (row.physician_id !== physicianId || !row.workload_adjustment_base_values) return row
      return {
        ...row,
        ...row.workload_adjustment_base_values,
        workload_adjusted: false,
        workload_adjustment_base_values: {},
        apply_workload_adjustment: false,
        clear_workload_adjustment: true,
      }
    }))
  }

  const saveRows = async () => {
    setBusy(true); setError('')
    try {
      const payload = rows.map((row) => Object.fromEntries([
        ['physician_id', row.physician_id],
        ...numericKeys.map((key) => [key, row[key] ?? '']),
        ['apply_workload_adjustment', Boolean(row.apply_workload_adjustment)],
        ['clear_workload_adjustment', Boolean(row.clear_workload_adjustment)],
        ['workload_adjustment_base_values', row.workload_adjustment_base_values || {}],
        ['carry_forward_confirmed', Boolean(row.carry_forward_confirmed)],
      ]))
      await jsonOrError(await fetch(`${API_BASE}/schedule-blocks/${blockId}/users/`, {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ users: payload }),
      }))
      await load()
      setWorkloadRecommendationStaged(false)
    } catch (reason) { setError(displayError(reason)) } finally { setBusy(false) }
  }

  const openPartial = async (physicianId: number) => {
    setBusy(true); setError('')
    try {
      const result = await jsonOrError(await fetch(`${API_BASE}/schedule-blocks/${blockId}/users/${physicianId}/partial/`)) as PartialResponse
      setPartial(result)
      setUnavailable(new Set(result.unavailable_dates))
      setPartialBlockValues(result.block_values || {})
      setOverrides(Object.fromEntries(result.rules.filter((rule) => rule.overridden).map((rule) => [rule.path, rule.effective_value])))
      setPartialTab('availability')
    } catch (reason) { setError(displayError(reason)) } finally { setBusy(false) }
  }

  const savePartial = async (review = false) => {
    if (!partial) return
    setBusy(true); setError('')
    try {
      const result = await jsonOrError(await fetch(`${API_BASE}/schedule-blocks/${blockId}/users/${partial.physician_id}/partial/`, {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(review
          ? { unavailable_dates: [...unavailable].sort(), overrides }
          : { unavailable_dates: [...unavailable].sort(), overrides, block_values: partialBlockValues }),
      })) as PartialResponse
      setPartial(result); setUnavailable(new Set(result.unavailable_dates)); setPartialBlockValues(result.block_values || {})
      if (review) setPartialTab('summary')
      await load()
    } catch (reason) { setError(displayError(reason)) } finally { setBusy(false) }
  }

  const deletePartial = async () => {
    if (!partial || !window.confirm(`Remove the Temporary Partial Block Contract for ${partial.user_name}?`)) return
    setBusy(true); setError('')
    try {
      await jsonOrError(await fetch(`${API_BASE}/schedule-blocks/${blockId}/users/${partial.physician_id}/partial/`, { method: 'DELETE' }))
      setPartial(null); await load()
    } catch (reason) { setError(displayError(reason)) } finally { setBusy(false) }
  }

  const monthGroups = useMemo(() => {
    if (!partial) return []
    const groups = new Map<string, string[]>()
    isoDates(partial.schedule_block.start_date, partial.schedule_block.end_date).forEach((date) => {
      const month = date.slice(0, 7)
      groups.set(month, [...(groups.get(month) || []), date])
    })
    return [...groups.entries()]
  }, [partial])

  const partialUserRows = useMemo(
    () => [
      ...rows.filter((row) => row.partial_block),
      ...rows.filter((row) => !row.partial_block),
    ],
    [rows],
  )

  if (!data) return <div className="facilities-view-card">{error || 'Loading Schedule Block users…'}</div>

  return <div className="facilities-view-card block-users-view">
    <div className="facilities-header">
      <div><h2>{data.schedule_block.generated_name || data.schedule_block.name} · Users</h2><p className="muted">Block-specific planning only · {data.schedule_block.start_date} through {data.schedule_block.end_date}</p></div>
      <button type="button" onClick={onBack}>Back to Schedule Blocks</button>
    </div>
    {data.read_only && <div className="block-plan-notice">This published or Preview block is read only.</div>}
    {error && <div className="facilities-error">{error}</div>}
    <div className="block-plan-tabs">
      <button className={tab === 'hours' ? 'active' : ''} onClick={() => setTab('hours')}>Block Hours</button>
      <button className={tab === 'partial' ? 'active' : ''} onClick={() => setTab('partial')}>Partial Block Users</button>
    </div>

    {tab === 'hours' ? <>
      {workloadRecommendation && (
        <div className="block-workload-recommendation">
          <div>
            <strong>Workload feasibility recommendation</strong>
            <span>
              {workloadRecommendation.direction === 'decrease_minimum' ? 'Lower' : 'Raise'} Block Target Hours proportionally by{' '}
              {workloadRecommendation.required_adjustment_hours.toFixed(1)} aggregate hours. Existing hour-range widths are preserved.
            </span>
          </div>
          <button
            type="button"
            className="primary-action"
            disabled={data.read_only || workloadRecommendationStaged}
            onClick={stageWorkloadRecommendation}
          >
            {workloadRecommendationStaged ? 'Proposed Values Ready' : 'Preview Suggested Targets'}
          </button>
          {workloadRecommendationStaged && <small>Review the highlighted Target, Min, and Max values, then save Block Values.</small>}
        </div>
      )}
      <div className="block-plan-toolbar">
        <button type="button" onClick={() => setSelected(new Set(rows.map((row) => row.physician_id)))}>Select All</button>
        <button type="button" onClick={() => setSelected(new Set())}>Clear All</button>
        <span className="toolbar-separator" />
        <label>Hours −<input value={adjustments.minHours} onChange={(event) => setAdjustments({ ...adjustments, minHours: event.target.value })} /></label>
        <label>Hours +<input value={adjustments.maxHours} onChange={(event) => setAdjustments({ ...adjustments, maxHours: event.target.value })} /></label>
        <button type="button" onClick={() => autofill('hours')} disabled={data.read_only}>Autofill Hours</button>
        <label>Shifts −<input value={adjustments.minShifts} onChange={(event) => setAdjustments({ ...adjustments, minShifts: event.target.value })} /></label>
        <label>Shifts +<input value={adjustments.maxShifts} onChange={(event) => setAdjustments({ ...adjustments, maxShifts: event.target.value })} /></label>
        <button type="button" onClick={() => autofill('shifts')} disabled={data.read_only}>Autofill Shifts</button>
        <button type="button" className="primary-action" onClick={() => autofill('both')} disabled={data.read_only}>Autofill Both</button>
      </div>
      {rows.some((row) => row.carry_confirmation_required) && <div className="block-plan-notice">One or more users were partial in the preceding block. Confirm or adjust each highlighted user's full-block hours before saving.</div>}
      {!data.previous_block && <div className="block-plan-notice">No earlier published block is available. Targets and ranges begin with the effective parent Contract.</div>}
      <div className="scheduler-table-wrap"><table className="scheduler-table block-plan-table"><thead><tr>
        <th></th><th>User / Contract</th><th>Target hrs</th><th>Min hrs</th><th>Max hrs</th><th>Previous hrs</th><th>Target shifts</th><th>Min shifts</th><th>Max shifts</th><th>Previous shifts</th>
      </tr></thead><tbody>{rows.map((row) => <tr key={row.physician_id} className={row.partial_block ? 'partial-row' : ''}>
        <td><input type="checkbox" checked={selected.has(row.physician_id)} onChange={(event) => setSelected((current) => { const next = new Set(current); event.target.checked ? next.add(row.physician_id) : next.delete(row.physician_id); return next })} /></td>
        <td><strong>{row.name}</strong>{row.partial_block && <button className="partial-badge" onClick={() => void openPartial(row.physician_id)}>Partial{row.excluded ? ' · Excluded' : ''}</button>}<small>{row.contract_name}</small>{row.carry_confirmation_required && <small className="contract-change-note">Partial in {data.planning_source_block?.name || 'the preceding block'} — confirm or adjust full-block hours. <button type="button" onClick={() => confirmCarriedHours(row.physician_id)} disabled={data.read_only}>Confirm hours</button></small>}{row.workload_adjusted && <small className="workload-adjusted-note">Target adjusted from {row.workload_adjustment_base_values?.target_hours ?? '—'} <button type="button" onClick={() => resetWorkloadAdjustment(row.physician_id)} disabled={data.read_only}>Reset</button></small>}{row.parent_contract_changed && <small className="contract-change-note">Parent Contract changed. <button type="button" onClick={() => resetRowToParent(row.physician_id)} disabled={data.read_only}>Reset to Contract</button></small>}{row.normalization_note && <small className="normalization-note">{row.normalization_note}</small>}</td>
        {(['target_hours','minimum_hours','maximum_hours'] as NumericPlanKey[]).map((key) => <td key={key}><input className={row.apply_workload_adjustment ? 'recommended-value' : ''} disabled={data.read_only} value={row[key] ?? ''} onChange={(event) => updateRow(row.physician_id, key, event.target.value)} /></td>)}
        <td>{formatNumber(row.previous_hours)}</td>
        {(['target_shifts','minimum_shifts','maximum_shifts'] as NumericPlanKey[]).map((key) => <td key={key}><input disabled={data.read_only} value={row[key] ?? ''} onChange={(event) => updateRow(row.physician_id, key, event.target.value)} /></td>)}
        <td>{formatNumber(row.previous_shifts)}</td>
      </tr>)}</tbody></table></div>
      {!data.read_only && <div className="block-plan-save"><button className="primary-action" disabled={busy} onClick={() => void saveRows()}>{busy ? 'Saving…' : 'Save Block Values'}</button></div>}
    </> : <div className="partial-user-list">
      <p>Select a clinically active user to create, review, or edit a Temporary Partial Block Contract.</p>
      {partialUserRows.map((row) => <button key={row.physician_id} className={row.partial_block ? 'has-partial' : ''} onClick={() => void openPartial(row.physician_id)}>
        <span><strong>{row.name}</strong><small>{row.contract_name}</small></span><span>{row.partial_block ? (row.excluded ? 'Excluded · Edit' : 'Partial · Edit') : 'Create'}</span>
      </button>)}
    </div>}

    {partial && <div className="shift-modal-overlay" onClick={() => setPartial(null)}><div className="shift-modal shift-modal-wide partial-contract-modal" onClick={(event) => event.stopPropagation()}>
      <div className="shift-modal-header"><div><span className="partial-contract-label">Temporary Partial Block Contract</span><h2>{partial.user_name}</h2><p>{partial.schedule_block.generated_name || partial.schedule_block.name} · {partial.schedule_block.start_date} through {partial.schedule_block.end_date} · Parent: {partial.contract_name}</p></div><button onClick={() => setPartial(null)}>×</button></div>
      <div className="block-plan-tabs partial-tabs">
        <button className={partialTab === 'availability' ? 'active' : ''} onClick={() => setPartialTab('availability')}>Availability</button>
        <button className={partialTab === 'summary' ? 'active' : ''} onClick={() => setPartialTab('summary')}>Summary</button>
        {[...new Set(partial.rules.map((rule) => rule.section))].map((section) => <button key={section} className={`${partialTab === section ? 'active' : ''} ${partial.adjusted_sections.includes(section) ? 'adjusted' : ''}`} onClick={() => setPartialTab(section)}>{sectionLabel(section)}</button>)}
      </div>
      <div className="shift-modal-body partial-contract-body">
        {partialTab === 'availability' && <><p>Select every date this user is <strong>not available</strong>. A selected date prohibits any shift starting on that date.</p><div className="partial-calendar-grid">{monthGroups.map(([month, dates]) => {
          const leading = new Date(`${month}-01T00:00:00Z`).getUTCDay()
          return <section key={month}><h3>{monthLabel(month)}</h3><div className="partial-calendar-head">{['S','M','T','W','T','F','S'].map((day, index) => <span key={`${day}${index}`}>{day}</span>)}</div><div className="partial-calendar-days">{Array.from({ length: leading }).map((_, index) => <span key={`blank${index}`} />)}{dates.map((date) => <button key={date} className={unavailable.has(date) ? 'unavailable' : ''} onClick={() => setUnavailable((current) => { const next = new Set(current); next.has(date) ? next.delete(date) : next.add(date); return next })} disabled={partial.read_only}>{Number(date.slice(-2))}</button>)}</div></section>
        })}</div></>}
        {partialTab === 'summary' && <div className="partial-summary"><h3>{unavailable.size} unavailable dates</h3><p>Atlas prorates applicable volume rules by eligible calendar days. Minimums round down, maximums round up, and penalty weights remain unchanged.</p><div className="partial-block-values"><strong>Synced Block Hours values</strong>{[['target_hours','Target hrs'],['minimum_hours','Min hrs'],['maximum_hours','Max hrs'],['target_shifts','Target shifts'],['minimum_shifts','Min shifts'],['maximum_shifts','Max shifts']].map(([key,label]) => <label key={key}>{label}<input disabled={partial.read_only} value={partialBlockValues[key] || ''} onChange={(event) => setPartialBlockValues({ ...partialBlockValues, [key]: event.target.value })} /></label>)}</div><div className="summary-pills">{partial.adjusted_sections.map((section) => <span key={section}>{sectionLabel(section)}</span>)}</div></div>}
        {partialTab !== 'availability' && partialTab !== 'summary' && <div className="partial-rule-list">{partial.rules.filter((rule) => rule.section === partialTab).map((rule) => <label key={rule.path} className={`${rule.atlas_adjusted ? 'atlas-adjusted' : ''} ${rule.overridden ? 'manual-override' : ''}`}><span>{rule.label}<small>Parent: {String(rule.parent_value ?? '—')}</small></span>{typeof rule.effective_value === 'boolean' ? <input type="checkbox" checked={Boolean(overrides[rule.path] ?? rule.effective_value)} disabled={partial.read_only} onChange={(event) => setOverrides({ ...overrides, [rule.path]: event.target.checked })} /> : <input value={String(overrides[rule.path] ?? rule.effective_value ?? '')} disabled={partial.read_only} onChange={(event) => setOverrides({ ...overrides, [rule.path]: event.target.value })} />}</label>)}</div>}
      </div>
      <div className="shift-modal-actions">{!partial.read_only && <button className="danger" onClick={() => void deletePartial()}>Remove Temporary Contract</button>}<span className="action-spacer" />{partialTab === 'availability' && !partial.read_only && <button className="primary-action" disabled={busy} onClick={() => void savePartial(true)}>Save Availability and Review</button>}{partialTab !== 'availability' && !partial.read_only && <button className="primary-action" disabled={busy} onClick={() => void savePartial(false)}>Save Temporary Contract</button>}<button onClick={() => setPartial(null)}>Close</button></div>
    </div></div>}
  </div>
}
