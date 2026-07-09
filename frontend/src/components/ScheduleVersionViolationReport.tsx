import React, { useEffect, useState } from 'react'
import { useLocation } from 'react-router-dom'

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
  violation_type: string
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
  physician_id: number
  physician: string
  assigned_shifts: number
  assigned_hours: number
  night_shifts: number
  target_units: string | null
  target_shifts: number | null
  target_hours: number | null
  expected_target: number | null
  allowed_min: number | null
  allowed_max: number | null
  allowed_units: string | null
  deviation: number
  deviation_direction: string
  penalty_weight: number
  shift_deviation: number | null
  hour_deviation: number | null
  score_contribution: number
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
  } | null
  total_score: number
  score_breakdown: Record<string, number>
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

function workloadRangeLabel(row: WorkloadScoreRow) {
  if (row.allowed_min !== null || row.allowed_max !== null) {
    const lower = row.allowed_min === null ? 'No min' : formatNumber(row.allowed_min)
    const upper = row.allowed_max === null ? 'No max' : formatNumber(row.allowed_max)
    return `Allowed range: ${lower}-${upper} ${workloadUnitsLabel(row.allowed_units)}`
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
                Run {report.optimizer_run.run_number} · {formatTimestamp(report.optimizer_run.created_at)} · Final {report.optimizer_run.final_score?.toFixed(1) ?? '-'}
              </>
            )}
          </div>
        </div>
        <a className="secondary build-workspace-link-button" href={`/schedule-blocks/${report.schedule_block.id}/build${report.optimizer_run ? `?optimizer_run_id=${report.optimizer_run.id}` : ''}`}>
          Back to Build Schedule
        </a>
      </div>

      {report.warnings.length > 0 && (
        <div className="violation-report-warning">
          {report.warnings.map((warning) => (
            <p key={warning}>{warning}</p>
          ))}
        </div>
      )}

      <div className="optimizer-summary-panel">
        <div>
          <span>Total score</span>
          <strong>{report.total_score.toFixed(1)}</strong>
        </div>
        <div>
          <span>Workload score</span>
          <strong>{(report.score_breakdown.workload_score ?? 0).toFixed(1)}</strong>
        </div>
        <div>
          <span>Night score</span>
          <strong>{(report.score_breakdown.night_score ?? 0).toFixed(1)}</strong>
        </div>
        <div>
          <span>Request score</span>
          <strong>{(report.score_breakdown.request_score ?? 0).toFixed(1)}</strong>
        </div>
        <div>
          <span>Coverage score</span>
          <strong>{(report.score_breakdown.coverage_score ?? 0).toFixed(1)}</strong>
        </div>
        <div>
          <span>Rest score</span>
          <strong>{(report.score_breakdown.rest_score ?? 0).toFixed(1)}</strong>
        </div>
        <div>
          <span>Overlap score</span>
          <strong>{(report.score_breakdown.overlap_score ?? 0).toFixed(1)}</strong>
        </div>
        <div>
          <span>Same shift score</span>
          <strong>{(report.score_breakdown.same_shift_score ?? 0).toFixed(1)}</strong>
        </div>
      </div>

      <div className="violation-user-list">
        {report.users.map((user) => (
          <section className="violation-user-section" key={user.user_id}>
            <div className="violation-user-heading">
              <h3>{user.display_name}</h3>
              <div className="violation-user-metrics">
                <span>Score: {user.total_score.toFixed(1)}</span>
                <span>{user.shifts} shifts</span>
                <span>{user.hours.toFixed(1)}h</span>
                <span>{user.night_shifts} night</span>
              </div>
            </div>

            {user.violations.length === 0 ? (
              <p className="violation-empty">No violations</p>
            ) : (
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
                        <td>{prettyType(violation.violation_type)}</td>
                        <td>{violation.contract_name ?? '-'}</td>
                        <td>{violation.dates_involved.map(formatDate).join(', ') || '-'}</td>
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
                <div className="violation-user-metrics">
                  <span>Assigned: {user.workload_score.assigned_shifts} shifts, {user.workload_score.assigned_hours.toFixed(1)} hours</span>
                  <span>{workloadRangeLabel(user.workload_score)}</span>
                  <span>Deviation: {workloadDeviationLabel(user.workload_score)}</span>
                  <span>Penalty weight: {user.workload_score.penalty_weight.toFixed(1)}</span>
                  <span>Score contribution: {user.workload_score.score_contribution.toFixed(1)}</span>
                </div>
              </div>
            )}
          </section>
        ))}
      </div>
    </div>
  )
}
