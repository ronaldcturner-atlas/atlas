import React, { useEffect, useMemo, useState } from 'react'
import { useAuth } from '../contexts/AuthContext'

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
}

type StatsGroup = { id: number; name: string; shift_template_ids: number[] }
type ShiftTemplateOption = { id: number; name: string; facility_name: string; start_time: string; active: boolean }

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

export default function StatsView() {
  const { user } = useAuth()
  const today = new Date()
  const [selectedMonth, setSelectedMonth] = useState(today.getMonth())
  const [selectedYear, setSelectedYear] = useState(today.getFullYear())
  const initialRange = monthRange(today.getFullYear(), today.getMonth())
  const [fromDate, setFromDate] = useState(initialRange.from)
  const [throughDate, setThroughDate] = useState(initialRange.through)
  const [shifts, setShifts] = useState<PublishedShift[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [activeStats, setActiveStats] = useState<'mine' | 'group'>('mine')
  const [statsGroups, setStatsGroups] = useState<StatsGroup[]>([])
  const [shiftTemplates, setShiftTemplates] = useState<ShiftTemplateOption[]>([])
  const [editingGroupId, setEditingGroupId] = useState<number | null>(null)
  const [groupName, setGroupName] = useState('')
  const [selectedTemplateIds, setSelectedTemplateIds] = useState<number[]>([])
  const [isSavingGroup, setIsSavingGroup] = useState(false)
  const canManage = Boolean(user?.is_staff || user?.is_superuser || user?.groups.some((group) => ['admin', 'scheduler'].includes(group.toLowerCase())))

  useEffect(() => {
    Promise.all([
      fetch('http://localhost:8000/api/published-schedule/', { credentials: 'include' }),
      fetch('http://localhost:8000/api/stats-groups/', { credentials: 'include' }),
      fetch('http://localhost:8000/api/shift-templates/', { credentials: 'include' }),
    ])
      .then(async ([shiftResponse, groupResponse, templateResponse]) => {
        const [shiftData, groupData, templateData] = await Promise.all([shiftResponse.json(), groupResponse.json(), templateResponse.json()])
        if (!shiftResponse.ok) throw new Error(shiftData.detail ?? 'Unable to load your statistics.')
        if (!groupResponse.ok) throw new Error(groupData.detail ?? 'Unable to load Stats groups.')
        setShifts(shiftData)
        setStatsGroups(groupData)
        if (templateResponse.ok) setShiftTemplates(templateData)
      })
      .catch((loadError) => setError(loadError instanceof Error ? loadError.message : 'Unable to load your statistics.'))
      .finally(() => setIsLoading(false))
  }, [])

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
  const groupsByTemplate = useMemo(() => {
    const index = new Map<number, number[]>()
    statsGroups.forEach((group) => group.shift_template_ids.forEach((templateId) => index.set(templateId, [...(index.get(templateId) ?? []), group.id])))
    return index
  }, [statsGroups])
  const groupStats = Array.from(
    shifts
      .filter((shift) => shift.physician != null && shift.status !== 'open' && shift.date >= fromDate && shift.date <= throughDate)
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

  const templatesByFacility = useMemo(() => shiftTemplates.reduce((groups, template) => {
    const facility = template.facility_name || 'Other'
    groups.set(facility, [...(groups.get(facility) ?? []), template])
    return groups
  }, new Map<string, ShiftTemplateOption[]>()), [shiftTemplates])

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
        <button type="button" className={activeStats === 'group' ? 'active' : ''} onClick={() => setActiveStats('group')}>Group Stats</button>
      </div>
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
        <div className="stats-layout">
          <div className="stats-shift-list">
            {visibleShifts.map((shift) => (
              <div className="stats-shift-row" key={shift.id}>
                <span>{shift.facility_short_name || shift.facility_name} {displayClock(shift.start_time)}-{displayClock(shift.end_time)}</span>
                <span>{displayDate(shift.date)}</span>
                <strong>{displayHours(shiftHours(shift))} hours</strong>
              </div>
            ))}
            {!visibleShifts.length && <div className="empty-state">No shifts in this date range.</div>}
          </div>
          <aside className="stats-totals">
            <div><span>Total hours</span><strong>{displayHours(totalHours)}</strong></div>
            <div><span>Night hours</span><strong>{displayHours(nightHours)}</strong></div>
          </aside>
        </div>
      ) : (
        <>
        {canManage && <details className="stats-group-manager">
          <summary>Manage custom columns</summary>
          <div className="stats-group-manager-content">
            {!!statsGroups.length && <div className="stats-group-existing">{statsGroups.map((group) => <div key={group.id}><strong>{group.name}</strong><span>{group.shift_template_ids.length} shifts</span><button type="button" onClick={() => { setEditingGroupId(group.id); setGroupName(group.name); setSelectedTemplateIds(group.shift_template_ids) }}>Edit</button><button type="button" className="danger" onClick={() => deleteGroup(group)}>Delete</button></div>)}</div>}
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
          <div className="group-stats-row group-stats-heading" style={{ gridTemplateColumns: `minmax(180px, 1fr) repeat(${2 + statsGroups.length}, 120px)` }}><span>User</span><span>Total hours</span><span>Night hours</span>{statsGroups.map((group) => <span key={group.id}>{group.name}</span>)}</div>
          {groupStats.map((person) => (
            <div className="group-stats-row" style={{ gridTemplateColumns: `minmax(180px, 1fr) repeat(${2 + statsGroups.length}, 120px)` }} key={person.id}>
              <strong>{person.name}</strong>
              <span>{displayHours(person.hours)}</span>
              <span>{displayHours(person.nightHours)}</span>
              {statsGroups.map((group) => <span key={group.id}>{displayHours(person.customHours[group.id] ?? 0)}</span>)}
            </div>
          ))}
          {!groupStats.length && <div className="empty-state">No scheduled users in this date range.</div>}
        </div></div>
        </>
      )}
    </div>
  )
}
