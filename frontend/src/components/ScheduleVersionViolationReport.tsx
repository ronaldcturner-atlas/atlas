import React, { useEffect, useState } from 'react'
import { Link, useLocation } from 'react-router-dom'

type ScheduleVersion = {
  id: number
  schedule_block: number
  domain_name: string
  version_number: number
  name: string
  status: string
}

type ScheduleBlock = {
  id: number
  name: string
  start_date: string
  end_date: string
}

type AssignmentDetail = {
  shift_instance_id: number
  date: string
  facility: string
  shift_template: string
  start_datetime?: string
  end_datetime?: string
  night_shift?: boolean
}

type ViolationRow = {
  period_start?: string | null
  period_end?: string | null
  violation_type: string
  score_component: string
  shift_rule_label?: string | null
  dates_involved: string[]
  night_block_dates?: string[]
  prior_night_block_dates?: string[]
  next_night_block_dates?: string[]
  night_block_assignments?: AssignmentDetail[]
  prior_night_block_assignments?: AssignmentDetail[]
  next_night_block_assignments?: AssignmentDetail[]
  assignment_details?: AssignmentDetail[]
  shift_instance_ids: number[]
  facility: string | null
  shift_template: string | null
  previous_assignment?: AssignmentDetail | null
  next_assignment?: AssignmentDetail | null
  contract_id?: number | null
  contract_name?: string | null
  configured_limit: number | string | null
  actual_value: number | string | null
  penalty_weight: number | null
  penalty_amount: number
  explanation: string
}

type WorkloadScoreRow = {
  rule_rows?: Array<{
    period_type: string
    period_start: string
    period_end: string
    units: string
    assigned_value: number
    effective_min_value: number | null
    effective_max_value: number | null
    score_contribution: number
  }>
  physician_id: number
  physician: string
  assigned_shifts: number
  assigned_hours: number
  night_shifts: number
  target_units: string | null
  target_shifts: number | null
  target_hours: number | null
  expected_target: number | null
  contract_name?: string | null
  period_type?: string | null
  raw_allowed_min?: number | null
  raw_allowed_max?: number | null
  allowed_min: number | null
  allowed_max: number | null
  allowed_units: string | null
  deviation: number
  deviation_direction: string
  penalty_weight: number
  shift_deviation: number | null
  hour_deviation: number | null
  score_contribution: number
  workload_hour_adjustment?: {
    contract_rules: Array<{
      period_type: string
      units: string
      minimum: number | null
      maximum: number | null
    }>
    original_block_minimum_hours: number | null
    original_block_maximum_hours: number | null
    effective_block_minimum_hours: number | null
    effective_block_maximum_hours: number | null
    minimum_adjustment_hours: number | null
    maximum_adjustment_hours: number | null
  } | null
}

type ViolationUser = {
  user_id: number
  display_name: string
  total_score: number
  shifts: number
  hours: number
  night_shifts: number
  violations: ViolationRow[]
  workload_score?: WorkloadScoreRow | null
}

type ViolationReport = {
  schedule_version: ScheduleVersion
  schedule_block: ScheduleBlock
  optimizer_run: {
    id: number
    run_number: number
    created_at: string
    final_score: number | null
    score_is_stale: boolean
  } | null
  total_score: number
  score_breakdown: Record<string, number>
  proportionality?: {
    total: number
    facility: number
    time_of_day: number
    is_penalty: false
  }
  rule_summary: Array<{
    score_component: string
    area: string
    contract_name: string | null
    rule_name: string
    scope: string | null
    configured: string | null
    violation_count: number
    total_penalty: number
  }>
  warnings: string[]
  debug?: {
    violations_recomputed_from_final_assignments?: boolean
    stale_violation_rows_dropped?: number
    violation_assignment_validation_errors?: Array<Record<string, unknown>>
    night_block_assignment_ids_by_physician?: Record<string, number[][]>
  }
  users: ViolationUser[]
}

type Props = {
  versionId: number
}

type PenaltyFilter =
  | 'total_score'
  | 'workload_score'
  | 'night_score'
  | 'request_score'
  | 'rest_score'
  | 'same_shift_score'
  | 'shift_rule_score'
  | 'weekend_score'
  | 'consecutive_days_score'
  | 'underutilization_score'
  | 'invalid_assignment_score'

const PENALTY_FILTERS: Array<{ key: PenaltyFilter; label: string }> = [
  { key: 'total_score', label: 'Total score' },
  { key: 'workload_score', label: 'Workload score' },
  { key: 'night_score', label: 'Night score' },
  { key: 'request_score', label: 'Request score' },
  { key: 'rest_score', label: 'Rest score' },
  { key: 'same_shift_score', label: 'Same shift score' },
  { key: 'shift_rule_score', label: 'Shift rule score' },
  { key: 'weekend_score', label: 'Weekend score' },
  { key: 'consecutive_days_score', label: 'Consecutive days score' },
  { key: 'underutilization_score', label: 'Underutilization score' },
  { key: 'invalid_assignment_score', label: 'Invalid assignment score' },
]

const API_BASE = 'http://localhost:8000/api'

function formatDate(value: string) {
  const [year, month, day] = value.split('-').map(Number)
  return new Date(Date.UTC(year, month - 1, day)).toLocaleDateString('en-US', {
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

function formatValue(value: number | string | null) {
  if (value === null || value === undefined || value === '') {
    return '-'
  }
  return String(value)
}

function prettyType(value: string) {
  return value
    .split('_')
    .map((part) => part.charAt(0) + part.slice(1).toLowerCase())
    .join(' ')
}

function violationLabel(violation: ViolationRow) {
  const typeLabel = prettyType(violation.violation_type)
  if (
    violation.violation_type.startsWith('SHIFT_GROUP_')
    && violation.shift_rule_label
  ) {
    return `${typeLabel} (${violation.shift_rule_label})`
  }
  return typeLabel
}

function assignmentLabel(detail: AssignmentDetail) {
  const kind = detail.night_shift ? 'Night' : 'Non-night'
  return `${kind}: ${formatDate(detail.date)} ${detail.shift_template} / ${detail.facility}`
}

function shiftFacilityLabel(violation: ViolationRow) {
  const details = violation.assignment_details?.length
    ? violation.assignment_details
    : [
      violation.previous_assignment,
      ...(violation.night_block_assignments ?? []),
      ...(violation.prior_night_block_assignments ?? []),
      ...(violation.next_night_block_assignments ?? []),
      violation.next_assignment,
    ].filter(Boolean) as AssignmentDetail[]

  if (details.length) {
    return (
      <div className="violation-assignment-details">
        {details.map((detail) => (
          <div key={detail.shift_instance_id}>{assignmentLabel(detail)}</div>
        ))}
      </div>
    )
  }

  return [violation.shift_template, violation.facility].filter(Boolean).join(' / ') || '-'
}

function formatNumber(value: number | null | undefined, digits = 1) {
  if (value === null || value === undefined) {
    return '-'
  }
  return value.toFixed(digits)
}

function workloadUnitsLabel(units: string | null | undefined) {
  return units === 'SHIFTS' ? 'shifts' : 'hours'
}

function workloadRangeLabel(row: WorkloadScoreRow, raw = false) {
  const minimum = raw ? row.raw_allowed_min : row.allowed_min
  const maximum = raw ? row.raw_allowed_max : row.allowed_max
  if (minimum !== null && minimum !== undefined || maximum !== null && maximum !== undefined) {
    const lower = minimum === null || minimum === undefined ? 'No min' : formatNumber(minimum)
    const upper = maximum === null || maximum === undefined ? 'No max' : formatNumber(maximum)
    return `${raw ? 'Raw' : 'Effective'} allowed range: ${lower}–${upper} ${workloadUnitsLabel(row.allowed_units)}`
  }
  return row.target_units === 'SHIFTS'
    ? `Target: ${formatNumber(row.target_shifts)} shifts`
    : `Target: ${formatNumber(row.target_hours)} hours`
}

function workloadDeviationLabel(row: WorkloadScoreRow) {
  if (row.allowed_min !== null || row.allowed_max !== null) {
    if (row.deviation_direction === 'below_minimum') {
      return `${formatNumber(row.deviation)} ${workloadUnitsLabel(row.allowed_units)} below minimum`
    }
    if (row.deviation_direction === 'above_maximum') {
      return `${formatNumber(row.deviation)} ${workloadUnitsLabel(row.allowed_units)} above maximum`
    }
    return '0'
  }
  return row.target_units === 'SHIFTS'
    ? `${formatNumber(row.shift_deviation)} shifts`
    : `${formatNumber(row.hour_deviation)} hours`
}

export default function ScheduleVersionViolationReport({ versionId }: Props) {
  const location = useLocation()
  const optimizerRunId = new URLSearchParams(location.search).get('optimizer_run_id')
  const [report, setReport] = useState<ViolationReport | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const [activeTab, setActiveTab] = useState<'details' | 'summary'>('details')
  const [activePenaltyFilter, setActivePenaltyFilter] = useState<PenaltyFilter>('total_score')

  useEffect(() => {
    let cancelled = false
    async function fetchReport() {
      setIsLoading(true)
      setError(null)
      try {
        const query = optimizerRunId ? `?optimizer_run_id=${optimizerRunId}` : ''
        const response = await fetch(`${API_BASE}/schedule-versions/${versionId}/violation-report/${query}`, {
          credentials: 'include',
        })
        const data = await response.json().catch(() => null)
        if (!response.ok) {
          throw new Error(data?.detail ?? 'Unable to load violation report.')
        }
        if (!cancelled) {
          setReport(data as ViolationReport)
          setActivePenaltyFilter('total_score')
        }
      } catch (fetchError) {
        if (!cancelled) {
          setError(fetchError instanceof Error ? fetchError.message : 'Unable to load violation report.')
        }
      } finally {
        if (!cancelled) {
          setIsLoading(false)
        }
      }
    }
    void fetchReport()
    return () => {
      cancelled = true
    }
  }, [versionId, optimizerRunId])

  if (isLoading) {
    return <div className="build-workspace-empty">Loading violation report...</div>
  }

  if (error || !report) {
    return <div className="facilities-error">{error ?? 'Unable to load violation report.'}</div>
  }

  const filteredUsers = report.users.map((user) => {
    const violations = user.violations.filter((violation) => (
      violation.penalty_amount > 0
      && (
        activePenaltyFilter === 'total_score'
        || violation.score_component === activePenaltyFilter
      )
    ))
    const workloadScore = (
      (activePenaltyFilter === 'total_score' || activePenaltyFilter === 'workload_score')
      && (user.workload_score?.score_contribution ?? 0) > 0
    ) ? user.workload_score ?? null : null
    const workloadRuleRows = (workloadScore?.rule_rows ?? []).filter(
      (row) => row.score_contribution > 0,
    )
    const filteredScore = violations.reduce(
      (sum, violation) => sum + violation.penalty_amount,
      0,
    ) + (workloadScore?.score_contribution ?? 0)
    return {
      ...user,
      violations,
      workload_score: workloadScore ? {
        ...workloadScore,
        rule_rows: workloadRuleRows,
      } : null,
      filtered_score: filteredScore,
    }
  }).filter((user) => user.violations.length > 0 || user.workload_score !== null)

  const activePenaltyLabel = PENALTY_FILTERS.find(
    (filter) => filter.key === activePenaltyFilter,
  )?.label ?? 'Total score'
  const visibleRuleSummary = report.rule_summary.filter((row) => (
    row.score_component !== 'coverage_score'
    && row.score_component !== 'overlap_score'
  ))
  return (
    <div className="violation-report-page">
      <div className="build-workspace-header">
        <div>
          <h2>{report.schedule_block.name}</h2>
          <div className="build-workspace-subtitle">
            {report.schedule_version.name} · {report.schedule_version.domain_name} · {report.schedule_version.status}
            {report.optimizer_run && (
              <>
                {' · '}
                Run {report.optimizer_run.run_number} · {formatTimestamp(report.optimizer_run.created_at)} · {report.optimizer_run.score_is_stale ? 'Stored final' : 'Final'} {report.optimizer_run.final_score?.toFixed(1) ?? '-'}
              </>
            )}
          </div>
        </div>
        <Link className="secondary build-workspace-link-button" to={`/schedule-blocks/${report.schedule_block.id}/build${report.optimizer_run ? `?optimizer_run_id=${report.optimizer_run.id}` : ''}`}>
          Back to Build Schedule
        </Link>
      </div>

      {report.optimizer_run?.score_is_stale && (
        <div className="violation-report-warning">
          <p>
            Contract or schedule rules changed after this run. The stored final score was {report.optimizer_run.final_score?.toFixed(1) ?? '-'}; the current-rules score is {report.total_score.toFixed(1)}. Recalculate the score or run the optimizer again before relying on this result.
          </p>
        </div>
      )}

      {report.warnings.length > 0 && (
        <div className="violation-report-warning">
          {report.warnings.map((warning) => (
            <p key={warning}>{warning}</p>
          ))}
        </div>
      )}

      <div className="violation-report-tabs" role="tablist" aria-label="Violation report views">
        <button
          type="button"
          role="tab"
          aria-selected={activeTab === 'details'}
          className={activeTab === 'details' ? 'active' : ''}
          onClick={() => setActiveTab('details')}
        >
          Details
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={activeTab === 'summary'}
          className={activeTab === 'summary' ? 'active' : ''}
          onClick={() => setActiveTab('summary')}
        >
          Summary
        </button>
      </div>

      {activeTab === 'summary' && (
        <div className="violation-summary-view" role="tabpanel">
          <div className="violation-summary-total">
            <span>Total schedule-block penalty</span>
            <strong>{report.total_score.toFixed(1)}</strong>
            <p>The rule totals below reconcile to the official penalty score for this schedule block.</p>
          </div>

          <div className="proportionality-context-grid">
            <section>
              <h3>Facility proportionality</h3>
              <strong>{(report.proportionality?.facility ?? 0).toFixed(1)}</strong>
              <p><b>Lower is better.</b> This measures how closely each eligible non-nocturnist physician's facility mix follows the available optimizer-controlled shift supply.</p>
            </section>
            <section>
              <h3>Time proportionality</h3>
              <strong>{(report.proportionality?.time_of_day ?? 0).toFixed(1)}</strong>
              <p><b>Lower is better.</b> This measures how closely each eligible non-nocturnist physician's early, midday, and late shift mix follows the available optimizer-controlled shift supply.</p>
            </section>
          </div>
          <p className="proportionality-context-note">
            These proportionality values are not penalties and are not included in the total score. The optimizer uses them only to prefer a more even distribution when the official penalty does not increase. The scheduler decides whether further distribution-focused optimization is worthwhile.
          </p>

          <div className="violation-table-wrap">
            <table className="scheduler-table violation-table violation-summary-table">
              <thead>
                <tr>
                  <th>Rule area</th>
                  <th>Contract</th>
                  <th>Rule</th>
                  <th>Scope</th>
                  <th>Configured</th>
                  <th>Violations</th>
                  <th>Total penalty</th>
                </tr>
              </thead>
              <tbody>
                {visibleRuleSummary.map((row, index) => (
                  <tr key={`${row.score_component}-${row.contract_name}-${row.rule_name}-${index}`}>
                    <td>{row.area}</td>
                    <td>{row.contract_name ?? 'All contracts'}</td>
                    <td>{row.rule_name}</td>
                    <td>{row.scope ? prettyType(row.scope) : '-'}</td>
                    <td>{row.configured ?? '-'}</td>
                    <td>{row.violation_count}</td>
                    <td>{row.total_penalty.toFixed(1)}</td>
                  </tr>
                ))}
              </tbody>
              <tfoot>
                <tr>
                  <th colSpan={6}>Total schedule-block penalty</th>
                  <th>{visibleRuleSummary.reduce((sum, row) => sum + row.total_penalty, 0).toFixed(1)}</th>
                </tr>
              </tfoot>
            </table>
          </div>
        </div>
      )}

      <div hidden={activeTab !== 'details'} role="tabpanel">

      <div className="optimizer-summary-panel violation-score-filters" aria-label="Penalty detail filters">
        {PENALTY_FILTERS.map((filter) => (
          <button
            type="button"
            key={filter.key}
            className={activePenaltyFilter === filter.key ? 'active' : ''}
            aria-pressed={activePenaltyFilter === filter.key}
            onClick={() => setActivePenaltyFilter(filter.key)}
          >
            <span>{filter.label}</span>
            <strong>{(
              filter.key === 'total_score'
                ? report.total_score
                : report.score_breakdown[filter.key] ?? 0
            ).toFixed(1)}</strong>
          </button>
        ))}
      </div>
      <div className="violation-proportionality-scores" aria-label="Distribution measures">
        <div><span>Facility proportionality</span><strong>{(report.proportionality?.facility ?? 0).toFixed(1)}</strong></div>
        <div><span>Time proportionality</span><strong>{(report.proportionality?.time_of_day ?? 0).toFixed(1)}</strong></div>
      </div>
      <p className="proportionality-context-note">
        Lower facility and time proportionality values are better. They are not penalties and are not included in Total score; the optimizer uses them only to prefer a more even distribution when the official penalty does not increase.
      </p>

      <p className="violation-filter-status">
        Showing nonzero penalties contributing to {activePenaltyLabel}.
      </p>

      <div className="violation-user-list">
        {filteredUsers.length === 0 && (
          <p className="violation-empty">No nonzero individual penalties contribute to {activePenaltyLabel}.</p>
        )}
        {filteredUsers.map((user) => (
          <section className="violation-user-section" key={user.user_id}>
            <div className="violation-user-heading">
              <h3>{user.display_name}</h3>
              <div className="violation-user-metrics">
                <span>{activePenaltyFilter === 'total_score' ? 'Score' : 'Selected penalty'}: {user.filtered_score.toFixed(1)}</span>
                <span>{user.shifts} shifts</span>
                <span>{user.hours.toFixed(1)}h</span>
                <span>{user.night_shifts} night</span>
              </div>
            </div>

            {user.violations.length > 0 && (
              <div className="violation-table-wrap">
                <table className="scheduler-table violation-table">
                  <thead>
                    <tr>
                      <th>Type</th>
                      <th>Contract</th>
                      <th>Dates</th>
                      <th>Shift/Facility</th>
                      <th>Configured</th>
                      <th>Actual</th>
                      <th>Weight</th>
                      <th>Penalty</th>
                      <th>Explanation</th>
                    </tr>
                  </thead>
                  <tbody>
                    {user.violations.map((violation, index) => (
                      <tr key={`${violation.violation_type}-${index}`}>
                        <td>{violationLabel(violation)}</td>
                        <td>{violation.contract_name ?? '-'}</td>
                        <td>{violation.period_start && violation.period_end
                          ? `${formatDate(violation.period_start)} – ${formatDate(violation.period_end)}`
                          : violation.dates_involved.map(formatDate).join(', ') || '-'}</td>
                        <td>{shiftFacilityLabel(violation)}</td>
                        <td>{formatValue(violation.configured_limit)}</td>
                        <td>{formatValue(violation.actual_value)}</td>
                        <td>{violation.penalty_weight === null ? '-' : violation.penalty_weight.toFixed(1)}</td>
                        <td>{violation.penalty_amount.toFixed(1)}</td>
                        <td>{violation.explanation}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            {user.workload_score && (
              <div className="violation-workload-score">
                <h4>Workload balancing score</h4>
                {user.workload_score.rule_rows?.length ? (
                  <div className="violation-table-wrap">
                    <table className="scheduler-table violation-table">
                      <thead><tr><th>Period</th><th>Dates</th><th>Assigned in period</th><th>Allowed range</th><th>Penalty</th></tr></thead>
                      <tbody>{user.workload_score.rule_rows.map((rule, index) => (
                        <tr key={`${rule.period_type}-${rule.period_start}-${index}`}>
                          <td>{prettyType(rule.period_type)}</td>
                          <td>{formatDate(rule.period_start)} – {formatDate(rule.period_end)}</td>
                          <td>{rule.assigned_value.toFixed(1)} {rule.units.toLowerCase()}</td>
                          <td>{rule.effective_min_value ?? 'No minimum'} – {rule.effective_max_value ?? 'No maximum'} {rule.units.toLowerCase()}</td>
                          <td>{rule.score_contribution.toFixed(1)}</td>
                        </tr>
                      ))}</tbody>
                    </table>
                  </div>
                ) : (
                <div className="violation-user-metrics">
                  <span>Contract: {user.workload_score.contract_name ?? 'No contract'}</span>
                  <span>Period: {user.workload_score.period_type ?? 'Not configured'}</span>
                  <span>Assigned: {user.workload_score.assigned_shifts} shifts, {user.workload_score.assigned_hours.toFixed(1)} hours</span>
                  <span>{workloadRangeLabel(user.workload_score, true)}</span>
                  <span>{workloadRangeLabel(user.workload_score)}</span>
                  <span>Deviation: {workloadDeviationLabel(user.workload_score)}</span>
                  <span>Penalty weight: {user.workload_score.penalty_weight.toFixed(1)}</span>
                  <span>Score contribution: {user.workload_score.score_contribution.toFixed(1)}</span>
                </div>
                )}
              </div>
            )}
          </section>
        ))}
      </div>
      </div>
    </div>
  )
}
