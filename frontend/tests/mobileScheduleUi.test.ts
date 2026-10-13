import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const mobileSource = readFileSync(
  new URL('../src/components/MobileNavigationPreview.tsx', import.meta.url),
  'utf8',
)
const mobileStyles = readFileSync(
  new URL('../src/components/MobileNavigationPreview.css', import.meta.url),
  'utf8',
)

describe('mobile published schedule actions', () => {
  it('uses the authoritative pickup workflows for open and posted shifts', () => {
    expect(mobileSource).toContain('shift-instances/${selectedShift.instanceId}/pickup/')
    expect(mobileSource).toContain("`${API_BASE}/shift-trades/`")
    expect(mobileSource).toContain('target_assignment_id: selectedShift.assignmentId')
    expect(mobileSource).toContain('selectedShift.canPickUp === false')
  })

  it('supports explicit conflict confirmation and reloads published ownership', () => {
    expect(mobileSource).toContain('data.requires_confirmation')
    expect(mobileSource).toContain('pickupRequiresForce')
    expect(mobileSource).toContain("fetch(`${API_BASE}/published-schedule/`, { credentials: 'include', cache: 'no-store' })")
    expect(mobileSource).toContain('Shift picked up and added to My schedule.')
  })

  it('creates, updates, and removes an owned shift posting through the shared endpoint', () => {
    expect(mobileSource).toContain('schedule-assignments/${selectedShift.assignmentId}/posting/')
    expect(mobileSource).toContain("savePosting(postingMode === 'pickup' ? 'PICKUP' : 'TRADE_ONLY')")
    expect(mobileSource).toContain("savePosting('CLOSE')")
    expect(mobileSource).toContain('comment: postComment')
    expect(mobileSource).toContain('shiftHasStarted(selectedShift.startDateTime)')
  })

  it('keeps selected-day priority highlights scoped to the month calendar', () => {
    expect(mobileSource).toContain('prioritizeCalendarShifts')
    expect(mobileStyles).toContain('.mobile-month-preview .mobile-day-schedule>button.calendar-own-highlight')
    expect(mobileStyles).toContain('.mobile-month-preview .mobile-day-schedule>button.calendar-available-highlight')
  })
})
