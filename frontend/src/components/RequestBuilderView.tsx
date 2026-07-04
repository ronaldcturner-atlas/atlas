import React, { useEffect, useMemo, useState } from 'react'

type BuildStatus = 'PRE_BUILD' | 'BUILD' | 'PREVIEW' | 'ARCHIVE'
type RequestScope = 'USER' | 'ADMIN'
type StoredRequestType = 'DAY_OFF' | 'SHIFT_OFF' | 'DAY_ON' | 'SHIFT_ON'
type RequestType = 'NONE' | StoredRequestType
type Weight = 'LOW' | 'MEDIUM' | 'HIGH' | 'FIXED'

type ScheduleBlockSummary = {
  id: number
  start_date: string
  end_date: string
  build_status: BuildStatus
}

type PhysicianChoice = {
  id: number
  name: string
}

type RequestItem = {
  id: number
  physician: number
  date: string
  request_scope: RequestScope
  request_type: StoredRequestType
  weight: Weight
  shift_template_ids: number[]
}

type ShiftTemplateOption = {
  id: number
  name: string
  facility_name: string
  active_days_of_week: string[]
}

type RequestContextResponse = {
  schedule_block: ScheduleBlockSummary
  can_manage_requests: boolean
  selected_physician_id: number
  physicians: PhysicianChoice[]
  requests: RequestItem[]
  shift_templates: ShiftTemplateOption[]
}

type BlockProp = {
  id: number
  start_date: string
  end_date: string
  build_status: BuildStatus
}

type Props = {
  block: BlockProp
}

const API_BASE = 'http://localhost:8000/api'

const REQUEST_TYPES: Array<{ value: RequestType; label: string }> = [
  { value: 'NONE', label: 'None' },
  { value: 'DAY_OFF', label: 'Day Off' },
  { value: 'SHIFT_OFF', label: 'Shift Off' },
  { value: 'DAY_ON', label: 'Day On' },
  { value: 'SHIFT_ON', label: 'Shift On' },
]

const WEIGHTS: Array<{ value: Weight; label: string }> = [
  { value: 'LOW', label: 'Low' },
  { value: 'MEDIUM', label: 'Medium' },
  { value: 'HIGH', label: 'High' },
  { value: 'FIXED', label: 'Fixed' },
]

const DAY_NAMES = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']

function parseIsoDateToUtc(value: string) {
  const [year, month, day] = value.split('-').map(Number)
  return new Date(Date.UTC(year, month - 1, day))
}

function toIsoDateUtc(value: Date) {
  const year = value.getUTCFullYear()
  const month = String(value.getUTCMonth() + 1).padStart(2, '0')
  const day = String(value.getUTCDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}

function addDaysUtc(value: Date, days: number) {
  const copy = new Date(value.getTime())
  copy.setUTCDate(copy.getUTCDate() + days)
  return copy
}

function startOfMonthUtc(value: Date) {
  return new Date(Date.UTC(value.getUTCFullYear(), value.getUTCMonth(), 1))
}

function endOfMonthUtc(value: Date) {
  return new Date(Date.UTC(value.getUTCFullYear(), value.getUTCMonth() + 1, 0))
}

function monthLabel(value: Date) {
  return value.toLocaleDateString('en-US', {
    month: 'long',
    year: 'numeric',
    timeZone: 'UTC',
  })
}

function formatDisplayDate(value: Date) {
  return value.toLocaleDateString('en-US', {
    weekday: 'short',
    month: 'short',
    day: 'numeric',
    year: 'numeric',
    timeZone: 'UTC',
  })
}

function getDaysForMonthGrid(monthDate: Date, minDate: Date, maxDate: Date) {
  const first = startOfMonthUtc(monthDate)
  const last = endOfMonthUtc(monthDate)
  const leadingEmpty = first.getUTCDay()
  const days: Array<{ key: string; date: Date; inRange: boolean }> = []

  for (let i = 0; i < leadingEmpty; i += 1) {
    days.push({ key: `empty-leading-${i}`, date: first, inRange: false })
  }

  for (let day = 1; day <= last.getUTCDate(); day += 1) {
    const current = new Date(Date.UTC(monthDate.getUTCFullYear(), monthDate.getUTCMonth(), day))
    const inRange = current >= minDate && current <= maxDate
    days.push({ key: toIsoDateUtc(current), date: current, inRange })
  }

  return days
}

function getErrorMessage(defaultMessage: string, data: unknown) {
  if (!data || typeof data !== 'object') {
    return defaultMessage
  }

  const typed = data as Record<string, unknown>
  if (typeof typed.detail === 'string') {
    return typed.detail
  }

  const parts = Object.entries(typed)
    .flatMap(([field, value]) => {
      if (typeof value === 'string') {
        return `${field}: ${value}`
      }

      if (Array.isArray(value)) {
        return value.filter((item): item is string => typeof item === 'string').map((item) => `${field}: ${item}`)
      }

      return []
    })

  if (parts.length) {
    return parts.join(' ')
  }

  return defaultMessage
}

export default function RequestBuilderView({ block }: Props) {
  const [contextData, setContextData] = useState<RequestContextResponse | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const [isSaving, setIsSaving] = useState(false)
  const [isBulkSaving, setIsBulkSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [selectedPhysicianId, setSelectedPhysicianId] = useState<number | null>(null)
  const [selectedScope, setSelectedScope] = useState<RequestScope>('USER')

  const blockStart = useMemo(() => parseIsoDateToUtc(block.start_date), [block.start_date])
  const blockEnd = useMemo(() => parseIsoDateToUtc(block.end_date), [block.end_date])

  const [visibleMonth, setVisibleMonth] = useState<Date>(startOfMonthUtc(blockStart))
  const [selectedDate, setSelectedDate] = useState<string>(block.start_date)

  const [requestType, setRequestType] = useState<RequestType>('NONE')
  const [weight, setWeight] = useState<Weight>('MEDIUM')
  const [selectedShiftTemplateIds, setSelectedShiftTemplateIds] = useState<number[]>([])

  const [bulkPhysicianIds, setBulkPhysicianIds] = useState<number[]>([])
  const [bulkRequestType, setBulkRequestType] = useState<StoredRequestType>('DAY_OFF')
  const [bulkWeight, setBulkWeight] = useState<Weight>('MEDIUM')
  const [bulkShiftTemplateIds, setBulkShiftTemplateIds] = useState<number[]>([])
  const [bulkSelectedDates, setBulkSelectedDates] = useState<string[]>([])
  const [patternMode, setPatternMode] = useState<'EVERY' | 'REMAINING' | 'NTH'>('EVERY')
  const [patternWeekday, setPatternWeekday] = useState<number>(1)
  const [patternNth, setPatternNth] = useState<number>(3)

  const canEdit = block.build_status === 'PRE_BUILD' || block.build_status === 'BUILD'

  const fetchContext = async (physicianId?: number) => {
    try {
      setIsLoading(true)
      setError(null)

      const params = new URLSearchParams()
      if (physicianId !== undefined && physicianId !== null) {
        params.set('physician_id', String(physicianId))
      }
      const query = params.toString()
      const url = query
        ? `${API_BASE}/schedule-blocks/${block.id}/requests/context/?${query}`
        : `${API_BASE}/schedule-blocks/${block.id}/requests/context/`

      const response = await fetch(url, {
        credentials: 'include',
      })

      if (!response.ok) {
        const data = await response.json().catch(() => null)
        throw new Error(getErrorMessage('Unable to load request builder context.', data))
      }

      const data: RequestContextResponse = await response.json()
      setContextData(data)
      setSelectedPhysicianId(data.selected_physician_id ?? (data.physicians[0]?.id ?? null))
      setBulkPhysicianIds(data.can_manage_requests ? data.physicians.map((physician) => physician.id) : [])
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Unable to load request builder context.')
      setContextData(null)
    } finally {
      setIsLoading(false)
    }
  }

  useEffect(() => {
    setVisibleMonth(startOfMonthUtc(parseIsoDateToUtc(block.start_date)))
    setSelectedDate(block.start_date)
    fetchContext()
  }, [block.id, block.start_date, block.end_date, block.build_status])

  const requestsByDateAndScope = useMemo(() => {
    const map = new Map<string, RequestItem>()
    for (const item of contextData?.requests ?? []) {
      map.set(`${item.date}-${item.request_scope}`, item)
    }
    return map
  }, [contextData])

  useEffect(() => {
    const key = `${selectedDate}-${selectedScope}`
    const existing = requestsByDateAndScope.get(key)
    if (!existing) {
      setRequestType('NONE')
      setWeight('MEDIUM')
      setSelectedShiftTemplateIds([])
      return
    }

    setRequestType(existing.request_type)
    setWeight(existing.weight)
    setSelectedShiftTemplateIds(existing.shift_template_ids)
  }, [requestsByDateAndScope, selectedDate, selectedScope])

  useEffect(() => {
    if (!contextData) {
      return
    }

    if (selectedPhysicianId !== null) {
      return
    }

    if (contextData.physicians.length > 0) {
      setSelectedPhysicianId(contextData.physicians[0].id)
    }
  }, [contextData, selectedPhysicianId])

  const selectedDateUtc = useMemo(() => parseIsoDateToUtc(selectedDate), [selectedDate])

  const availableTemplatesForSelectedDate = useMemo(() => {
    const dayName = DAY_NAMES[selectedDateUtc.getUTCDay()]
    return (contextData?.shift_templates ?? []).filter((template) => template.active_days_of_week.includes(dayName))
  }, [contextData?.shift_templates, selectedDateUtc])

  const daysInGrid = useMemo(() => getDaysForMonthGrid(visibleMonth, blockStart, blockEnd), [visibleMonth, blockStart, blockEnd])

  const existingForSelectedDate = useMemo(() => {
    const userRequest = requestsByDateAndScope.get(`${selectedDate}-USER`) ?? null
    const adminRequest = requestsByDateAndScope.get(`${selectedDate}-ADMIN`) ?? null
    return { userRequest, adminRequest }
  }, [requestsByDateAndScope, selectedDate])

  const handlePhysicianChange = async (nextPhysicianId: number) => {
    setSelectedPhysicianId(nextPhysicianId)
    await fetchContext(nextPhysicianId)
  }

  const handleToggleTemplate = (templateId: number, singleSelect: boolean, forBulk = false) => {
    if (forBulk) {
      setBulkShiftTemplateIds((current) => {
        if (singleSelect) {
          return current[0] === templateId ? [] : [templateId]
        }

        if (current.includes(templateId)) {
          return current.filter((value) => value !== templateId)
        }

        return [...current, templateId]
      })
      return
    }

    setSelectedShiftTemplateIds((current) => {
      if (singleSelect) {
        return current[0] === templateId ? [] : [templateId]
      }

      if (current.includes(templateId)) {
        return current.filter((value) => value !== templateId)
      }

      return [...current, templateId]
    })
  }

  const saveRequest = async () => {
    if (!selectedPhysicianId) {
      setError('Select a physician first.')
      return
    }

    try {
      setIsSaving(true)
      setError(null)

      const response = await fetch(`${API_BASE}/schedule-blocks/${block.id}/requests/upsert/`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        credentials: 'include',
        body: JSON.stringify({
          physician_id: selectedPhysicianId,
          date: selectedDate,
          request_scope: selectedScope,
          request_type: requestType,
          weight,
          shift_template_ids: selectedShiftTemplateIds,
        }),
      })

      if (!response.ok) {
        const data = await response.json().catch(() => null)
        throw new Error(getErrorMessage('Unable to save request.', data))
      }

      await fetchContext(selectedPhysicianId)
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : 'Unable to save request.')
    } finally {
      setIsSaving(false)
    }
  }

  const collectPatternDates = () => {
    const results: string[] = []

    if (patternMode === 'EVERY') {
      for (let pointer = new Date(blockStart.getTime()); pointer <= blockEnd; pointer = addDaysUtc(pointer, 1)) {
        if (pointer.getUTCDay() === patternWeekday) {
          results.push(toIsoDateUtc(pointer))
        }
      }
      return results
    }

    if (patternMode === 'REMAINING') {
      const start = selectedDateUtc > blockStart ? selectedDateUtc : blockStart
      for (let pointer = new Date(start.getTime()); pointer <= blockEnd; pointer = addDaysUtc(pointer, 1)) {
        if (pointer.getUTCDay() === patternWeekday) {
          results.push(toIsoDateUtc(pointer))
        }
      }
      return results
    }

    const monthCursor = startOfMonthUtc(blockStart)
    while (monthCursor <= blockEnd) {
      const firstOfMonth = new Date(monthCursor.getTime())
      let count = 0

      for (let pointer = new Date(firstOfMonth.getTime()); pointer.getUTCMonth() === firstOfMonth.getUTCMonth(); pointer = addDaysUtc(pointer, 1)) {
        if (pointer < blockStart || pointer > blockEnd) {
          continue
        }

        if (pointer.getUTCDay() === patternWeekday) {
          count += 1
          if (count === patternNth) {
            results.push(toIsoDateUtc(pointer))
            break
          }
        }
      }

      monthCursor.setUTCMonth(monthCursor.getUTCMonth() + 1)
    }

    return results
  }

  const applyPattern = () => {
    const patternDates = collectPatternDates()
    setBulkSelectedDates((current) => Array.from(new Set([...current, ...patternDates])).sort())
  }

  const toggleBulkDate = (dateValue: string) => {
    setBulkSelectedDates((current) => {
      if (current.includes(dateValue)) {
        return current.filter((value) => value !== dateValue)
      }
      return [...current, dateValue].sort()
    })
  }

  const saveBulkRequests = async () => {
    if (!bulkPhysicianIds.length) {
      setError('Select one or more physicians for bulk requests.')
      return
    }

    if (!bulkSelectedDates.length) {
      setError('Select one or more dates for bulk requests.')
      return
    }

    try {
      setIsBulkSaving(true)
      setError(null)

      const response = await fetch(`${API_BASE}/schedule-blocks/${block.id}/requests/bulk/`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        credentials: 'include',
        body: JSON.stringify({
          physician_ids: bulkPhysicianIds,
          request_type: bulkRequestType,
          weight: bulkWeight,
          shift_template_ids: bulkShiftTemplateIds,
          dates: bulkSelectedDates,
        }),
      })

      if (!response.ok) {
        const data = await response.json().catch(() => null)
        throw new Error(getErrorMessage('Unable to save bulk requests.', data))
      }

      if (selectedPhysicianId) {
        await fetchContext(selectedPhysicianId)
      }
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : 'Unable to save bulk requests.')
    } finally {
      setIsBulkSaving(false)
    }
  }

  if (isLoading) {
    return <div className="scheduler-loading">Loading Request Builder...</div>
  }

  if (!contextData) {
    return <div className="facilities-error">Unable to load Request Builder context.</div>
  }

  if (!contextData.physicians.length) {
    return <div className="facilities-error">No physicians are available for Request Builder.</div>
  }

  if (selectedPhysicianId === null) {
    return <div className="scheduler-loading">Loading Request Builder...</div>
  }

  const selectedPhysician = contextData.physicians.find((physician) => physician.id === selectedPhysicianId) ?? null
  const shiftSelectionMode = requestType === 'SHIFT_ON' ? 'single' : requestType === 'SHIFT_OFF' ? 'multiple' : 'none'
  const bulkShiftSelectionMode = bulkRequestType === 'SHIFT_ON' ? 'single' : bulkRequestType === 'SHIFT_OFF' ? 'multiple' : 'none'

  return (
    <div className="request-builder-root">
      {error && <div className="facilities-error">{error}</div>}

      {!canEdit && (
        <div className="request-builder-banner">
          Requests are read-only for {block.build_status} Schedule Blocks. Requests can only be entered in PRE_BUILD or BUILD.
        </div>
      )}

      <div className="request-builder-toolbar">
        <div className="request-builder-physician">
          <span>Selected Physician</span>
          {contextData.can_manage_requests ? (
            <select
              value={selectedPhysicianId}
              onChange={(event) => void handlePhysicianChange(Number(event.target.value))}
            >
              {contextData.physicians.map((physician) => (
                <option key={physician.id} value={physician.id}>
                  {physician.name}
                </option>
              ))}
            </select>
          ) : (
            <div className="request-builder-selected-name">{selectedPhysician?.name}</div>
          )}
        </div>

        <div className="request-builder-month-nav">
          <button
            type="button"
            onClick={() => setVisibleMonth((current) => new Date(Date.UTC(current.getUTCFullYear(), current.getUTCMonth() - 1, 1)))}
            disabled={startOfMonthUtc(visibleMonth) <= startOfMonthUtc(blockStart)}
          >
            Previous
          </button>
          <strong>{monthLabel(visibleMonth)}</strong>
          <button
            type="button"
            onClick={() => setVisibleMonth((current) => new Date(Date.UTC(current.getUTCFullYear(), current.getUTCMonth() + 1, 1)))}
            disabled={startOfMonthUtc(visibleMonth) >= startOfMonthUtc(blockEnd)}
          >
            Next
          </button>
        </div>
      </div>

      <div className="request-builder-layout">
        <section className="request-builder-calendar">
          <div className="request-builder-weekdays">
            {['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'].map((day) => (
              <div key={day}>{day}</div>
            ))}
          </div>
          <div className="request-builder-grid">
            {daysInGrid.map((cell) => {
              const dateKey = toIsoDateUtc(cell.date)
              const userRequest = requestsByDateAndScope.get(`${dateKey}-USER`) ?? null
              const adminRequest = requestsByDateAndScope.get(`${dateKey}-ADMIN`) ?? null
              const isSelected = dateKey === selectedDate
              const isBulkSelected = bulkSelectedDates.includes(dateKey)

              if (!cell.key.startsWith('empty-leading-') && !cell.inRange) {
                return <div key={cell.key} className="request-day-cell request-day-cell-disabled" />
              }

              if (cell.key.startsWith('empty-leading-')) {
                return <div key={cell.key} className="request-day-cell request-day-cell-empty" />
              }

              return (
                <button
                  type="button"
                  key={cell.key}
                  className={`request-day-cell ${isSelected ? 'request-day-selected' : ''}`}
                  onClick={() => setSelectedDate(dateKey)}
                >
                  <div className="request-day-header">
                    <span>{cell.date.getUTCDate()}</span>
                    {contextData.can_manage_requests && (
                      <input
                        type="checkbox"
                        checked={isBulkSelected}
                        onChange={(event) => {
                          event.stopPropagation()
                          toggleBulkDate(dateKey)
                        }}
                        onClick={(event) => event.stopPropagation()}
                        aria-label={`Select ${dateKey} for bulk requests`}
                      />
                    )}
                  </div>
                  {userRequest && <div className="request-chip">User: {userRequest.request_type} ({userRequest.weight})</div>}
                  {adminRequest && <div className="request-chip request-chip-admin">Admin: {adminRequest.request_type} ({adminRequest.weight})</div>}
                </button>
              )
            })}
          </div>
        </section>

        <section className="request-editor">
          <h3>{formatDisplayDate(selectedDateUtc)}</h3>
          {contextData.can_manage_requests && (
            <div className="request-scope-toggle">
              <button
                type="button"
                className={selectedScope === 'USER' ? 'active' : ''}
                onClick={() => setSelectedScope('USER')}
              >
                User Request
              </button>
              <button
                type="button"
                className={selectedScope === 'ADMIN' ? 'active' : ''}
                onClick={() => setSelectedScope('ADMIN')}
              >
                Admin Request
              </button>
            </div>
          )}

          {!contextData.can_manage_requests && existingForSelectedDate.userRequest && (
            <div className="request-existing-note">Your existing request for this date is loaded below.</div>
          )}

          <label className="facility-field">
            <span>Request Type</span>
            <select
              value={requestType}
              onChange={(event) => setRequestType(event.target.value as RequestType)}
              disabled={!canEdit}
            >
              {REQUEST_TYPES.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>

          {shiftSelectionMode !== 'none' && (
            <fieldset className="days-fieldset">
              <legend>Shift Templates On This Date</legend>
              <div className="request-template-list">
                {availableTemplatesForSelectedDate.map((template) => (
                  <label key={template.id} className="day-option">
                    <input
                      type={shiftSelectionMode === 'single' ? 'radio' : 'checkbox'}
                      name="single-request-template"
                      checked={selectedShiftTemplateIds.includes(template.id)}
                      onChange={() => handleToggleTemplate(template.id, shiftSelectionMode === 'single')}
                      disabled={!canEdit}
                    />
                    <span>{template.name} ({template.facility_name})</span>
                  </label>
                ))}
                {!availableTemplatesForSelectedDate.length && <div className="empty-state">No shift templates available on this date.</div>}
              </div>
            </fieldset>
          )}

          {requestType !== 'NONE' && (
            <label className="facility-field">
              <span>Weight</span>
              <select value={weight} onChange={(event) => setWeight(event.target.value as Weight)} disabled={!canEdit}>
                {WEIGHTS.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
            </label>
          )}

          <div className="request-editor-actions">
            <button type="button" onClick={saveRequest} disabled={!canEdit || isSaving}>
              {isSaving ? 'Saving...' : 'Save'}
            </button>
          </div>
        </section>
      </div>

      {contextData.can_manage_requests && (
        <section className="request-bulk-panel">
          <h3>Bulk Requests</h3>
          <div className="request-bulk-grid">
            <fieldset className="days-fieldset">
              <legend>Select Users</legend>
              <div className="request-template-list">
                {contextData.physicians.map((physician) => (
                  <label key={physician.id} className="day-option">
                    <input
                      type="checkbox"
                      checked={bulkPhysicianIds.includes(physician.id)}
                      onChange={() => {
                        setBulkPhysicianIds((current) => {
                          if (current.includes(physician.id)) {
                            return current.filter((value) => value !== physician.id)
                          }
                          return [...current, physician.id]
                        })
                      }}
                    />
                    <span>{physician.name}</span>
                  </label>
                ))}
              </div>
            </fieldset>

            <label className="facility-field">
              <span>Request Type</span>
              <select
                value={bulkRequestType}
                onChange={(event) => {
                  const nextType = event.target.value as StoredRequestType
                  setBulkRequestType(nextType)
                  if (nextType === 'DAY_OFF' || nextType === 'DAY_ON') {
                    setBulkShiftTemplateIds([])
                  }
                  if (nextType === 'SHIFT_ON' && bulkShiftTemplateIds.length > 1) {
                    setBulkShiftTemplateIds(bulkShiftTemplateIds.slice(0, 1))
                  }
                }}
                disabled={!canEdit}
              >
                <option value="DAY_OFF">Day Off</option>
                <option value="SHIFT_OFF">Shift Off</option>
                <option value="DAY_ON">Day On</option>
                <option value="SHIFT_ON">Shift On</option>
              </select>
            </label>

            {bulkShiftSelectionMode !== 'none' && (
              <fieldset className="days-fieldset">
                <legend>Shift Templates</legend>
                <div className="request-template-list">
                  {(contextData.shift_templates ?? []).map((template) => (
                    <label key={template.id} className="day-option">
                      <input
                        type={bulkShiftSelectionMode === 'single' ? 'radio' : 'checkbox'}
                        name="bulk-template"
                        checked={bulkShiftTemplateIds.includes(template.id)}
                        onChange={() => handleToggleTemplate(template.id, bulkShiftSelectionMode === 'single', true)}
                        disabled={!canEdit}
                      />
                      <span>{template.name} ({template.facility_name})</span>
                    </label>
                  ))}
                </div>
              </fieldset>
            )}

            <label className="facility-field">
              <span>Weight</span>
              <select value={bulkWeight} onChange={(event) => setBulkWeight(event.target.value as Weight)} disabled={!canEdit}>
                {WEIGHTS.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
            </label>
          </div>

          <div className="request-pattern-panel">
            <h4>Pattern Helper</h4>
            <div className="request-pattern-grid">
              <label className="facility-field">
                <span>Pattern</span>
                <select value={patternMode} onChange={(event) => setPatternMode(event.target.value as 'EVERY' | 'REMAINING' | 'NTH')}>
                  <option value="EVERY">Every weekday</option>
                  <option value="REMAINING">Remaining weekday</option>
                  <option value="NTH">Nth weekday of month</option>
                </select>
              </label>

              <label className="facility-field">
                <span>Weekday</span>
                <select value={patternWeekday} onChange={(event) => setPatternWeekday(Number(event.target.value))}>
                  {DAY_NAMES.map((day, index) => (
                    <option key={day} value={index}>
                      {day}
                    </option>
                  ))}
                </select>
              </label>

              {patternMode === 'NTH' && (
                <label className="facility-field">
                  <span>Occurrence</span>
                  <select value={patternNth} onChange={(event) => setPatternNth(Number(event.target.value))}>
                    <option value={1}>First</option>
                    <option value={2}>Second</option>
                    <option value={3}>Third</option>
                    <option value={4}>Fourth</option>
                    <option value={5}>Fifth</option>
                  </select>
                </label>
              )}

              <button type="button" onClick={applyPattern} disabled={!canEdit}>
                Apply Pattern
              </button>
            </div>

            <div className="request-bulk-selected-dates">
              <strong>Selected Dates ({bulkSelectedDates.length})</strong>
              <div className="facility-chip-list">
                {bulkSelectedDates.map((dateValue) => (
                  <button key={dateValue} type="button" className="facility-chip" onClick={() => toggleBulkDate(dateValue)}>
                    {dateValue}
                  </button>
                ))}
              </div>
            </div>
          </div>

          <div className="request-editor-actions">
            <button type="button" onClick={saveBulkRequests} disabled={!canEdit || isBulkSaving}>
              {isBulkSaving ? 'Saving Bulk Requests...' : 'Save Bulk Requests'}
            </button>
          </div>
        </section>
      )}
    </div>
  )
}
