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
  score_is_stale: boolean
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
  is_locked_open: boolean
  assignments: ShiftAssignment[]
}

type ShiftAssignment = {
  id: number
  physician: number
  physician_name: string
  assignment_source: 'MANUAL' | 'OPTIMIZER'
  is_locked: boolean
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
  runtime_seconds?: number
  timed_out?: boolean
  stopped_reason?: string
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
  debug?: Record<string, unknown> | string
  request_violations_summary?: {
    violations: number
    rewards: number
  }
  workload_summary?: Array<{
    physician_id: number
    physician_name: string
    contract_name?: string | null
    assigned_hours: number
    assigned_shifts: number
    night_shifts?: number
    target_units: 'HOURS' | 'SHIFTS' | null
    target: number | null
    effective_workload_range?: {
      period_type: string
      period_start: string
      period_end: string
      units: 'HOURS' | 'SHIFTS'
      raw_min_value: number | null
      raw_max_value: number | null
      min_value: number | null
      max_value: number | null
      debug_warning?: string | null
    } | null
    deviation?: number
    deviation_direction?: string
    score_contribution?: number
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
  score_is_stale: boolean
  copied_from_run: number | null
  copied_from_run_number: number | null
  run_kind: 'OPTIMIZER' | 'COPY'
  locked_open_shift_instance_ids: number[]
  start_mode: 'CURRENT_SCHEDULE' | 'FRESH_FILL'
  optimizer_summary?: OptimizerSummary
  optimizer_debug?: OptimizerSummary['debug']
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

function isCompletedOptimizerRun(run: OptimizerRun) {
  return run.status === 'COMPLETED'
}

function isTimedOutOptimizerRun(run: OptimizerRun) {
  return Boolean(run.optimizer_summary?.timed_out || run.optimizer_debug?.timed_out)
}

function optimizerRunStatusLabel(run: OptimizerRun) {
  if (isTimedOutOptimizerRun(run)) {
    return 'FAILED/TIMEOUT'
  }
  return run.status
}

function optimizerRunScoreLabel(run: OptimizerRun) {
  if (!isCompletedOptimizerRun(run)) {
    return run.final_score === null || run.final_score === undefined
      ? 'No completed score'
      : `${formatScore(run.final_score)} partial score`
  }
  return `${formatScore(run.final_score)} final score`
}

function optimizerRunLabel(run: OptimizerRun) {
  const copyLabel = run.copied_from_run_number ? ` - Copy of Run ${run.copied_from_run_number}` : ''
  const startLabel = run.start_mode === 'CURRENT_SCHEDULE' ? 'Current schedule' : 'Fresh fill'
  if (!isCompletedOptimizerRun(run)) {
    return `Run ${run.run_number} - ${optimizerRunStatusLabel(run)} - ${formatTimestamp(run.created_at)} - seed ${run.seed ?? '-'}`
  }
  return `Run ${run.run_number}${copyLabel} - ${startLabel} - ${formatScore(run.final_score)} - ${formatTimestamp(run.created_at)} - seed ${run.seed ?? '-'}`
}

function workloadRangeLabel(range: OptimizerSummary['workload_summary'][number]['effective_workload_range']) {
  if (!range) {
    return 'No workload range'
  }
  const value = (item: number | null | undefined) => item === null || item === undefined ? '-' : item
  const units = range.units === 'HOURS' ? 'h' : ' shifts'
  return `${range.period_type} raw ${value(range.raw_min_value)}–${value(range.raw_max_value)}${units} · effective ${value(range.min_value)}–${value(range.max_value)}${units}`
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
  const lockedLabel = instance.is_locked_open || instance.assignments.some((assignment) => assignment.is_locked) ? 'Locked' : ''
  return [
    instance.facility_short_name,
    `${formatTime(instance.template_start_time)}-${formatTime(instance.template_end_time)}`,
    assignedLabel,
    lockedLabel,
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
  const [optimizerStartMode, setOptimizerStartMode] = useState<'CURRENT_SCHEDULE' | 'FRESH_FILL'>('FRESH_FILL')
  const [isRecalculatingScore, setIsRecalculatingScore] = useState(false)
  const [isSavingCopy, setIsSavingCopy] = useState(false)
  const [clearingAction, setClearingAction] = useState<'optimizer' | 'all' | null>(null)
  const [deletingRunId, setDeletingRunId] = useState<number | null>(null)
  const [optimizerSummary, setOptimizerSummary] = useState<OptimizerSummary | null>(null)
  const [selectedOptimizerRunId, setSelectedOptimizerRunIdState] = useState<number | null>(null)
  const [showRunHistory, setShowRunHistory] = useState(false)
  const [showScoreDetails, setShowScoreDetails] = useState(false)
  const [showWorkloadDetails, setShowWorkloadDetails] = useState(false)
  const [showOptimizerDebug, setShowOptimizerDebug] = useState(false)
  const [debugCopyStatus, setDebugCopyStatus] = useState<'idle' | 'copied' | 'failed'>('idle')
  const [summaryCopyStatus, setSummaryCopyStatus] = useState<'idle' | 'copied' | 'failed'>('idle')
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [assignmentContext, setAssignmentContext] = useState<AssignmentContext | null>(null)
  const [assignmentTarget, setAssignmentTarget] = useState<ShiftInstance | null>(null)
  const [isAssignmentLoading, setIsAssignmentLoading] = useState(false)
  const [isAssignmentSaving, setIsAssignmentSaving] = useState(false)
  const [assignmentError, setAssignmentError] = useState<string | null>(null)
  const [physicianSearch, setPhysicianSearch] = useState('')
  const [editingAssignmentId, setEditingAssignmentId] = useState<number | null>(null)
  const [selectedPhysicianId, setSelectedPhysicianId] = useState<number | null>(null)
  const [lockAssignment, setLockAssignment] = useState(false)
  const [lockOpen, setLockOpen] = useState(false)
  const [popoverPosition, setPopoverPosition] = useState<PopoverPosition | null>(null)
  const assignmentPopoverRef = useRef<HTMLDivElement | null>(null)
  const assignmentTriggerRef = useRef<HTMLButtonElement | null>(null)
  const assignmentLoadIdRef = useRef(0)
  const selectedOptimizerRunIdRef = useRef<number | null>(null)

  const setSelectedOptimizerRunId = (runId: number | null) => {
    selectedOptimizerRunIdRef.current = runId
    setSelectedOptimizerRunIdState(runId)
  }

  const closeAssignments = () => {
    assignmentLoadIdRef.current += 1
    setAssignmentTarget(null)
    setAssignmentContext(null)
    setAssignmentError(null)
    setPhysicianSearch('')
    setEditingAssignmentId(null)
    setSelectedPhysicianId(null)
    setLockAssignment(false)
    setLockOpen(false)
    setPopoverPosition(null)
    assignmentTriggerRef.current = null
  }

  const updateOptimizerRunUrl = (runId: number | null) => {
    const url = new URL(window.location.href)
    if (runId) {
      url.searchParams.set('optimizer_run_id', String(runId))
    } else {
      url.searchParams.delete('optimizer_run_id')
    }
    window.history.replaceState(null, '', `${url.pathname}${url.search}`)
  }

  const copyTextToClipboard = async (text: string) => {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text)
      return
    }

    const textArea = document.createElement('textarea')
    textArea.value = text
    textArea.setAttribute('readonly', 'true')
    textArea.style.position = 'fixed'
    textArea.style.left = '-9999px'
    textArea.style.top = '0'
    document.body.appendChild(textArea)
    textArea.focus()
    textArea.select()
    const copied = document.execCommand('copy')
    document.body.removeChild(textArea)
    if (!copied) {
      throw new Error('Copy command failed.')
    }
  }

  const debugJsonText = () => {
    if (!optimizerSummary?.debug) {
      return ''
    }
    return typeof optimizerSummary.debug === 'string'
      ? optimizerSummary.debug
      : JSON.stringify(optimizerSummary.debug, null, 2)
  }

  const scoreSummaryText = () => {
    if (!optimizerSummary) {
      return ''
    }
    const breakdown = optimizerSummary.score_breakdown ?? {}
    const debug = typeof optimizerSummary.debug === 'string' ? {} : optimizerSummary.debug ?? {}
    const rows = [
      ['run id', optimizerSummary.optimizer_run_id ?? selectedOptimizerRunId ?? ''],
      ['seed', optimizerSummary.seed ?? debug.seed ?? ''],
      ['initial score', optimizerSummary.initial_score ?? optimizerSummary.total_score],
      ['final score', optimizerSummary.final_score ?? optimizerSummary.total_score],
      ['iterations', optimizerSummary.iterations_run ?? 0],
      ['runtime seconds', optimizerSummary.runtime_seconds ?? debug.runtime_seconds ?? ''],
      ['request score', breakdown.request_score ?? 0],
      ['workload score', breakdown.workload_score ?? 0],
      ['night score', breakdown.night_score ?? 0],
      ['same shift score', breakdown.same_shift_score ?? 0],
      ['coverage score', breakdown.coverage_score ?? 0],
      ['rest / overlap score', `${breakdown.rest_score ?? 0} / ${breakdown.overlap_score ?? 0}`],
      ['unfilled shifts', optimizerSummary.unfilled_shift_count],
      ['assignments made', optimizerSummary.assignments_made],
      ['timed out', optimizerSummary.timed_out ?? debug.timed_out ?? false],
      ['stopped reason', optimizerSummary.stopped_reason ?? debug.stopped_reason ?? ''],
    ]
    return rows.map(([label, value]) => `${label}: ${String(value)}`).join('\n')
  }

  const copyOptimizerDebug = async () => {
    try {
      await copyTextToClipboard(debugJsonText())
      setDebugCopyStatus('copied')
      window.setTimeout(() => setDebugCopyStatus('idle'), 1600)
    } catch {
      setDebugCopyStatus('failed')
    }
  }

  const copyScoreSummary = async () => {
    try {
      await copyTextToClipboard(scoreSummaryText())
      setSummaryCopyStatus('copied')
      window.setTimeout(() => setSummaryCopyStatus('idle'), 1600)
    } catch {
      setSummaryCopyStatus('failed')
    }
  }

  const fetchContext = async (
    versionId?: number,
    options: { preserveError?: boolean; quiet?: boolean; rethrow?: boolean; optimizerRunId?: number | null } = {},
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
      const requestedOptimizerRunId = (
        options.optimizerRunId !== undefined
          ? options.optimizerRunId
          : selectedOptimizerRunIdRef.current
      )
      if (requestedOptimizerRunId) {
        params.set('optimizer_run_id', String(requestedOptimizerRunId))
      }
      const query = params.toString() ? `?${params.toString()}` : ''
      console.info('Fetching workspace for optimizer_run_id', requestedOptimizerRunId ?? 'active/default')
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
      const returnedRunId = nextContext.selected_optimizer_run?.id ?? null
      if (requestedOptimizerRunId !== undefined || selectedOptimizerRunIdRef.current === null) {
        setSelectedOptimizerRunId(returnedRunId)
      }
      if (requestedOptimizerRunId !== undefined && returnedRunId !== requestedOptimizerRunId) {
        updateOptimizerRunUrl(returnedRunId)
      }
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
  const completedOptimizerRuns = optimizerRuns.filter(isCompletedOptimizerRun)
  const selectedRunForActions = completedOptimizerRuns.find((run) => run.id === selectedOptimizerRunId)
    ?? selectedOptimizerRun
  const activeOptimizerRun = optimizerRuns.find((run) => run.is_active) ?? null

  const selectOptimizerRun = async (run: OptimizerRun) => {
    if (!isCompletedOptimizerRun(run)) {
      return
    }
    try {
      closeAssignments()
      setSelectedOptimizerRunId(run.id)
      updateOptimizerRunUrl(run.id)
      setError(null)
      setNotice(null)
      await fetchContext(run.schedule_version, { optimizerRunId: run.id, quiet: true })
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
      setSelectedOptimizerRunId(runId)
      updateOptimizerRunUrl(runId)
      await fetchContext(versionId, { optimizerRunId: runId, quiet: true })
    } catch (activateError) {
      setError(activateError instanceof Error ? activateError.message : 'Unable to activate optimizer run.')
    }
  }

  const deleteOptimizerRun = async (run: OptimizerRun) => {
    const versionId = context?.selected_version?.id
    if (!versionId || run.is_active) {
      return
    }
    const confirmed = window.confirm('Delete this optimizer run? This cannot be undone.')
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
      const deletedSelectedRun = selectedOptimizerRunId === run.id
      const currentRun = completedOptimizerRuns.find((item) => item.id === selectedOptimizerRunId && item.id !== run.id)
      const fallbackRun = completedOptimizerRuns.find((item) => item.is_active && item.id !== run.id)
        ?? completedOptimizerRuns.find((item) => item.id !== run.id)
        ?? null
      const nextRun = deletedSelectedRun ? fallbackRun : currentRun
      setSelectedOptimizerRunId(nextRun?.id ?? null)
      updateOptimizerRunUrl(nextRun?.id ?? null)
      await fetchContext(versionId, {
        optimizerRunId: nextRun?.id,
        quiet: true,
      })
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
    const parsedOptimizerRunId = optimizerRunId && Number.isFinite(Number(optimizerRunId))
      ? Number(optimizerRunId)
      : null
    setSelectedOptimizerRunId(parsedOptimizerRunId)
    void fetchContext(undefined, {
      optimizerRunId: parsedOptimizerRunId,
    })
  }, [blockId])

  useEffect(() => {
    setDebugCopyStatus('idle')
    setSummaryCopyStatus('idle')
  }, [optimizerSummary?.optimizer_run_id, optimizerSummary?.seed])

  useEffect(() => {
    const hasViewedAssignments = (context?.shift_instances ?? []).some((instance) => instance.assignments.length > 0)
    setOptimizerStartMode(hasViewedAssignments ? 'CURRENT_SCHEDULE' : 'FRESH_FILL')
  }, [context?.selected_optimizer_run?.id, context?.selected_version?.id])

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
      setSelectedOptimizerRunId(null)
      updateOptimizerRunUrl(null)
      await fetchContext(data.schedule_version.id, { optimizerRunId: null })
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
          headers: { 'Content-Type': 'application/json' },
          credentials: 'include',
          body: JSON.stringify({
            schedule_version_id: versionId,
            currently_viewed_run_id: selectedOptimizerRunId,
            start_mode: optimizerStartMode,
          }),
        },
      )
      const data = await response.json().catch(() => null)
      if (!response.ok) {
        throw new Error(apiError(data, 'Unable to run optimizer.'))
      }

      const summary = data as OptimizerSummary
      completedOptimizerRunId = summary.optimizer_run_id
      setSelectedOptimizerRunId(completedOptimizerRunId ?? null)
      updateOptimizerRunUrl(completedOptimizerRunId ?? null)
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
      } catch {
        if (!optimizeErrorMessage) {
          setError('Optimizer completed, but the Schedule Build Workspace could not refresh.')
        }
      }
      setIsOptimizing(false)
    }
  }

  const recalculateScore = async () => {
    const versionId = context?.selected_version?.id
    const runId = selectedRunForActions?.id
    if (!versionId || !runId) return
    try {
      setIsRecalculatingScore(true)
      setError(null)
      setNotice(null)
      const response = await fetch(`${API_BASE}/schedule-versions/${versionId}/recalculate-score/`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({ optimizer_run_id: runId }),
      })
      const data = await response.json().catch(() => null)
      if (!response.ok) throw new Error(apiError(data, 'Unable to recalculate score.'))
      await fetchContext(versionId, { optimizerRunId: runId, quiet: true })
      setNotice('Score, workload details, and violations recalculated.')
    } catch (recalculateError) {
      setError(recalculateError instanceof Error ? recalculateError.message : 'Unable to recalculate score.')
    } finally {
      setIsRecalculatingScore(false)
    }
  }

  const saveRunCopy = async () => {
    const source = selectedRunForActions
    if (!source) return
    try {
      setIsSavingCopy(true)
      setError(null)
      setNotice(null)
      const response = await fetch(`${API_BASE}/optimizer-runs/${source.id}/save-copy/`, {
        method: 'POST', credentials: 'include',
      })
      const data = await response.json().catch(() => null)
      if (!response.ok) throw new Error(apiError(data, 'Unable to save a copy of this run.'))
      const copied = data as OptimizerRun
      setSelectedOptimizerRunId(copied.id)
      updateOptimizerRunUrl(copied.id)
      await fetchContext(copied.schedule_version, { optimizerRunId: copied.id, quiet: true })
      setNotice(`Viewing Run ${copied.run_number} — Copy of Run ${source.run_number}.`)
    } catch (copyError) {
      setError(copyError instanceof Error ? copyError.message : 'Unable to save a copy of this run.')
    } finally {
      setIsSavingCopy(false)
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
      const loaded = data as AssignmentContext
      const firstAssignment = loaded.shift_instance.assignments[0]
      setEditingAssignmentId(firstAssignment?.id ?? null)
      setSelectedPhysicianId(firstAssignment?.physician ?? null)
      setLockAssignment(firstAssignment?.is_locked ?? loaded.shift_instance.is_locked_open)
      setLockOpen(loaded.shift_instance.is_locked_open)
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
      await fetchContext(context?.selected_version?.id, { quiet: true, preserveError: true })
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
      await fetchContext(context?.selected_version?.id, { quiet: true, preserveError: true })
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

  const saveAssignment = async () => {
    if (!assignmentContext || !selectedPhysicianId) return
    try {
      setIsAssignmentSaving(true)
      setAssignmentError(null)
      const base = `${API_BASE}/schedule-blocks/${blockId}/build/shift-instances/${assignmentContext.shift_instance.id}/assignments/`
      const response = await fetch(editingAssignmentId ? `${base}${editingAssignmentId}/` : base, {
        method: editingAssignmentId ? 'PATCH' : 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({ physician_id: selectedPhysicianId, assignment_source: 'MANUAL', is_locked: lockAssignment }),
      })
      const data = await response.json().catch(() => null)
      if (!response.ok) throw new Error(apiError(data, 'Unable to save assignment.'))
      applyAssignmentContext(data as AssignmentContext)
      closeAssignments()
      await fetchContext(context?.selected_version?.id, { quiet: true, preserveError: true })
      setNotice('Manual assignment saved.')
    } catch (saveError) {
      setAssignmentError(saveError instanceof Error ? saveError.message : 'Unable to save assignment.')
    } finally {
      setIsAssignmentSaving(false)
    }
  }

  const markOpen = async () => {
    if (!assignmentContext) return
    try {
      setIsAssignmentSaving(true)
      const response = await fetch(`${API_BASE}/schedule-blocks/${blockId}/build/shift-instances/${assignmentContext.shift_instance.id}/assignments/`, {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' }, credentials: 'include',
        body: JSON.stringify({ physician_id: null, is_locked_open: lockOpen }),
      })
      const data = await response.json().catch(() => null)
      if (!response.ok) throw new Error(apiError(data, 'Unable to mark shift open.'))
      closeAssignments()
      await fetchContext(context?.selected_version?.id, { quiet: true, preserveError: true })
      setNotice(lockOpen ? 'Shift marked open and locked.' : 'Shift marked open.')
    } catch (openError) {
      setAssignmentError(openError instanceof Error ? openError.message : 'Unable to mark shift open.')
    } finally { setIsAssignmentSaving(false) }
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
    && (!context.selected_optimizer_run || context.selected_optimizer_run.is_active)
  const canOptimize = canEditAssignments && context.shift_instances.length > 0
  const canClearAssignments = canEditAssignments && context.shift_instances.length > 0
  const isMutatingBuild = isGenerating || isOptimizing || isRecalculatingScore || isSavingCopy || clearingAction !== null || deletingRunId !== null
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
            onChange={(event) => {
              setSelectedOptimizerRunId(null)
              updateOptimizerRunUrl(null)
              void fetchContext(Number(event.target.value), { optimizerRunId: null })
            }}
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

        <div className="optimizer-start-control">
          <label className="facility-field">
            <span>Optimizer Start</span>
            <select value={optimizerStartMode} onChange={(event) => setOptimizerStartMode(event.target.value as 'CURRENT_SCHEDULE' | 'FRESH_FILL')} disabled={isMutatingBuild}>
              <option value="CURRENT_SCHEDULE">Current Viewed Schedule</option>
              <option value="FRESH_FILL">Fresh Fill</option>
            </select>
          </label>
          <small>
            {optimizerStartMode === 'CURRENT_SCHEDULE'
              ? 'Uses the currently displayed assignments as the optimizer starting point. Locked edits are preserved; unlocked edits may change.'
              : 'Starts from a fresh assignment fill. Locked edits are still preserved.'}
          </small>
        </div>

        <button type="button" className="primary-action" onClick={runOptimizer} disabled={!canOptimize || isMutatingBuild}>
          {isOptimizing
            ? 'Running...'
            : optimizerStartMode === 'CURRENT_SCHEDULE'
              ? 'Run Optimizer from Current Schedule'
              : 'Run Optimizer from Fresh Fill'}
        </button>

        <button
          type="button"
          className="secondary"
          onClick={() => void recalculateScore()}
          disabled={!selectedRunForActions || isMutatingBuild}
        >
          {isRecalculatingScore ? 'Recalculating...' : 'Recalculate Score'}
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

      {optimizerRuns.length > 0 && (
        <div className="optimizer-run-selector-panel">
          {selectedRunForActions && completedOptimizerRuns.length > 0 && (
            <div className="optimizer-run-selector-main">
              <label className="facility-field optimizer-run-select">
                <span>Viewing optimizer run</span>
                <select
                  value={selectedRunForActions.id}
                  onChange={(event) => {
                    const nextRun = completedOptimizerRuns.find((run) => run.id === Number(event.target.value))
                    if (nextRun) {
                      void selectOptimizerRun(nextRun)
                    }
                  }}
                  disabled={isMutatingBuild}
                >
                  {completedOptimizerRuns.map((run) => (
                    <option key={run.id} value={run.id}>
                      {optimizerRunLabel(run)}
                    </option>
                  ))}
                </select>
              </label>
              <div className="optimizer-run-status">
                <span>Viewing Run {selectedRunForActions.run_number}{selectedRunForActions.copied_from_run_number ? ` — Copy of Run ${selectedRunForActions.copied_from_run_number}` : ''}</span>
                {selectedRunForActions.is_active ? (
                  <strong>Active</strong>
                ) : (
                  <span>Active Run {activeOptimizerRun?.run_number ?? '-'}</span>
                )}
              </div>
              <div className="optimizer-run-actions">
                <button type="button" className="secondary" onClick={() => void saveRunCopy()} disabled={isMutatingBuild}>
                  {isSavingCopy ? 'Saving...' : 'Save Copy'}
                </button>
                <button
                  type="button"
                  className="secondary"
                  onClick={() => void selectOptimizerRun(selectedRunForActions)}
                  disabled={isMutatingBuild || selectedOptimizerRunId === selectedRunForActions.id}
                >
                  View
                </button>
                <button
                  type="button"
                  className="secondary"
                  onClick={() => void activateOptimizerRun(selectedRunForActions.id)}
                  disabled={isMutatingBuild || selectedRunForActions.is_active}
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
          )}
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
                  className={`optimizer-run-row${selectedOptimizerRunId === run.id ? ' optimizer-run-row-selected' : ''}`}
                  key={run.id}
                >
                  <div>
                    <strong>Run {run.run_number}</strong>
                    <span>{optimizerRunStatusLabel(run)}</span>
                    <span>{run.start_mode === 'CURRENT_SCHEDULE' ? 'Current schedule' : 'Fresh fill'}</span>
                    <span>{optimizerRunScoreLabel(run)}</span>
                    <span>{formatTimestamp(run.created_at)}</span>
                    <span>Seed {run.seed ?? '-'}</span>
                    <span>{run.is_active ? 'Active' : 'Inactive'}</span>
                  </div>
                  <div className="optimizer-run-row-actions">
                    <button
                      type="button"
                      className="secondary"
                      onClick={() => void selectOptimizerRun(run)}
                      disabled={isMutatingBuild || !isCompletedOptimizerRun(run) || selectedOptimizerRunId === run.id}
                    >
                      View
                    </button>
                    <button
                      type="button"
                      className="secondary"
                      onClick={() => void activateOptimizerRun(run.id)}
                      disabled={isMutatingBuild || !isCompletedOptimizerRun(run) || run.is_active}
                    >
                      Activate
                    </button>
                    {isCompletedOptimizerRun(run) ? (
                      <a
                        className="secondary build-workspace-link-button"
                        href={`/schedule-versions/${run.schedule_version}/violations?optimizer_run_id=${run.id}`}
                      >
                        Violations
                      </a>
                    ) : (
                      <button type="button" className="secondary" disabled>
                        Violations
                      </button>
                    )}
                  {!run.is_active && (
                    <button
                      type="button"
                      className="secondary danger-action"
                      onClick={() => void deleteOptimizerRun(run)}
                      disabled={isMutatingBuild || run.status === 'RUNNING'}
                    >
                      {deletingRunId === run.id ? 'Deleting...' : 'Delete'}
                    </button>
                  )}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {optimizerSummary && (
        <section className="optimizer-summary-card" aria-live="polite">
          {selectedRunForActions?.score_is_stale && (
            <div className="optimizer-score-stale" role="status">
              Score may be outdated after manual edits. Recalculate Score to refresh.
            </div>
          )}
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
            <div className="optimizer-workload-grid">
              {[...optimizerSummary.workload_summary]
                .sort((left, right) => left.physician_name.localeCompare(right.physician_name))
                .map((item) => {
                  const nightCount = item.night_shifts ?? 0
                  return (
                    <div className="optimizer-workload-item" key={item.physician_id}>
                      <div>
                        <strong>{item.physician_name}</strong>
                        <span>
                          {item.assigned_shifts} shifts, {item.assigned_hours.toFixed(1)}h, {nightCount} {nightCount === 1 ? 'night' : 'nights'}
                        </span>
                      </div>
                      <small>
                        {item.contract_name ?? 'No contract'} · {workloadRangeLabel(item.effective_workload_range)}
                        {item.score_contribution !== undefined ? ` · score ${item.score_contribution.toFixed(1)}` : ''}
                      </small>
                    </div>
                  )
                })}
            </div>
          ) : null}

          {showOptimizerDebug && optimizerSummary.debug && (
            <div className="optimizer-debug-panel">
              <div className="optimizer-debug-toolbar">
                <button
                  type="button"
                  className="secondary"
                  onClick={() => void copyOptimizerDebug()}
                >
                  {debugCopyStatus === 'copied'
                    ? 'Copied'
                    : debugCopyStatus === 'failed'
                      ? 'Copy failed'
                      : 'Copy Debug JSON'}
                </button>
                <button
                  type="button"
                  className="secondary"
                  onClick={() => void copyScoreSummary()}
                >
                  {summaryCopyStatus === 'copied'
                    ? 'Copied'
                    : summaryCopyStatus === 'failed'
                      ? 'Copy failed'
                      : 'Copy Score Summary'}
                </button>
              </div>
              <pre className="optimizer-debug-details">
                {debugJsonText()}
              </pre>
            </div>
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
        <div className="assignment-modal-backdrop"><div
          ref={assignmentPopoverRef}
          className="assignment-popover assignment-modal"
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
              <div className="assignment-modal-details">
                <span>Shift</span><strong>{displayedShift.shift_template_name}</strong>
                <span>Facility</span><strong>{displayedShift.facility_short_name}</strong>
                <span>Time</span><strong>{formatTime(displayedShift.template_start_time)}–{formatTime(displayedShift.template_end_time)}</strong>
                <span>Staffing</span><strong>{displayedShift.assigned_count}/{displayedShift.required_staffing}</strong>
                <span>Source</span><strong>{displayedShift.assignments.find((item) => item.id === editingAssignmentId)?.assignment_source ?? 'OPEN'}</strong>
              </div>

              {!canEditAssignments ? (
                <div className="assignment-popover-state">
                  Assignments can only be edited in a BUILD Schedule Version.
                </div>
              ) : (
                <div className="assignment-popover-picker">
                  {displayedShift.assignments.length > 1 && <label>Assignment slot<select value={editingAssignmentId ?? ''} onChange={(event) => {
                    const assignment = displayedShift.assignments.find((item) => item.id === Number(event.target.value))
                    setEditingAssignmentId(assignment?.id ?? null); setSelectedPhysicianId(assignment?.physician ?? null)
                    setLockAssignment(assignment?.is_locked ?? false)
                  }}>{displayedShift.assignments.map((assignment) => <option key={assignment.id} value={assignment.id}>{assignment.physician_name}</option>)}</select></label>}
                  <label>Assigned physician<select value={selectedPhysicianId ?? ''} onChange={(event) => setSelectedPhysicianId(Number(event.target.value) || null)}>
                    <option value="">Select physician</option>
                    {assignmentContext.eligible_physicians.filter((physician) => physician.can_assign || physician.id === selectedPhysicianId).map((physician) => <option key={physician.id} value={physician.id}>{physician.name}</option>)}
                  </select></label>
                  <label className="assignment-lock-option"><input type="checkbox" checked={lockAssignment} onChange={(event) => setLockAssignment(event.target.checked)} />
                    Lock this assignment during optimizer runs
                  </label>
                  <label className="assignment-lock-option"><input type="checkbox" checked={lockOpen} onChange={(event) => setLockOpen(event.target.checked)} />Lock open during optimizer runs</label>
                  <div className="assignment-modal-actions">
                    <button type="button" className="primary-action" disabled={!selectedPhysicianId || isAssignmentSaving} onClick={() => void saveAssignment()}>Save Assignment</button>
                    <button type="button" className="secondary" disabled={isAssignmentSaving} onClick={() => void markOpen()}>Mark Open</button>
                    <button type="button" className="secondary" disabled={isAssignmentSaving} onClick={closeAssignments}>Cancel</button>
                  </div>
                </div>
              )}
            </>
          )}
        </div></div>
      )}
    </div>
  )
}
