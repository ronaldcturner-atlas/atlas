import React, { useEffect, useMemo, useRef, useState } from 'react'

type BuildStatus = 'PRE_BUILD' | 'BUILD' | 'PREVIEW' | 'ARCHIVE'

type ScheduleBlock = {
  id: number
  name: string
  start_date: string
  end_date: string
  build_status: BuildStatus
}

type DomainOption = {
  id: number
  name: string
}

type ScheduleVersion = {
  id: number
  schedule_block: number
  domain: number
  domain_name: string
  version_number: number
  name: string
  status: 'BUILD' | 'PREVIEW' | 'ARCHIVED'
  shift_instance_count: number
}

type ShiftInstance = {
  id: number
  schedule_version: number
  date: string
  shift_template: number
  shift_template_name: string
  facility_short_name: string
  template_start_time: string
  template_end_time: string
  required_staffing: number
  assigned_count: number
  open_count: number
  is_open: boolean
  status: 'OPEN' | 'ASSIGNED'
  assignments: ShiftAssignment[]
}

type ShiftAssignment = {
  id: number
  physician: number
  physician_name: string
}

type EligiblePhysician = {
  id: number
  name: string
  already_assigned: boolean
  domain_eligible: boolean
  facility_eligible: boolean
  can_assign: boolean
  ineligibility_reason: string
}

type AssignmentContext = {
  shift_instance: ShiftInstance
  eligible_physicians: EligiblePhysician[]
}

type PopoverPosition = {
  left: number
  top?: number
  bottom?: number
  maxHeight: number
}

type BuildContext = {
  schedule_block: ScheduleBlock
  domains: DomainOption[]
  versions: ScheduleVersion[]
  selected_version: ScheduleVersion | null
  shift_instances: ShiftInstance[]
}

type Props = {
  blockId: number
  onBack: () => void
}

const API_BASE = 'http://localhost:8000/api'

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

function formatDate(value: string) {
  return parseIsoDateToUtc(value).toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
    timeZone: 'UTC',
  })
}

function formatTime(value: string) {
  const [hoursRaw, minutesRaw] = value.split(':')
  const hours = Number(hoursRaw)
  const minutes = Number(minutesRaw)
  const suffix = hours < 12 ? 'a' : 'p'
  const hour = hours % 12 || 12
  return minutes === 0 ? `${hour}${suffix}` : `${hour}:${String(minutes).padStart(2, '0')}${suffix}`
}

function physicianLastName(name: string) {
  const parts = name.trim().split(/\s+/)
  return parts[parts.length - 1] || name
}

function shiftLabel(instance: ShiftInstance) {
  const staffing = `${instance.assigned_count}/${instance.required_staffing}`
  const names = instance.assignments.map((assignment) => physicianLastName(assignment.physician_name))
  const assignedLabel = names.length
    ? `${names[0]}${names.length > 1 ? ` +${names.length - 1}` : ''}`
    : ''
  const openLabel = instance.is_open ? 'Open ' : ''
  return [
    instance.facility_short_name,
    `${formatTime(instance.template_start_time)}-${formatTime(instance.template_end_time)}`,
    assignedLabel,
    `${openLabel}${staffing}`,
  ].filter(Boolean).join(' ')
}

function monthGrid(month: Date, blockStart: Date, blockEnd: Date) {
  const first = startOfMonthUtc(month)
  const last = endOfMonthUtc(month)
  const cells: Array<{ key: string; date: Date | null; inRange: boolean }> = []

  for (let index = 0; index < first.getUTCDay(); index += 1) {
    cells.push({ key: `empty-${index}`, date: null, inRange: false })
  }

  for (let day = 1; day <= last.getUTCDate(); day += 1) {
    const date = new Date(Date.UTC(month.getUTCFullYear(), month.getUTCMonth(), day))
    cells.push({
      key: toIsoDateUtc(date),
      date,
      inRange: date >= blockStart && date <= blockEnd,
    })
  }

  return cells
}

function apiError(data: unknown, fallback: string) {
  if (!data || typeof data !== 'object') {
    return fallback
  }
  const payload = data as Record<string, unknown>
  if (typeof payload.detail === 'string') {
    return payload.detail
  }
  const messages = Object.entries(payload).flatMap(([field, value]) => {
    if (typeof value === 'string') {
      return `${field}: ${value}`
    }
    if (Array.isArray(value)) {
      return value.map((item) => `${field}: ${String(item)}`)
    }
    return []
  })
  return messages.join(' ') || fallback
}

function sortedPhysicianMatches(
  physicians: EligiblePhysician[],
  query: string,
) {
  const normalizedQuery = query.trim().toLocaleLowerCase()
  const alphaSort = (left: EligiblePhysician, right: EligiblePhysician) => (
    left.name.localeCompare(right.name, undefined, { sensitivity: 'base' })
  )

  if (!normalizedQuery) {
    return [...physicians].sort(alphaSort)
  }

  return physicians
    .filter((physician) => physician.name.toLocaleLowerCase().includes(normalizedQuery))
    .sort((left, right) => {
      const leftStarts = left.name.toLocaleLowerCase().startsWith(normalizedQuery)
      const rightStarts = right.name.toLocaleLowerCase().startsWith(normalizedQuery)
      if (leftStarts !== rightStarts) {
        return leftStarts ? -1 : 1
      }
      return alphaSort(left, right)
    })
}

export default function ScheduleBuildWorkspace({ blockId, onBack }: Props) {
  const [context, setContext] = useState<BuildContext | null>(null)
  const [selectedDomainId, setSelectedDomainId] = useState<number | null>(null)
  const [visibleMonth, setVisibleMonth] = useState<Date | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const [isGenerating, setIsGenerating] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [assignmentContext, setAssignmentContext] = useState<AssignmentContext | null>(null)
  const [assignmentTarget, setAssignmentTarget] = useState<ShiftInstance | null>(null)
  const [isAssignmentLoading, setIsAssignmentLoading] = useState(false)
  const [isAssignmentSaving, setIsAssignmentSaving] = useState(false)
  const [assignmentError, setAssignmentError] = useState<string | null>(null)
  const [physicianSearch, setPhysicianSearch] = useState('')
  const [popoverPosition, setPopoverPosition] = useState<PopoverPosition | null>(null)
  const assignmentPopoverRef = useRef<HTMLDivElement | null>(null)
  const assignmentTriggerRef = useRef<HTMLButtonElement | null>(null)
  const assignmentLoadIdRef = useRef(0)

  const closeAssignments = () => {
    assignmentLoadIdRef.current += 1
    setAssignmentTarget(null)
    setAssignmentContext(null)
    setAssignmentError(null)
    setPhysicianSearch('')
    setPopoverPosition(null)
    assignmentTriggerRef.current = null
  }

  const fetchContext = async (versionId?: number) => {
    try {
      setIsLoading(true)
      setError(null)
      const query = versionId ? `?version_id=${versionId}` : ''
      const response = await fetch(`${API_BASE}/schedule-blocks/${blockId}/build/${query}`, {
        credentials: 'include',
        cache: 'no-store',
      })
      const data = await response.json().catch(() => null)
      if (!response.ok) {
        throw new Error(apiError(data, 'Unable to load the Schedule Build Workspace.'))
      }

      const nextContext = data as BuildContext
      setContext(nextContext)
      setSelectedDomainId(
        nextContext.selected_version?.domain
        ?? nextContext.domains[0]?.id
        ?? null,
      )
      setVisibleMonth(startOfMonthUtc(parseIsoDateToUtc(nextContext.schedule_block.start_date)))
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : 'Unable to load the Schedule Build Workspace.')
    } finally {
      setIsLoading(false)
    }
  }

  useEffect(() => {
    closeAssignments()
    void fetchContext()
  }, [blockId])

  useEffect(() => {
    if (!assignmentTarget) {
      return undefined
    }

    const handlePointerDown = (event: MouseEvent) => {
      const target = event.target as Node
      if (
        !assignmentPopoverRef.current?.contains(target)
        && !assignmentTriggerRef.current?.contains(target)
        && !isAssignmentSaving
      ) {
        closeAssignments()
      }
    }
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !isAssignmentSaving) {
        closeAssignments()
      }
    }
    const handleViewportChange = () => {
      if (!isAssignmentSaving) {
        closeAssignments()
      }
    }
    const handleScroll = (event: Event) => {
      const target = event.target
      if (
        target instanceof Node
        && assignmentPopoverRef.current?.contains(target)
      ) {
        return
      }
      handleViewportChange()
    }

    document.addEventListener('mousedown', handlePointerDown)
    document.addEventListener('keydown', handleKeyDown)
    window.addEventListener('resize', handleViewportChange)
    document.addEventListener('scroll', handleScroll, true)
    return () => {
      document.removeEventListener('mousedown', handlePointerDown)
      document.removeEventListener('keydown', handleKeyDown)
      window.removeEventListener('resize', handleViewportChange)
      document.removeEventListener('scroll', handleScroll, true)
    }
  }, [assignmentTarget, isAssignmentSaving])

  const instancesByDate = useMemo(() => {
    const map = new Map<string, ShiftInstance[]>()
    for (const instance of context?.shift_instances ?? []) {
      const values = map.get(instance.date) ?? []
      values.push(instance)
      map.set(instance.date, values)
    }
    for (const instances of map.values()) {
      instances.sort((left, right) => {
        const facilityOrder = left.facility_short_name.localeCompare(
          right.facility_short_name,
          undefined,
          { sensitivity: 'base' },
        )
        if (facilityOrder !== 0) {
          return facilityOrder
        }

        const startTimeOrder = left.template_start_time.localeCompare(right.template_start_time)
        if (startTimeOrder !== 0) {
          return startTimeOrder
        }

        return left.id - right.id
      })
    }
    return map
  }, [context?.shift_instances])

  const blockStart = context ? parseIsoDateToUtc(context.schedule_block.start_date) : null
  const blockEnd = context ? parseIsoDateToUtc(context.schedule_block.end_date) : null
  const calendarCells = visibleMonth && blockStart && blockEnd
    ? monthGrid(visibleMonth, blockStart, blockEnd)
    : []

  const generateInstances = async () => {
    if (!selectedDomainId) {
      setError('Select a Domain before generating shift instances.')
      return
    }

    try {
      setIsGenerating(true)
      setError(null)
      setNotice(null)
      const response = await fetch(`${API_BASE}/schedule-blocks/${blockId}/build/generate/`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        credentials: 'include',
        body: JSON.stringify({ domain_id: selectedDomainId }),
      })
      const data = await response.json().catch(() => null)
      if (!response.ok) {
        throw new Error(apiError(data, 'Unable to generate shift instances.'))
      }

      setNotice(data.message)
      await fetchContext(data.schedule_version.id)
    } catch (generateError) {
      setError(generateError instanceof Error ? generateError.message : 'Unable to generate shift instances.')
    } finally {
      setIsGenerating(false)
    }
  }

  const applyAssignmentContext = (nextAssignmentContext: AssignmentContext) => {
    setAssignmentContext(nextAssignmentContext)
    setContext((current) => current
      ? {
        ...current,
        shift_instances: current.shift_instances.map((instance) => (
          instance.id === nextAssignmentContext.shift_instance.id
            ? nextAssignmentContext.shift_instance
            : instance
        )),
      }
      : current)
  }

  const openAssignments = async (
    instance: ShiftInstance,
    trigger: HTMLButtonElement,
  ) => {
    if (isAssignmentSaving) {
      return
    }
    if (assignmentTarget?.id === instance.id) {
      closeAssignments()
      return
    }

    const loadId = assignmentLoadIdRef.current + 1
    assignmentLoadIdRef.current = loadId
    try {
      const triggerRect = trigger.getBoundingClientRect()
      const popoverWidth = Math.min(320, window.innerWidth - 16)
      const left = Math.max(
        8,
        Math.min(triggerRect.left, window.innerWidth - popoverWidth - 8),
      )
      const spaceBelow = window.innerHeight - triggerRect.bottom - 12
      const spaceAbove = triggerRect.top - 12
      const opensBelow = spaceBelow >= spaceAbove || spaceAbove < 180
      const maxHeight = Math.max(120, Math.min(440, opensBelow ? spaceBelow : spaceAbove))
      setPopoverPosition(opensBelow
        ? { left, top: triggerRect.bottom + 6, maxHeight }
        : { left, bottom: window.innerHeight - triggerRect.top + 6, maxHeight })
      assignmentTriggerRef.current = trigger
      setAssignmentTarget(instance)
      setAssignmentContext(null)
      setIsAssignmentLoading(true)
      setAssignmentError(null)
      setPhysicianSearch('')
      const response = await fetch(
        `${API_BASE}/schedule-blocks/${blockId}/build/shift-instances/${instance.id}/assignments/`,
        {
          credentials: 'include',
          cache: 'no-store',
        },
      )
      const data = await response.json().catch(() => null)
      if (!response.ok) {
        throw new Error(apiError(data, 'Unable to load shift assignments.'))
      }
      if (assignmentLoadIdRef.current !== loadId) {
        return
      }
      setAssignmentContext(data as AssignmentContext)
    } catch (assignmentLoadError) {
      if (assignmentLoadIdRef.current !== loadId) {
        return
      }
      setAssignmentError(
        assignmentLoadError instanceof Error
          ? assignmentLoadError.message
          : 'Unable to load shift assignments.',
      )
    } finally {
      if (assignmentLoadIdRef.current === loadId) {
        setIsAssignmentLoading(false)
      }
    }
  }

  const assignPhysician = async (physicianId: number) => {
    if (!assignmentContext) {
      return
    }
    try {
      setIsAssignmentSaving(true)
      setAssignmentError(null)
      const response = await fetch(
        `${API_BASE}/schedule-blocks/${blockId}/build/shift-instances/${assignmentContext.shift_instance.id}/assignments/`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          credentials: 'include',
          body: JSON.stringify({ physician_id: physicianId }),
        },
      )
      const data = await response.json().catch(() => null)
      if (!response.ok) {
        throw new Error(apiError(data, 'Unable to assign physician.'))
      }
      const nextAssignmentContext = data as AssignmentContext
      applyAssignmentContext(nextAssignmentContext)
      setPhysicianSearch('')
      if (nextAssignmentContext.shift_instance.open_count === 0) {
        closeAssignments()
      } else {
        setAssignmentTarget(nextAssignmentContext.shift_instance)
      }
    } catch (assignmentSaveError) {
      setAssignmentError(
        assignmentSaveError instanceof Error
          ? assignmentSaveError.message
          : 'Unable to assign physician.',
      )
    } finally {
      setIsAssignmentSaving(false)
    }
  }

  const removeAssignment = async (assignmentId: number) => {
    if (!assignmentContext) {
      return
    }
    try {
      setIsAssignmentSaving(true)
      setAssignmentError(null)
      const response = await fetch(
        `${API_BASE}/schedule-blocks/${blockId}/build/shift-instances/${assignmentContext.shift_instance.id}/assignments/${assignmentId}/`,
        {
          method: 'DELETE',
          credentials: 'include',
        },
      )
      const data = await response.json().catch(() => null)
      if (!response.ok) {
        throw new Error(apiError(data, 'Unable to remove physician.'))
      }
      applyAssignmentContext(data as AssignmentContext)
    } catch (assignmentSaveError) {
      setAssignmentError(
        assignmentSaveError instanceof Error
          ? assignmentSaveError.message
          : 'Unable to remove physician.',
      )
    } finally {
      setIsAssignmentSaving(false)
    }
  }

  if (isLoading && !context) {
    return <div className="scheduler-loading">Loading Schedule Build Workspace...</div>
  }

  if (!context || !visibleMonth || !blockStart || !blockEnd) {
    return (
      <div className="facilities-view-card">
        {error && <div className="facilities-error">{error}</div>}
        <button type="button" className="secondary" onClick={onBack}>Back to Schedule Blocks</button>
      </div>
    )
  }

  const canGenerate = context.schedule_block.build_status === 'PRE_BUILD'
    || context.schedule_block.build_status === 'BUILD'
  const canEditAssignments = context.schedule_block.build_status === 'BUILD'
    && context.selected_version?.status === 'BUILD'
  const eligiblePhysicians = assignmentContext?.eligible_physicians.filter(
    (physician) => physician.can_assign && !physician.already_assigned,
  ) ?? []
  const filteredPhysicians = sortedPhysicianMatches(
    eligiblePhysicians,
    physicianSearch,
  )
  const displayedShift = assignmentContext?.shift_instance ?? assignmentTarget

  return (
    <div className="facilities-view-card build-workspace">
      <div className="build-workspace-header">
        <div>
          <h2>{context.schedule_block.name}</h2>
          <div className="build-workspace-subtitle">
            {formatDate(context.schedule_block.start_date)} – {formatDate(context.schedule_block.end_date)}
            {' · '}
            Status: {context.schedule_block.build_status}
          </div>
        </div>
        <button type="button" className="secondary" onClick={onBack}>Back to Schedule Blocks</button>
      </div>

      {error && <div className="facilities-error">{error}</div>}
      {notice && <div className="contract-saved-banner">{notice}</div>}

      <div className="build-workspace-controls">
        <label className="facility-field">
          <span>Domain</span>
          <select
            value={selectedDomainId ?? ''}
            onChange={(event) => setSelectedDomainId(Number(event.target.value))}
            disabled={Boolean(context.selected_version)}
          >
            {!context.domains.length && <option value="">No active domains</option>}
            {context.domains.map((domain) => (
              <option key={domain.id} value={domain.id}>{domain.name}</option>
            ))}
          </select>
        </label>

        <label className="facility-field">
          <span>Schedule Version</span>
          <select
            value={context.selected_version?.id ?? ''}
            onChange={(event) => void fetchContext(Number(event.target.value))}
            disabled={!context.versions.length}
          >
            {!context.versions.length && <option value="">No version generated</option>}
            {context.versions.map((version) => (
              <option key={version.id} value={version.id}>
                {version.name} · {version.domain_name} · {version.status}
              </option>
            ))}
          </select>
        </label>

        <button
          type="button"
          className="primary-action"
          onClick={generateInstances}
          disabled={!canGenerate || !selectedDomainId || isGenerating}
        >
          {isGenerating ? 'Generating...' : 'Generate Shift Instances'}
        </button>
      </div>

      {!context.shift_instances.length ? (
        <div className="build-workspace-empty">
          <strong>No shift instances generated yet.</strong>
          <button
            type="button"
            className="primary-action"
            onClick={generateInstances}
            disabled={!canGenerate || !selectedDomainId || isGenerating}
          >
            Generate Shift Instances
          </button>
        </div>
      ) : (
        <>
          <div className="build-calendar-toolbar">
            <button
              type="button"
              onClick={() => setVisibleMonth(new Date(Date.UTC(visibleMonth.getUTCFullYear(), visibleMonth.getUTCMonth() - 1, 1)))}
              disabled={startOfMonthUtc(visibleMonth) <= startOfMonthUtc(blockStart)}
            >
              Previous
            </button>
            <strong>{monthLabel(visibleMonth)}</strong>
            <button
              type="button"
              onClick={() => setVisibleMonth(new Date(Date.UTC(visibleMonth.getUTCFullYear(), visibleMonth.getUTCMonth() + 1, 1)))}
              disabled={startOfMonthUtc(visibleMonth) >= startOfMonthUtc(blockEnd)}
            >
              Next
            </button>
          </div>

          <div className="build-calendar">
            <div className="build-calendar-weekdays">
              {['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'].map((day) => (
                <div key={day}>{day}</div>
              ))}
            </div>
            <div className="build-calendar-grid">
              {calendarCells.map((cell) => {
                if (!cell.date) {
                  return <div key={cell.key} className="build-day build-day-empty" />
                }
                if (!cell.inRange) {
                  return <div key={cell.key} className="build-day build-day-disabled" />
                }

                const dateKey = toIsoDateUtc(cell.date)
                const instances = instancesByDate.get(dateKey) ?? []
                return (
                  <div key={cell.key} className="build-day">
                    <div className="build-day-number">{cell.date.getUTCDate()}</div>
                    {instances.map((instance) => (
                      <button
                        key={instance.id}
                        type="button"
                        className="build-shift-chip"
                        aria-expanded={assignmentTarget?.id === instance.id}
                        onClick={(event) => void openAssignments(instance, event.currentTarget)}
                      >
                        {shiftLabel(instance)}
                      </button>
                    ))}
                  </div>
                )
              })}
            </div>
          </div>
        </>
      )}

      {displayedShift && popoverPosition && (
        <div
          ref={assignmentPopoverRef}
          className="assignment-popover"
          style={popoverPosition}
          role="dialog"
          aria-label={`Assignments for ${displayedShift.shift_template_name}`}
        >
          <div className="assignment-popover-header">
            <div>
              <strong>
                {displayedShift.facility_short_name}{' '}
                {formatTime(displayedShift.template_start_time)}-{formatTime(displayedShift.template_end_time)}
              </strong>
              <span>
                {formatDate(displayedShift.date)} · {displayedShift.assigned_count}/{displayedShift.required_staffing} staffed
              </span>
            </div>
            <button
              type="button"
              aria-label="Close assignment popover"
              disabled={isAssignmentSaving}
              onClick={closeAssignments}
            >
              ×
            </button>
          </div>

          {assignmentError && <div className="assignment-popover-error">{assignmentError}</div>}
          {isAssignmentLoading ? (
            <div className="assignment-popover-state">Loading assignments...</div>
          ) : assignmentContext && (
            <>
              {displayedShift.assignments.length > 0 && (
                <div className="assignment-popover-current">
                  <span className="assignment-popover-label">Assigned</span>
                  {displayedShift.assignments.map((assignment) => (
                    <div key={assignment.id} className="assignment-popover-assignment">
                      <span>{assignment.physician_name}</span>
                      <button
                        type="button"
                        disabled={isAssignmentSaving || !canEditAssignments}
                        onClick={() => void removeAssignment(assignment.id)}
                      >
                        Remove
                      </button>
                    </div>
                  ))}
                </div>
              )}

              {!canEditAssignments ? (
                <div className="assignment-popover-state">
                  Assignments can only be edited in a BUILD Schedule Version.
                </div>
              ) : displayedShift.open_count > 0 && (
                <div className="assignment-popover-picker">
                  <input
                    type="search"
                    value={physicianSearch}
                    onChange={(event) => setPhysicianSearch(event.target.value)}
                    placeholder="Search eligible physicians"
                    aria-label="Search eligible physicians"
                    autoFocus
                  />
                  <div className="assignment-popover-options">
                    {filteredPhysicians.length ? filteredPhysicians.map((physician) => (
                      <button
                        key={physician.id}
                        type="button"
                        disabled={isAssignmentSaving}
                        onClick={() => void assignPhysician(physician.id)}
                      >
                        {physician.name}
                      </button>
                    )) : <div className="assignment-popover-state">
                      {eligiblePhysicians.length
                        ? 'No matching eligible physicians.'
                        : 'No eligible physicians.'}
                    </div>}
                  </div>
                </div>
              )}
            </>
          )}
        </div>
      )}
    </div>
  )
}
