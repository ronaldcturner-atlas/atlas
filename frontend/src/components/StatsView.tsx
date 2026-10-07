import React, { useEffect, useMemo, useState } from 'react'
import { useAuth } from '../contexts/AuthContext'
import { readSessionNumber, writeSessionSelection } from '../utils/sessionSelection'

type PublishedShift = {
  id: number | null
  physician: number | null
  physician_name: string
  facility_short_name: string
  facility_name: string
  date: string
  start_time: string
  end_time: string
  is_night: boolean
  status: string
  shift_template_id: number
  domain: number
  domain_name: string
  region: number
  region_name: string
}

type StatsGroup = { id: number; name: string; shift_template_ids: number[]; domain_ids: number[] }
type ShiftTemplateOption = { id: number; domain: number; name: string; facility_name: string; facility_sort_order: number; start_time: string; end_time: string; active: boolean }
type DomainOption = { id: number; name: string; region: number; region_name: string; active: boolean }

function csrfToken() {
  return document.cookie.split(';').map((value) => value.trim()).find((value) => value.startsWith('csrftoken='))?.slice(10) ?? ''
}

const MONTHS = [
  'January', 'February', 'March', 'April', 'May', 'June',
  'July', 'August', 'September', 'October', 'November', 'December',
]

function isoDate(date: Date) {
  const year = date.getFullYear()
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}

function monthRange(year: number, month: number) {
  return {
    from: isoDate(new Date(year, month, 1)),
    through: isoDate(new Date(year, month + 1, 0)),
  }
}

function clockMinutes(value: string) {
  const [hours, minutes] = value.split(':').map(Number)
  return hours * 60 + minutes
}

function shiftHours(shift: PublishedShift) {
  const start = clockMinutes(shift.start_time)
  let end = clockMinutes(shift.end_time)
  if (end <= start) end += 24 * 60
  return (end - start) / 60
}

function displayClock(value: string) {
  const [rawHour, rawMinute] = value.split(':').map(Number)
  const suffix = rawHour < 12 ? 'a' : 'p'
  const hour = rawHour % 12 || 12
  return `${hour}${rawMinute ? `:${String(rawMinute).padStart(2, '0')}` : ''}${suffix}`
}

function displayDate(value: string) {
  const [year, month, day] = value.split('-').map(Number)
  return `${month}/${day}/${year}`
}

function displayHours(value: number) {
  return Number.isInteger(value) ? String(value) : value.toFixed(1)
}

type StatsViewProps = {
  limitedToHours?: boolean
}

export default function StatsView({ limitedToHours = false }: StatsViewProps) {
  const { user } = useAuth()
  const today = new Date()
  const [selectedMonth, setSelectedMonth] = useState(today.getMonth())
  const [selectedYear, setSelectedYear] = useState(today.getFullYear())
  const initialRange = monthRange(today.getFullYear(), today.getMonth())
  const [fromDate, setFromDate] = useState(initialRange.from)
  const [throughDate, setThroughDate] = useState(initialRange.through)
  const [shifts, setShifts] = useState<PublishedShift[]>([])
  const [groupShifts, setGroupShifts] = useState<PublishedShift[]>([])
  const [domains, setDomains] = useState<DomainOption[]>([])
  const [selectedRegionId, setSelectedRegionId] = useState<number | null>(() => readSessionNumber('atlas.stats.region'))
  const [selectedDomainId, setSelectedDomainId] = useState<number | null>(() => readSessionNumber('atlas.stats.domain'))
  const [isLoading, setIsLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [activeStats, setActiveStats] = useState<'mine' | 'group'>('mine')
  const [statsGroups, setStatsGroups] = useState<StatsGroup[]>([])
  const [shiftTemplates, setShiftTemplates] = useState<ShiftTemplateOption[]>([])
  const [editingGroupId, setEditingGroupId] = useState<number | null>(null)
  const [groupName, setGroupName] = useState('')
  const [selectedTemplateIds, setSelectedTemplateIds] = useState<number[]>([])
  const [isSavingGroup, setIsSavingGroup] = useState(false)
  const statsDomainIds = useMemo(() => {
    if (!user) return new Set<number>()
    if (user.test_access) {
      return new Set(user.permissions.includes('view_domain_statistics') ? [user.test_access.domain_id] : [])
    }
    if (user.is_superuser || user.is_org_admin) return new Set(domains.map((domain) => domain.id))
    return new Set(user.domain_access.filter((access) => access.active && access.permissions.includes('view_domain_statistics')).map((access) => access.domain_id))
  }, [domains, user])
  const statsDomains = useMemo(() => domains.filter((domain) => domain.active && statsDomainIds.has(domain.id)), [domains, statsDomainIds])
  const statsRegions = useMemo(() => Array.from(new Map(statsDomains.map((domain) => [domain.region, { id: domain.region, name: domain.region_name }])).values()), [statsDomains])
  const statsDomainsForRegion = useMemo(() => statsDomains.filter((domain) => domain.region === selectedRegionId), [selectedRegionId, statsDomains])
  const selectedDomainAccess = user?.domain_access.find((access) => access.domain_id === selectedDomainId)
  const canManage = Boolean(
    selectedDomainId
    && (
      user?.is_superuser
      || (!user?.test_access && user?.is_org_admin)
      || (user?.test_access
        ? user.permissions.includes('manage_build_workspace')
        : selectedDomainAccess?.permissions.includes('manage_build_workspace'))
    )
  )

  useEffect(() => {
    Promise.all([
      fetch('http://localhost:8000/api/published-schedule/', { credentials: 'include' }),
      fetch('http://localhost:8000/api/published-schedule/?purpose=stats', { credentials: 'include' }),
      fetch('http://localhost:8000/api/stats-groups/', { credentials: 'include' }),
      fetch('http://localhost:8000/api/shift-templates/', { credentials: 'include' }),
      fetch('http://localhost:8000/api/domains/?active=true', { credentials: 'include' }),
    ])
      .then(async ([shiftResponse, groupShiftResponse, groupResponse, templateResponse, domainResponse]) => {
        const [shiftData, groupShiftData, groupData, templateData, domainData] = await Promise.all([shiftResponse.json(), groupShiftResponse.json(), groupResponse.json(), templateResponse.json(), domainResponse.json()])
        if (!shiftResponse.ok) throw new Error(shiftData.detail ?? 'Unable to load your statistics.')
        if (!groupShiftResponse.ok) throw new Error(groupShiftData.detail ?? 'Unable to load Group Stats.')
        if (!groupResponse.ok) throw new Error(groupData.detail ?? 'Unable to load Stats groups.')
        if (!domainResponse.ok) throw new Error(domainData.detail ?? 'Unable to load Stats access.')
        setShifts(shiftData)
        setGroupShifts(groupShiftData)
        setStatsGroups(groupData)
        setDomains(domainData)
        if (templateResponse.ok) setShiftTemplates(templateData)
      })
      .catch((loadError) => setError(loadError instanceof Error ? loadError.message : 'Unable to load your statistics.'))
      .finally(() => setIsLoading(false))
  }, [])

  useEffect(() => {
    if (!statsDomains.length) {
      setSelectedRegionId(null)
      setSelectedDomainId(null)
      return
    }
    setSelectedRegionId((current) => statsRegions.some((region) => region.id === current) ? current : statsRegions[0]?.id ?? null)
  }, [statsDomains.length, statsRegions])

  useEffect(() => {
    if (!statsDomains.length || selectedRegionId === null) return
    setSelectedDomainId((current) => statsDomainsForRegion.some((domain) => domain.id === current) ? current : statsDomainsForRegion[0]?.id ?? null)
  }, [selectedRegionId, statsDomains.length, statsDomainsForRegion])

  useEffect(() => writeSessionSelection('atlas.stats.region', selectedRegionId), [selectedRegionId])
  useEffect(() => writeSessionSelection('atlas.stats.domain', selectedDomainId), [selectedDomainId])

  const applyMonth = (year = selectedYear, month = selectedMonth) => {
    const range = monthRange(year, month)
    setFromDate(range.from)
    setThroughDate(range.through)
  }

  const years = useMemo(() => {
    const scheduleYears = shifts.map((shift) => Number(shift.date.slice(0, 4)))
    const minimum = Math.min(today.getFullYear() - 3, ...scheduleYears)
    const maximum = Math.max(today.getFullYear() + 3, ...scheduleYears)
    return Array.from({ length: maximum - minimum + 1 }, (_, index) => minimum + index)
  }, [shifts])

  const visibleShifts = shifts
    .filter((shift) => (
      shift.physician === user?.physician_id
      && shift.status !== 'open'
      && shift.date >= fromDate
      && shift.date <= throughDate
    ))
    .sort((left, right) => left.date.localeCompare(right.date) || left.start_time.localeCompare(right.start_time))
  const totalHours = visibleShifts.reduce((total, shift) => total + shiftHours(shift), 0)
  const nightHours = visibleShifts.reduce((total, shift) => total + (shift.is_night ? shiftHours(shift) : 0), 0)
  const visibleStatsGroups = useMemo(
    () => statsGroups.filter((group) => selectedDomainId !== null && group.domain_ids.includes(selectedDomainId)),
    [selectedDomainId, statsGroups],
  )
  const groupsByTemplate = useMemo(() => {
    const index = new Map<number, number[]>()
    visibleStatsGroups.forEach((group) => group.shift_template_ids.forEach((templateId) => index.set(templateId, [...(index.get(templateId) ?? []), group.id])))
    return index
  }, [visibleStatsGroups])
  const groupStats = Array.from(
    groupShifts
      .filter((shift) => shift.domain === selectedDomainId && shift.physician != null && shift.status !== 'open' && shift.date >= fromDate && shift.date <= throughDate)
      .reduce((totals, shift) => {
        const physicianId = shift.physician as number
        const current = totals.get(physicianId) ?? { id: physicianId, name: shift.physician_name, hours: 0, nightHours: 0, customHours: {} as Record<number, number> }
        const hours = shiftHours(shift)
        current.hours += hours
        if (shift.is_night) current.nightHours += hours
        for (const groupId of groupsByTemplate.get(shift.shift_template_id) ?? []) {
          current.customHours[groupId] = (current.customHours[groupId] ?? 0) + hours
        }
        totals.set(physicianId, current)
        return totals
      }, new Map<number, { id: number; name: string; hours: number; nightHours: number; customHours: Record<number, number> }>())
      .values(),
  ).sort((left, right) => left.name.localeCompare(right.name))

  const templatesByFacility = useMemo(() => shiftTemplates.filter((template) => template.domain === selectedDomainId).reduce((groups, template) => {
    const facility = template.facility_name || 'Other'
    groups.set(facility, [...(groups.get(facility) ?? []), template])
    return groups
  }, new Map<string, ShiftTemplateOption[]>()), [selectedDomainId, shiftTemplates])

  const resetGroupForm = () => {
    setEditingGroupId(null)
    setGroupName('')
    setSelectedTemplateIds([])
  }

  const saveGroup = async () => {
    setIsSavingGroup(true)
    setError(null)
    try {
      const response = await fetch(`http://localhost:8000/api/stats-groups/${editingGroupId ? `${editingGroupId}/` : ''}`, {
        method: editingGroupId ? 'PATCH' : 'POST', credentials: 'include',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken() },
        body: JSON.stringify({ name: groupName, shift_template_ids: selectedTemplateIds }),
      })
      const data = await response.json()
      if (!response.ok) throw new Error(data.detail ?? 'Unable to save this Stats group.')
      setStatsGroups((current) => editingGroupId ? current.map((group) => group.id === data.id ? data : group) : [...current, data].sort((a, b) => a.name.localeCompare(b.name)))
      resetGroupForm()
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : 'Unable to save this Stats group.')
    } finally { setIsSavingGroup(false) }
  }

  const deleteGroup = async (group: StatsGroup) => {
    if (!window.confirm(`Delete the “${group.name}” Stats group?`)) return
    const response = await fetch(`http://localhost:8000/api/stats-groups/${group.id}/`, { method: 'DELETE', credentials: 'include', headers: { 'X-CSRFToken': csrfToken() } })
    if (response.ok) { setStatsGroups((current) => current.filter((item) => item.id !== group.id)); if (editingGroupId === group.id) resetGroupForm() }
    else setError('Unable to delete this Stats group.')
  }

  return (
    <div className="stats-page-card">
      <div className="stats-quick-links">
        <button type="button" className={activeStats === 'mine' ? 'active' : ''} onClick={() => setActiveStats('mine')}>My Stats</button>
        {!!statsDomains.length && <button type="button" className={activeStats === 'group' ? 'active' : ''} onClick={() => setActiveStats('group')}>Group Stats</button>}
      </div>
      {activeStats === 'group' && !!statsDomains.length && <div className="stats-scope-controls">
        <label className="stats-scope-field">
          <span>Region</span>
          {statsRegions.length > 1 ? <select value={selectedRegionId ?? ''} onChange={(event) => { setSelectedRegionId(Number(event.target.value)); setSelectedDomainId(null) }}>{statsRegions.map((region) => <option key={region.id} value={region.id}>{region.name}</option>)}</select> : <strong>{statsRegions[0]?.name}</strong>}
        </label>
        <label className="stats-scope-field">
          <span>Domain</span>
          {statsDomainsForRegion.length > 1 ? <select value={selectedDomainId ?? ''} onChange={(event) => setSelectedDomainId(Number(event.target.value))}>{statsDomainsForRegion.map((domain) => <option key={domain.id} value={domain.id}>{domain.name}</option>)}</select> : <strong>{statsDomainsForRegion[0]?.name}</strong>}
        </label>
      </div>}
      <div className="stats-filters">
        <div className="stats-month-filter">
          <label>Month<select value={selectedMonth} onChange={(event) => { const month = Number(event.target.value); setSelectedMonth(month); applyMonth(selectedYear, month) }}>{MONTHS.map((month, index) => <option value={index} key={month}>{month}</option>)}</select></label>
          <label>Year<select value={selectedYear} onChange={(event) => { const year = Number(event.target.value); setSelectedYear(year); applyMonth(year, selectedMonth) }}>{years.map((year) => <option key={year}>{year}</option>)}</select></label>
        </div>
        <span className="stats-filter-divider">or</span>
        <div className="stats-range-filter">
          <label>From<input type="date" value={fromDate} max={throughDate} onChange={(event) => setFromDate(event.target.value)} /></label>
          <label>Through<input type="date" value={throughDate} min={fromDate} onChange={(event) => setThroughDate(event.target.value)} /></label>
        </div>
      </div>

      {error && <div className="facilities-error">{error}</div>}
      {isLoading ? <div className="scheduler-loading">Loading statistics...</div> : activeStats === 'mine' ? (
        <div className={`stats-layout${limitedToHours ? ' stats-layout-summary-only' : ''}`}>
          {!limitedToHours && <div className="stats-shift-list">
            {visibleShifts.map((shift) => (
              <div className="stats-shift-row" key={shift.id}>
                <span>{shift.facility_short_name || shift.facility_name} {displayClock(shift.start_time)}-{displayClock(shift.end_time)}</span>
                <span>{displayDate(shift.date)}</span>
                <strong>{displayHours(shiftHours(shift))} hours</strong>
              </div>
            ))}
            {!visibleShifts.length && <div className="empty-state">No shifts in this date range.</div>}
          </div>}
          <aside className="stats-totals">
            <div><span>Total hours</span><strong>{displayHours(totalHours)}</strong></div>
            <div><span>Night hours</span><strong>{displayHours(nightHours)}</strong></div>
          </aside>
        </div>
      ) : (
        <>
        {!limitedToHours && canManage && <details className="stats-group-manager">
          <summary>Manage custom columns</summary>
          <div className="stats-group-manager-content">
            {!!visibleStatsGroups.length && <div className="stats-group-existing">{visibleStatsGroups.map((group) => <div key={group.id}><strong>{group.name}</strong><span>{group.shift_template_ids.length} shifts</span><button type="button" onClick={() => { setEditingGroupId(group.id); setGroupName(group.name); setSelectedTemplateIds(group.shift_template_ids) }}>Edit</button><button type="button" className="danger" onClick={() => deleteGroup(group)}>Delete</button></div>)}</div>}
            <div className="stats-group-form">
              <label>Column name<input value={groupName} maxLength={80} placeholder="For example, Evenings" onChange={(event) => setGroupName(event.target.value)} /></label>
              <div className="stats-group-template-list">
                {[...templatesByFacility.entries()].map(([facility, templates]) => <fieldset key={facility}><legend>{facility}</legend>{templates.map((template) => <label key={template.id}><input type="checkbox" checked={selectedTemplateIds.includes(template.id)} onChange={(event) => setSelectedTemplateIds((current) => event.target.checked ? [...current, template.id] : current.filter((id) => id !== template.id))} />{template.name}{!template.active && <small>Disabled</small>}</label>)}</fieldset>)}
              </div>
              <div className="stats-group-form-actions"><button type="button" disabled={isSavingGroup} onClick={saveGroup}>{editingGroupId ? 'Save changes' : 'Create column'}</button>{editingGroupId && <button type="button" onClick={resetGroupForm}>Cancel</button>}</div>
            </div>
          </div>
        </details>}
        <div className="group-stats-scroll"><div className="group-stats-list">
          <div className="group-stats-row group-stats-heading" style={{ gridTemplateColumns: `minmax(180px, 1fr) repeat(${2 + (limitedToHours ? 0 : visibleStatsGroups.length)}, 120px)` }}><span>User</span><span>Total hours</span><span>Night hours</span>{!limitedToHours && visibleStatsGroups.map((group) => <span key={group.id}>{group.name}</span>)}</div>
          {groupStats.map((person) => (
            <div className="group-stats-row" style={{ gridTemplateColumns: `minmax(180px, 1fr) repeat(${2 + (limitedToHours ? 0 : visibleStatsGroups.length)}, 120px)` }} key={person.id}>
              <strong>{person.name}</strong>
              <span>{displayHours(person.hours)}</span>
              <span>{displayHours(person.nightHours)}</span>
              {!limitedToHours && visibleStatsGroups.map((group) => <span key={group.id}>{displayHours(person.customHours[group.id] ?? 0)}</span>)}
            </div>
          ))}
          {!groupStats.length && <div className="empty-state">No scheduled users in this date range.</div>}
        </div></div>
        </>
      )}
    </div>
  )
}
