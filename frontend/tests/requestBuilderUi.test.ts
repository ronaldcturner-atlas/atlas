import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const requestBuilderSource = readFileSync(
  new URL('../src/components/RequestBuilderView.tsx', import.meta.url),
  'utf8',
)

describe('Schedule Block request overview', () => {
  it('shows separate User and Admin priority and weekend totals', () => {
    expect(requestBuilderSource).toContain('Schedule Block Request Totals')
    expect(requestBuilderSource).toContain("(['USER', 'ADMIN'] as RequestScope[])")
    expect(requestBuilderSource).toContain("['FIXED', 'HIGH', 'MEDIUM', 'LOW', 'WEEKEND']")
  })

  it('orders User requests before Admin requests within each calendar day', () => {
    expect(requestBuilderSource).toContain("left.request_scope === 'USER' ? 0 : 1")
    expect(requestBuilderSource).toContain("right.request_scope === 'USER' ? 0 : 1")
  })

  it('only offers Fixed priority for permitted Admin requests', () => {
    expect(requestBuilderSource).toContain("option.value !== 'FIXED' || (selectedScope === 'ADMIN' && canManageFixedRequests)")
    expect(requestBuilderSource).toContain("option.value !== 'FIXED' || (bulkScope === 'ADMIN' && canManageFixedRequests)")
    expect(requestBuilderSource).toContain('Fixed (permission required)')
  })

  it('does not expose Admin requests while using the User request view', () => {
    expect(requestBuilderSource).toContain("data.requests.filter((item) => item.request_scope === 'USER')")
    expect(requestBuilderSource).toContain("data.visible_requests.filter((item) => item.request_scope === 'USER')")
  })

  it('lets the individual request workflow select and save multiple dates', () => {
    expect(requestBuilderSource).toContain('individualSelectedDates.includes(dateKey)')
    expect(requestBuilderSource).toContain("dates: targetDates")
    expect(requestBuilderSource).toContain("'bulk/' : 'upsert/'")
    expect(requestBuilderSource).toContain('Select additional calendar dates before saving.')
    expect(requestBuilderSource).toContain('Save ${individualSelectedDates.length} Requests')
  })

  it('keeps the post-save automatic date advance for a multi-date save', () => {
    expect(requestBuilderSource).toContain('const nextDate = nextIndividualRequestDate(savedDate, block.end_date)')
    expect(requestBuilderSource).toContain('setIndividualSelectedDates([nextDate])')
  })

  it('projects request and weekend allowances while dates are selected', () => {
    expect(requestBuilderSource).toContain('const projectedRequestCounters = useMemo')
    expect(requestBuilderSource).toContain("`${counter.used}/${counter.limit ?? '—'}${showingProjection ? ' projected' : ''}`")
    expect(requestBuilderSource).toContain('projected.WEEKEND.used += 1')
    expect(requestBuilderSource).toContain('request-counter-card-over')
    expect(requestBuilderSource).toContain('Remove ${counter.used - (counter.limit ?? 0)} selected date')
  })
})
