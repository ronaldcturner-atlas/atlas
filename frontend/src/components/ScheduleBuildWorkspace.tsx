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

type OptimizerSummary = {
  message?: string
  optimizer_run_id?: number
  optimizer_run_number?: number
  seed?: number
  assignments_cleared?: number
  total_score: number
  initial_score?: number
  final_score?: number
  improvement_count?: number
  iterations_run?: number
  unfilled_shift_count: number
  assignments_made: number
  candidate_rest_rejections?: number
  rest_violations_blocked?: number
  final_rest_violations?: number
  final_overlap_violations?: number
  final_duplicate_violations?: number
  final_overstaffed_violations?: number
  same_shift_violations_count?: number
  night_violations_count?: number
  total_night_shifts?: number
  max_nights_assigned_to_one_physician?: number
  night_fix_improvements?: number
  night_unresolved_reasons?: string[]
  score_breakdown?: {
    coverage_score: number
    workload_score: number
    underutilization_score?: number
    request_score: number
    rest_score: number
    overlap_score: number
    invalid_assignment_score?: number
    consecutive_days_score?: number
    same_shift_score?: number
    night_score?: number
    weekend_score?: number
    facility_distribution_score?: number
    total_score: number
  }
  debug?: Record<string, unknown>
  request_violations_summary?: {
    violations: number
    rewards: number
  }
  workload_summary?: Array<{
    physician_id: number
    physician_name: string
    assigned_hours: number
    assigned_shifts: number
    night_shifts?: number
    target_units: 'HOURS' | 'SHIFTS' | null
    target: number | null
  }>
}

type OptimizerRun = {
  id: number
  schedule_version: number
  run_number: number
  created_at: string
  status: 'RUNNING' | 'COMPLETED' | 'FAILED'
  seed: number | string | null
  initial_score: string | number | null
  final_score: string | number | null
  is_active: boolean
  optimizer_summary?: OptimizerSummary
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
  optimizer_summary?: OptimizerSummary | null
  optimizer_runs?: OptimizerRun[]
  selected_optimizer_run?: OptimizerRun | null
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

function formatTimestamp(value: string) {
  return new Date(value).toLocaleString('en-US', {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  })
}

function formatScore(value: string | number | null | undefined) {
  if (value === null || value === undefined || value === '') {
    return '-'
  }
  return Number(value).toFixed(1)
}

function optimizerRunLabel(run: OptimizerRun) {
  return `Run ${run.run_number} - ${formatScore(run.final_score)} - ${formatTimestamp(run.created_at)} - seed ${run.seed ?? '-'}`
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
  const [isOptimizing, setIsOptimizing] = useState(false)
  const [clearingAction, setClearingAction] = useState<'optimizer' | 'all' | null>(null)
  const [deletingRunId, setDeletingRunId] = useState<number | null>(null)
  const [optimizerSummary, setOptimizerSummary] = useState<OptimizerSummary | null>(null)
  const [runSelectorId, setRunSelectorId] = useState<number | null>(null)
  const [showRunHistory, setShowRunHistory] = useState(false)
  const [showScoreDetails, setShowScoreDetails] = useState(false)
  const [showWorkloadDetails, setShowWorkloadDetails] = useState(false)
  const [showOptimizerDebug, setShowOptimizerDebug] = useState(false)
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

  const fetchContext = async (
    versionId?: number,
    options: { preserveError?: boolean; quiet?: boolean; rethrow?: boolean; optimizerRunId?: number } = {},
  ) => {
    try {
      if (!options.quiet) {
        setIsLoading(true)
      }
      if (!options.preserveError) {
        setError(null)
      }
      const params = new URLSearchParams()
      if (versionId) {
        params.set('version_id', String(versionId))
      }
      if (options.optimizerRunId) {
        params.set('optimizer_run_id', String(options.optimizerRunId))
      }
      const query = params.toString() ? `?${params.toString()}` : ''
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
      setOptimizerSummary(nextContext.optimizer_summary ?? null)
      setSelectedDomainId(
        nextContext.selected_version?.domain
        ?? nextContext.domains[0]?.id
        ?? null,
      )
      setVisibleMonth(startOfMonthUtc(parseIsoDateToUtc(nextContext.schedule_block.start_date)))
    } catch (loadError) {
      if (!options.preserveError) {
        setError(loadError instanceof Error ? loadError.message : 'Unable to load the Schedule Build Workspace.')
      }
      if (options.rethrow) {
        throw loadError
      }
    } finally {
      if (!options.quiet) {
        setIsLoading(false)
      }
    }
  }

  const selectedOptimizerRun = context?.selected_optimizer_run ?? null
  const optimizerRuns = context?.optimizer_runs ?? []
  const selectedRunForActions = optimizerRuns.find((run) => run.id === runSelectorId) ?? selectedOptimizerRun

  const updateOptimizerRunUrl = (runId: number | null) => {
    const url = new URL(window.location.href)
    if (runId) {
      url.searchParams.set('optimizer_run_id', String(runId))
    } else {
      url.searchParams.delete('optimizer_run_id')
    }
    window.history.replaceState(null, '', `${url.pathname}${url.search}`)
  }

  const viewOptimizerRun = async (run: OptimizerRun) => {
    try {
      closeAssignments()
      setError(null)
      setNotice(null)
      await fetchContext(run.schedule_version, { optimizerRunId: run.id, quiet: true })
      updateOptimizerRunUrl(run.id)
      setNotice(`Viewing Run ${run.run_number}.`)
    } catch (viewError) {
      setError(viewError instanceof Error ? viewError.message : 'Unable to view optimizer run.')
    }
  }

  const activateOptimizerRun = async (runId: number) => {
    const versionId = context?.selected_version?.id
    if (!versionId) {
      return
    }
    try {
      closeAssignments()
      setError(null)
      setNotice(null)
      const response = await fetch(`${API_BASE}/optimizer-runs/${runId}/activate/`, {
        method: 'POST',
        credentials: 'include',
      })
      const data = await response.json().catch(() => null)
      if (!response.ok) {
        throw new Error(apiError(data, 'Unable to activate optimizer run.'))
      }
      setNotice(`Run ${(data as OptimizerRun).run_number} is active.`)
      await fetchContext(versionId, { optimizerRunId: runId, quiet: true })
      updateOptimizerRunUrl(runId)
    } catch (activateError) {
      setError(activateError instanceof Error ? activateError.message : 'Unable to activate optimizer run.')
    }
  }

  const deleteOptimizerRun = async (run: OptimizerRun) => {
    const versionId = context?.selected_version?.id
    if (!versionId || run.is_active) {
      return
    }
    const confirmed = window.confirm(`Delete Run ${run.run_number}? This removes only that run and its optimizer-created assignments.`)
    if (!confirmed) {
      return
    }
    try {
      closeAssignments()
      setDeletingRunId(run.id)
      setError(null)
      setNotice(null)
      const response = await fetch(`${API_BASE}/optimizer-runs/${run.id}/`, {
        method: 'DELETE',
        credentials: 'include',
      })
      const data = await response.json().catch(() => null)
      if (!response.ok) {
        throw new Error(apiError(data, 'Unable to delete optimizer run.'))
      }
      const fallbackRun = optimizerRuns.find((item) => item.is_active && item.id !== run.id)
        ?? optimizerRuns.find((item) => item.id !== run.id)
        ?? null
      await fetchContext(versionId, {
        optimizerRunId: fallbackRun?.id,
        quiet: true,
      })
      updateOptimizerRunUrl(fallbackRun?.id ?? null)
      setNotice(data?.message ?? `Run ${run.run_number} deleted.`)
    } catch (deleteError) {
      setError(deleteError instanceof Error ? deleteError.message : 'Unable to delete optimizer run.')
    } finally {
      setDeletingRunId(null)
    }
  }

  useEffect(() => {
    closeAssignments()
    const optimizerRunId = new URLSearchParams(window.location.search).get('optimizer_run_id')
    void fetchContext(undefined, {
      optimizerRunId: optimizerRunId ? Number(optimizerRunId) : undefined,
    })
  }, [blockId])

  useEffect(() => {
    if (selectedOptimizerRun) {
      setRunSelectorId(selectedOptimizerRun.id)
    } else if (optimizerRuns.length) {
      setRunSelectorId(optimizerRuns[0].id)
    } else {
      setRunSelectorId(null)
    }
  }, [selectedOptimizerRun?.id, optimizerRuns])

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
      updateOptimizerRunUrl(null)
    } catch (generateError) {
      setError(generateError instanceof Error ? generateError.message : 'Unable to generate shift instances.')
    } finally {
      setIsGenerating(false)
    }
  }

  const runOptimizer = async () => {
    const versionId = context.selected_version?.id
    if (!versionId) {
      setError('Select a BUILD Schedule Version before running the optimizer.')
      return
    }

    let optimizeErrorMessage: string | null = null
    let completedOptimizerRunId: number | undefined
    try {
      closeAssignments()
      setIsOptimizing(true)
      setError(null)
      setNotice(null)
      setOptimizerSummary(null)
      setShowScoreDetails(false)
      setShowWorkloadDetails(false)
      setShowOptimizerDebug(false)
      const response = await fetch(
        `${API_BASE}/schedule-versions/${versionId}/run-optimizer/`,
        {
          method: 'POST',
          credentials: 'include',
        },
      )
      const data = await response.json().catch(() => null)
      if (!response.ok) {
        throw new Error(apiError(data, 'Unable to run optimizer.'))
      }

      const summary = data as OptimizerSummary
      completedOptimizerRunId = summary.optimizer_run_id
      setOptimizerSummary(summary)
      setNotice((data as OptimizerSummary).message ?? 'Optimizer v0 completed.')
    } catch (optimizeError) {
      optimizeErrorMessage = optimizeError instanceof Error ? optimizeError.message : 'Unable to run optimizer.'
      setError(optimizeErrorMessage)
    } finally {
      try {
        await fetchContext(versionId, {
          preserveError: Boolean(optimizeErrorMessage),
          quiet: true,
          rethrow: true,
          optimizerRunId: completedOptimizerRunId,
        })
        if (completedOptimizerRunId) {
          updateOptimizerRunUrl(completedOptimizerRunId)
        }
      } catch {
        if (!optimizeErrorMessage) {
          setError('Optimizer completed, but the Schedule Build Workspace could not refresh.')
        }
      }
      setIsOptimizing(false)
    }
  }

  const clearScheduleAssignments = async (clearType: 'optimizer' | 'all') => {
    const versionId = context.selected_version?.id
    if (!versionId) {
      setError('Select a BUILD Schedule Version before clearing assignments.')
      return
    }

    const confirmed = window.confirm(
      clearType === 'optimizer'
        ? 'Clear optimizer-generated assignments for this schedule version?'
        : 'Clear ALL assignments for this schedule version? This is for development/testing only.',
    )
    if (!confirmed) {
      return
    }

    let clearErrorMessage: string | null = null
    try {
      closeAssignments()
      setClearingAction(clearType)
      setError(null)
      setNotice(null)
      setOptimizerSummary(null)
      setShowWorkloadDetails(false)
      setShowOptimizerDebug(false)
      const endpoint = clearType === 'optimizer'
        ? 'clear-optimizer-assignments'
        : 'clear-all-assignments'
      const response = await fetch(
        `${API_BASE}/schedule-blocks/${blockId}/build/versions/${versionId}/${endpoint}/`,
        {
          method: 'POST',
          credentials: 'include',
        },
      )
      const data = await response.json().catch(() => null)
      if (!response.ok) {
        throw new Error(apiError(data, 'Unable to clear assignments.'))
      }

      setOptimizerSummary(data as OptimizerSummary)
      setNotice((data as OptimizerSummary).message ?? 'Assignments cleared.')
    } catch (clearError) {
      clearErrorMessage = clearError instanceof Error ? clearError.message : 'Unable to clear assignments.'
      setError(clearErrorMessage)
    } finally {
      try {
        await fetchContext(versionId, {
          preserveError: Boolean(clearErrorMessage),
          quiet: true,
          rethrow: true,
        })
      } catch {
        if (!clearErrorMessage) {
          setError('Assignments were cleared, but the Schedule Build Workspace could not refresh.')
        }
      }
      setClearingAction(null)
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
  const canOptimize = canEditAssignments && context.shift_instances.length > 0
  const canClearAssignments = canEditAssignments && context.shift_instances.length > 0
  const isMutatingBuild = isGenerating || isOptimizing || clearingAction !== null || deletingRunId !== null
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
          disabled={!canGenerate || !selectedDomainId || isMutatingBuild}
        >
          {isGenerating ? 'Generating...' : 'Generate Shift Instances'}
        </button>

        <button
          type="button"
          className="primary-action"
          onClick={runOptimizer}
          disabled={!canOptimize || isMutatingBuild}
        >
          {isOptimizing ? 'Running...' : 'Run Optimizer v0'}
        </button>

        <button
          type="button"
          className="secondary"
          onClick={() => void clearScheduleAssignments('optimizer')}
          disabled={!canClearAssignments || isMutatingBuild}
        >
          {clearingAction === 'optimizer' ? 'Clearing...' : 'Clear Optimizer Assignments'}
        </button>

        <button
          type="button"
          className="secondary danger-action"
          onClick={() => void clearScheduleAssignments('all')}
          disabled={!canClearAssignments || isMutatingBuild}
        >
          {clearingAction === 'all' ? 'Clearing...' : 'Clear All Assignments'}
        </button>

      </div>

      {optimizerRuns.length > 0 && selectedRunForActions && (
        <div className="optimizer-run-selector-panel">
          <div className="optimizer-run-selector-main">
            <label className="facility-field optimizer-run-select">
              <span>Viewing optimizer run</span>
              <select
                value={runSelectorId ?? ''}
                onChange={(event) => setRunSelectorId(Number(event.target.value))}
                disabled={isMutatingBuild}
              >
                {optimizerRuns.map((run) => (
                  <option key={run.id} value={run.id}>
                    {optimizerRunLabel(run)}
                  </option>
                ))}
              </select>
            </label>
            <div className="optimizer-run-status">
              {selectedOptimizerRun && (
                <span>Viewing Run {selectedOptimizerRun.run_number}</span>
              )}
              {selectedRunForActions.is_active && <strong>Active</strong>}
              {!selectedRunForActions.is_active && <span>Inactive</span>}
            </div>
            <div className="optimizer-run-actions">
              <button
                type="button"
                className="secondary"
                onClick={() => void viewOptimizerRun(selectedRunForActions)}
                disabled={isMutatingBuild || selectedOptimizerRun?.id === selectedRunForActions.id}
              >
                View
              </button>
              <button
                type="button"
                className="secondary"
                onClick={() => void activateOptimizerRun(selectedRunForActions.id)}
                disabled={isMutatingBuild || selectedRunForActions.is_active || selectedRunForActions.status !== 'COMPLETED'}
              >
                Activate
              </button>
              <a
                className="secondary build-workspace-link-button"
                href={`/schedule-versions/${selectedRunForActions.schedule_version}/violations?optimizer_run_id=${selectedRunForActions.id}`}
              >
                Violations
              </a>
              {!selectedRunForActions.is_active && (
                <button
                  type="button"
                  className="secondary danger-action"
                  onClick={() => void deleteOptimizerRun(selectedRunForActions)}
                  disabled={isMutatingBuild}
                >
                  {deletingRunId === selectedRunForActions.id ? 'Deleting...' : 'Delete'}
                </button>
              )}
            </div>
          </div>
          <button
            type="button"
            className="secondary optimizer-history-toggle"
            onClick={() => setShowRunHistory((current) => !current)}
            aria-expanded={showRunHistory}
          >
            {showRunHistory ? 'Hide run history' : `Show run history (${optimizerRuns.length})`}
          </button>
          {showRunHistory && (
            <div className="optimizer-runs-list">
              {optimizerRuns.map((run) => (
                <div
                  className={`optimizer-run-row${selectedOptimizerRun?.id === run.id ? ' optimizer-run-row-selected' : ''}`}
                  key={run.id}
                >
                  <strong>Run {run.run_number}</strong>
                  <span>{formatScore(run.final_score)} final</span>
                  <span>{formatTimestamp(run.created_at)}</span>
                  <span>Seed {run.seed ?? '-'}</span>
                  <span>{run.is_active ? 'Active' : 'Inactive'}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {optimizerSummary && (
        <section className="optimizer-summary-card" aria-live="polite">
          <div className="optimizer-summary-grid">
            <div>
              <span>Initial score</span>
              <strong>{(optimizerSummary.initial_score ?? optimizerSummary.total_score).toFixed(1)}</strong>
            </div>
            <div>
              <span>Final score</span>
              <strong>{(optimizerSummary.final_score ?? optimizerSummary.total_score).toFixed(1)}</strong>
            </div>
            <div>
              <span>Improvements</span>
              <strong>{optimizerSummary.improvement_count ?? 0}</strong>
            </div>
            <div>
              <span>Iterations</span>
              <strong>{optimizerSummary.iterations_run ?? 0}</strong>
            </div>
            <div>
              <span>Assignments made</span>
              <strong>{optimizerSummary.assignments_made}</strong>
            </div>
            <div>
              <span>Unfilled shifts</span>
              <strong>{optimizerSummary.unfilled_shift_count}</strong>
            </div>
            <div>
              <span>Request score</span>
              <strong>{(optimizerSummary.score_breakdown?.request_score ?? 0).toFixed(1)}</strong>
            </div>
            <div>
              <span>Workload score</span>
              <strong>{(optimizerSummary.score_breakdown?.workload_score ?? 0).toFixed(1)}</strong>
            </div>
            <div>
              <span>Night score</span>
              <strong>{(optimizerSummary.score_breakdown?.night_score ?? 0).toFixed(1)}</strong>
            </div>
            <div>
              <span>Same shift score</span>
              <strong>{(optimizerSummary.score_breakdown?.same_shift_score ?? 0).toFixed(1)}</strong>
            </div>
            <div>
              <span>Coverage score</span>
              <strong>{(optimizerSummary.score_breakdown?.coverage_score ?? 0).toFixed(1)}</strong>
            </div>
            <div>
              <span>Rest / overlap</span>
              <strong>{optimizerSummary.final_rest_violations ?? 0} / {optimizerSummary.final_overlap_violations ?? 0}</strong>
            </div>
          </div>

          <div className="optimizer-detail-actions">
            <button
              type="button"
              className="secondary"
              onClick={() => setShowScoreDetails((current) => !current)}
              aria-expanded={showScoreDetails}
            >
              {showScoreDetails ? 'Hide score details' : 'Show score details'}
            </button>
            {optimizerSummary.workload_summary?.length ? (
              <button
                type="button"
                className="secondary"
                onClick={() => setShowWorkloadDetails((current) => !current)}
                aria-expanded={showWorkloadDetails}
              >
                {showWorkloadDetails ? 'Hide workload details' : 'Show workload details'}
              </button>
            ) : null}
            {optimizerSummary.debug && (
              <button
                type="button"
                className="secondary"
                onClick={() => setShowOptimizerDebug((current) => !current)}
                aria-expanded={showOptimizerDebug}
              >
                {showOptimizerDebug ? 'Hide optimizer debug' : 'Show optimizer debug'}
              </button>
            )}
          </div>

          {showScoreDetails && (
            <div className="optimizer-extra-grid">
              <div>
                <span>Underutilization score</span>
                <strong>{(optimizerSummary.score_breakdown?.underutilization_score ?? 0).toFixed(1)}</strong>
              </div>
              <div>
                <span>Consecutive days score</span>
                <strong>{(optimizerSummary.score_breakdown?.consecutive_days_score ?? 0).toFixed(1)}</strong>
              </div>
              <div>
                <span>Weekend score</span>
                <strong>{(optimizerSummary.score_breakdown?.weekend_score ?? 0).toFixed(1)}</strong>
              </div>
              <div>
                <span>Facility score</span>
                <strong>{(optimizerSummary.score_breakdown?.facility_distribution_score ?? 0).toFixed(1)}</strong>
              </div>
              <div>
                <span>Request violations</span>
                <strong>{optimizerSummary.request_violations_summary?.violations ?? 0}</strong>
              </div>
              <div>
                <span>Same shift violations</span>
                <strong>{optimizerSummary.same_shift_violations_count ?? 0}</strong>
              </div>
              <div>
                <span>Night violations</span>
                <strong>{optimizerSummary.night_violations_count ?? 0}</strong>
              </div>
              <div>
                <span>Total night shifts</span>
                <strong>{optimizerSummary.total_night_shifts ?? 0}</strong>
              </div>
              <div>
                <span>Max nights assigned</span>
                <strong>{optimizerSummary.max_nights_assigned_to_one_physician ?? 0}</strong>
              </div>
              <div>
                <span>Night fixes kept</span>
                <strong>{optimizerSummary.night_fix_improvements ?? 0}</strong>
              </div>
              <div>
                <span>Rest rejections</span>
                <strong>{optimizerSummary.candidate_rest_rejections ?? optimizerSummary.rest_violations_blocked ?? 0}</strong>
              </div>
              {optimizerSummary.assignments_cleared !== undefined && (
                <div>
                  <span>Assignments cleared</span>
                  <strong>{optimizerSummary.assignments_cleared}</strong>
                </div>
              )}
            </div>
          )}

          {showWorkloadDetails && optimizerSummary.workload_summary?.length ? (
            <div className="optimizer-workload-table-wrap">
              <table className="optimizer-workload-table">
                <thead>
                  <tr>
                    <th>Name</th>
                    <th>Shifts</th>
                    <th>Hours</th>
                    <th>Nights</th>
                  </tr>
                </thead>
                <tbody>
                  {optimizerSummary.workload_summary.map((item) => (
                    <tr key={item.physician_id}>
                      <td>{item.physician_name}</td>
                      <td>{item.assigned_shifts}</td>
                      <td>{item.assigned_hours.toFixed(1)}</td>
                      <td>{item.night_shifts ?? 0}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}

          {showOptimizerDebug && optimizerSummary.debug && (
            <pre className="optimizer-debug-details">
              {JSON.stringify(optimizerSummary.debug, null, 2)}
            </pre>
          )}
        </section>
      )}

      {!context.shift_instances.length ? (
        <div className="build-workspace-empty">
          <strong>No shift instances generated yet.</strong>
          <button
            type="button"
            className="primary-action"
            onClick={generateInstances}
            disabled={!canGenerate || !selectedDomainId || isMutatingBuild}
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
