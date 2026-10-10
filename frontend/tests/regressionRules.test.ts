import { describe, expect, it } from 'vitest'
import {
  DEFAULT_AUTHENTICATED_PATH,
  assignablePhysicians,
  calendarYearOptions,
  isVisibleInAvailableShifts,
  optimizerStartOptions,
  penaltyContributingRows,
  pendingTradeHighlights,
  shiftHasStarted,
  tradeRequestAttention,
} from '../src/utils/regressionRules'

describe('login destination', () => {
  it('sends a newly authenticated user to the Schedule calendar', () => {
    expect(DEFAULT_AUTHENTICATED_PATH).toBe('/')
  })
})

describe('optimizer schedule source selection', () => {
  it('keeps previous runs and fresh fill available before first publication', () => {
    const options = optimizerStartOptions({
      hasCurrentLiveSchedule: false,
      hasOriginalPublishedSnapshot: false,
    })

    expect(options.map((option) => option.value)).toEqual([
      'PREVIOUS_OPTIMIZER_RUN',
      'ORIGINAL_PUBLISHED_SCHEDULE',
      'FRESH_FILL',
    ])
    expect(options.find((option) => option.value === 'ORIGINAL_PUBLISHED_SCHEDULE')?.disabled).toBe(true)
  })

  it('offers live and original published schedules only when they actually exist', () => {
    const options = optimizerStartOptions({
      hasCurrentLiveSchedule: true,
      hasOriginalPublishedSnapshot: true,
    })

    expect(options.map((option) => option.value)).toEqual([
      'PREVIOUS_OPTIMIZER_RUN',
      'CURRENT_SCHEDULE',
      'ORIGINAL_PUBLISHED_SCHEDULE',
      'FRESH_FILL',
    ])
    expect(options.find((option) => option.value === 'ORIGINAL_PUBLISHED_SCHEDULE')?.disabled).toBe(false)
  })
})

describe('eligible-user filtering', () => {
  it('shows only users the server says can be assigned and who are not already assigned', () => {
    const physicians = [
      { id: 1, name: 'Goodman', can_assign: true, already_assigned: false },
      { id: 2, name: 'View Only APP', can_assign: false, already_assigned: false },
      { id: 3, name: 'Already Assigned', can_assign: true, already_assigned: true },
      { id: 4, name: 'Not Clinically Active', can_assign: false, already_assigned: false },
    ]

    expect(assignablePhysicians(physicians).map((physician) => physician.name)).toEqual(['Goodman'])
  })
})

describe('zero-penalty rows', () => {
  it('hides rows that do not contribute to the official penalty total', () => {
    const rows = [
      { rule_name: 'Maximum nights', score_component: 'night_score', total_penalty: 300 },
      { rule_name: 'Total nights assigned', score_component: 'night_score', total_penalty: 0 },
      { rule_name: 'Coverage', score_component: 'coverage_score', total_penalty: 500 },
      { rule_name: 'Overlap', score_component: 'overlap_score', total_penalty: 200 },
    ]

    expect(penaltyContributingRows(rows).map((row) => row.rule_name)).toEqual(['Maximum nights'])
  })
})

describe('calendar trade highlights', () => {
  const trades = [
    {
      status: 'PENDING_SCHEDULER', requester_id: 10, recipient_id: 20,
      offered_assignment: { id: 100 }, requested_assignment: { id: 200 },
    },
    {
      status: 'PENDING_RECIPIENT', requester_id: 30, recipient_id: 10,
      offered_assignment: { id: 300 }, requested_assignment: { id: 400 },
    },
    {
      status: 'APPROVED', requester_id: 10, recipient_id: 40,
      offered_assignment: { id: 500 }, requested_assignment: { id: 600 },
    },
  ]

  it('separates sent and received pending trades and excludes resolved trades', () => {
    const highlights = pendingTradeHighlights(trades, 10)
    expect([...highlights.sentAssignmentIds]).toEqual([100, 200])
    expect([...highlights.receivedAssignmentIds]).toEqual([300, 400])
    expect(highlights.sentAssignmentIds.has(500)).toBe(false)
  })
})

describe('trade request attention', () => {
  it('counts a sent pending trade without highlighting it as unseen', () => {
    expect(tradeRequestAttention([{
      status: 'PENDING_RECIPIENT',
      requester_id: 10,
      recipient_id: 20,
      can_accept: false,
      can_cancel: true,
      can_review: false,
      is_unseen: false,
    }], 10)).toEqual({ pendingCount: 1, hasUnseenIncoming: false })
  })

  it('highlights a new incoming request until it has been viewed', () => {
    expect(tradeRequestAttention([{
      status: 'PENDING_RECIPIENT',
      requester_id: 10,
      recipient_id: 20,
      can_accept: true,
      can_cancel: false,
      can_review: false,
      is_unseen: true,
    }], 20)).toEqual({ pendingCount: 1, hasUnseenIncoming: true })
  })

  it('keeps an accepted trade in the recipient count while scheduler review is pending', () => {
    expect(tradeRequestAttention([{
      status: 'PENDING_SCHEDULER',
      requester_id: 10,
      recipient_id: 20,
      can_accept: false,
      can_cancel: false,
      can_review: false,
      is_unseen: false,
    }], 20).pendingCount).toBe(1)
  })
})

describe('calendar month navigation', () => {
  it('includes current, viewed, and published schedule years in the year menu', () => {
    const years = calendarYearOptions(['2022-01-01', '2038-12-01'], 2026, 2040)
    expect(years[0]).toBe(2021)
    expect(years).toContain(2026)
    expect(years).toContain(2038)
    expect(years.at(-1)).toBe(2040)
  })
})

describe('shift start-time cutoff', () => {
  const noon = new Date('2026-10-10T12:00:00-04:00').getTime()

  it('treats shifts starting at or before the current instant as started', () => {
    expect(shiftHasStarted('2026-10-10T11:59:59-04:00', noon)).toBe(true)
    expect(shiftHasStarted('2026-10-10T12:00:00-04:00', noon)).toBe(true)
  })

  it('keeps later shifts on the same date eligible', () => {
    expect(shiftHasStarted('2026-10-10T12:00:01-04:00', noon)).toBe(false)
    expect(shiftHasStarted('2026-10-10T19:00:00-04:00', noon)).toBe(false)
  })
})

describe('available shifts filter', () => {
  const noon = new Date('2026-10-10T12:00:00-04:00').getTime()

  it('always includes the user\'s own schedule', () => {
    expect(isVisibleInAvailableShifts({
      physician: 10,
      start_datetime: '2026-10-09T07:00:00-04:00',
      posting_mode: null,
      status: 'scheduled',
    }, 10, noon)).toBe(true)
  })

  it('includes future posted and open shifts but excludes ordinary shifts and past availability', () => {
    const future = '2026-10-10T13:00:00-04:00'
    expect(isVisibleInAvailableShifts({ physician: 20, start_datetime: future, posting_mode: 'PICKUP', status: 'scheduled' }, 10, noon)).toBe(true)
    expect(isVisibleInAvailableShifts({ physician: null, start_datetime: future, posting_mode: null, status: 'open' }, 10, noon)).toBe(true)
    expect(isVisibleInAvailableShifts({ physician: 20, start_datetime: future, posting_mode: null, status: 'scheduled' }, 10, noon)).toBe(false)
    expect(isVisibleInAvailableShifts({ physician: null, start_datetime: '2026-10-10T12:00:00-04:00', posting_mode: null, status: 'open' }, 10, noon)).toBe(false)
  })
})
