export const DEFAULT_AUTHENTICATED_PATH = '/'

export type OptimizerStartSelection =
  | 'PREVIOUS_OPTIMIZER_RUN'
  | 'CURRENT_SCHEDULE'
  | 'ORIGINAL_PUBLISHED_SCHEDULE'
  | 'FRESH_FILL'

export type OptimizerStartOption = {
  value: OptimizerStartSelection
  label: string
  disabled?: boolean
}

export function optimizerStartOptions({
  hasCurrentLiveSchedule,
  hasOriginalPublishedSnapshot,
}: {
  hasCurrentLiveSchedule: boolean
  hasOriginalPublishedSnapshot: boolean
}): OptimizerStartOption[] {
  return [
    { value: 'PREVIOUS_OPTIMIZER_RUN', label: 'Previous Optimizer Run' },
    ...(hasCurrentLiveSchedule
      ? [{ value: 'CURRENT_SCHEDULE' as const, label: 'Current Live Schedule' }]
      : []),
    {
      value: 'ORIGINAL_PUBLISHED_SCHEDULE',
      label: 'Original Published Schedule',
      disabled: !hasOriginalPublishedSnapshot,
    },
    { value: 'FRESH_FILL', label: 'Fresh Fill' },
  ]
}

export function assignablePhysicians<T extends { can_assign: boolean; already_assigned: boolean }>(
  physicians: T[],
) {
  return physicians.filter((physician) => physician.can_assign && !physician.already_assigned)
}

export function penaltyContributingRows<
  T extends { total_penalty: number; score_component: string },
>(rows: T[]) {
  return rows.filter((row) => (
    row.total_penalty > 0
    && row.score_component !== 'coverage_score'
    && row.score_component !== 'overlap_score'
  ))
}

type PendingTrade = {
  status: string
  requester_id: number
  recipient_id: number | null
  offered_assignment: { id: number } | null
  requested_assignment: { id: number } | null
}

export function pendingTradeHighlights(trades: PendingTrade[], physicianId: number | null) {
  const sentAssignmentIds = new Set<number>()
  const receivedAssignmentIds = new Set<number>()
  if (physicianId === null) return { sentAssignmentIds, receivedAssignmentIds }

  trades
    .filter((trade) => ['PENDING_RECIPIENT', 'PENDING_SCHEDULER'].includes(trade.status))
    .forEach((trade) => {
      const destination = trade.requester_id === physicianId
        ? sentAssignmentIds
        : trade.recipient_id === physicianId
          ? receivedAssignmentIds
          : null
      if (!destination) return
      if (trade.offered_assignment) destination.add(trade.offered_assignment.id)
      if (trade.requested_assignment) destination.add(trade.requested_assignment.id)
    })

  return { sentAssignmentIds, receivedAssignmentIds }
}

type TradeRequestAttention = {
  status: string
  requester_id: number
  recipient_id: number | null
  can_accept: boolean
  can_cancel: boolean
  can_review: boolean
  is_unseen: boolean
}

export function tradeRequestAttention(
  trades: TradeRequestAttention[],
  physicianId: number | null,
) {
  return {
    pendingCount: trades.filter((trade) => (
      ['PENDING_RECIPIENT', 'PENDING_SCHEDULER'].includes(trade.status)
      && (
        trade.requester_id === physicianId
        || trade.recipient_id === physicianId
        || trade.can_review
      )
    )).length,
    hasUnseenIncoming: trades.some((trade) => trade.is_unseen),
  }
}

export function calendarYearOptions(
  shiftDates: string[],
  todayYear: number,
  viewYear: number,
) {
  const scheduleYears = shiftDates
    .map((date) => Number(date.slice(0, 4)))
    .filter(Number.isInteger)
  const firstYear = Math.min(todayYear - 5, viewYear, ...scheduleYears)
  const lastYear = Math.max(todayYear + 10, viewYear, ...scheduleYears)
  return Array.from({ length: lastYear - firstYear + 1 }, (_, index) => firstYear + index)
}

export function shiftHasStarted(startDateTime: string, nowMs = Date.now()) {
  const startMs = new Date(startDateTime).getTime()
  return Number.isFinite(startMs) && startMs <= nowMs
}
