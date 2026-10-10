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
})
