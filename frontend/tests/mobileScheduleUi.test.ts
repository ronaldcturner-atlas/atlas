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
    expect(mobileSource).toContain('selectedShift.assignmentId == null && selectedShift.canPickUp === false')
    expect(mobileSource).toContain("selectedShift.tone === 'open' && selectedShift.canPickUp === false")
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

  it('splits an owned shift through the same published-assignment endpoint as desktop', () => {
    expect(mobileSource).toContain('schedule-assignments/${selectedShift.assignmentId}/split/')
    expect(mobileSource).toContain('split_time: splitTime')
    expect(mobileSource).toContain("data.detail || 'Shift split successfully.'")
    expect(mobileSource).not.toContain('Preview complete — this would split the shift.</button>')
  })

  it('supports scheduler split and backend-directed unsplit workflows', () => {
    expect(mobileSource).toContain("onClick={() => showWorkflow('split')}")
    expect(mobileSource).toContain('schedule-assignments/${selectedShift.assignmentId}/unsplit/')
    expect(mobileSource).toContain('data.requires_physician_selection')
    expect(mobileSource).toContain('data.requires_confirmation')
    expect(mobileSource).toContain('body.physician_id = unsplitUserId')
    expect(mobileSource).toContain('body.force = true')
  })

  it('changes the scheduled user through the desktop reassignment endpoint', () => {
    expect(mobileSource).toContain("showWorkflow('reassign')")
    expect(mobileSource).toContain('schedule-assignments/${selectedShift.assignmentId}/reassign/')
    expect(mobileSource).toContain('physician_id: reassignUserId')
    expect(mobileSource).toContain('setReassignRequiresForce(true)')
    expect(mobileSource).toContain("data.detail || 'Scheduled user changed.'")
  })

  it('adjusts actual times through the desktop shift-instance endpoint', () => {
    expect(mobileSource).toContain("showWorkflow('times')")
    expect(mobileSource).toContain('shift-instances/${selectedShift.instanceId}/times/')
    expect(mobileSource).toContain("method: 'PATCH'")
    expect(mobileSource).toContain('start_time: actualStartTime, end_time: actualEndTime')
    expect(mobileSource).toContain('setTimesRequireForce(true)')
    expect(mobileSource).toContain("data.detail || 'Actual shift times updated.'")
  })

  it('swaps scheduled users through the desktop trade-options and swap endpoints', () => {
    expect(mobileSource).toContain("showWorkflow('swap')")
    expect(mobileSource).toContain('schedule-assignments/${selectedShift.assignmentId}/trade-options/')
    expect(mobileSource).toContain('schedule-assignments/${selectedShift.assignmentId}/swap/')
    expect(mobileSource).toContain('target_assignment_id: swapTargetId')
    expect(mobileSource).toContain('setSwapRequiresForce(true)')
    expect(mobileSource).toContain("data.detail || 'Scheduled users swapped.'")
  })

  it('opens an assigned shift through the desktop published-assignment endpoint', () => {
    expect(mobileSource).toContain('schedule-assignments/${selectedShift.assignmentId}/open/')
    expect(mobileSource).toContain('onClick={openScheduledShift}')
    expect(mobileSource).toContain("data.detail || 'Shift opened.'")
  })

  it('assigns an open shift through the desktop shift-instance endpoint', () => {
    expect(mobileSource).toContain("showWorkflow('assign')")
    expect(mobileSource).toContain('shift-instances/${selectedShift.instanceId}/assign/')
    expect(mobileSource).toContain('physician_id: assignUserId')
    expect(mobileSource).toContain('setAssignRequiresForce(true)')
    expect(mobileSource).toContain("data.detail || 'Open shift assigned.'")
  })

  it('manages trade requests using backend action flags and endpoints', () => {
    expect(mobileSource).toContain("fetch(`${API_BASE}/shift-trades/`, { credentials: 'include', cache: 'no-store' })")
    expect(mobileSource).toContain('shift-trades/mark-seen/')
    expect(mobileSource).toContain('shift-trades/${tradeId}/${action}/')
    expect(mobileSource).toContain('trade.can_accept')
    expect(mobileSource).toContain('trade.can_cancel')
    expect(mobileSource).toContain('trade.can_review')
    expect(mobileSource).toContain('setTradeActionConfirmation({ tradeId, action })')
    expect(mobileStyles).toContain('.mobile-trade-card')
  })

  it('signs out through the shared authentication context without clearing view preferences', () => {
    expect(mobileSource).toContain('const { user, logout } = useAuth()')
    expect(mobileSource).toContain('await logout()')
    expect(mobileSource).toContain('setApiShifts([])')
    expect(mobileSource).toContain('setTradeRequests([])')
    expect(mobileSource).not.toContain("localStorage.removeItem('atlas.mobile-preview")
  })

  it('shows only monthly personal statistics in the mobile Stats view', () => {
    expect(mobileSource).toContain('<strong>My Stats</strong>')
    expect(mobileSource).toContain('aria-label="Stats month"')
    expect(mobileSource).toContain('aria-label="Stats year"')
    expect(mobileSource).toContain('shift.physician === user?.physician_id')
    expect(mobileSource).toContain('myStatsTotalHours')
    expect(mobileSource).toContain('myStatsNightHours')
    expect(mobileSource).not.toContain('Account settings')
    expect(mobileSource).not.toContain('Group Stats')
    expect(mobileStyles).toContain('.mobile-stats-page')
  })

  it('loads authoritative trade-only offers and submits the selected exchange', () => {
    expect(mobileSource).toContain('schedule-assignments/${candidate.id}/trade-options/')
    expect(mobileSource).toContain('option.id === selectedShift.assignmentId')
    expect(mobileSource).toContain('offered_assignment_id: tradeOfferAssignmentId')
    expect(mobileSource).toContain('target_assignment_id: selectedShift.assignmentId')
    expect(mobileSource).toContain('note: tradeOfferNote')
    expect(mobileSource).toContain('Trade request sent successfully.')
  })

  it('loads and submits authoritative trade proposals from an owned shift', () => {
    expect(mobileSource).toContain('schedule-assignments/${selectedShift.assignmentId}/trade-options/')
    expect(mobileSource).toContain('offered_assignment_id: selectedShift.assignmentId')
    expect(mobileSource).toContain('target_assignment_id: tradeTarget')
    expect(mobileSource).toContain('note: tradeNote')
    expect(mobileSource).toContain('Trade proposal sent successfully.')
    expect(mobileSource).toContain('selectedPartnerTradeOptions')
  })

  it('keeps selected-day priority highlights scoped to the month calendar', () => {
    expect(mobileSource).toContain('prioritizeCalendarShifts')
    expect(mobileStyles).toContain('.mobile-month-preview .mobile-day-schedule>button.calendar-own-highlight')
    expect(mobileStyles).toContain('.mobile-month-preview .mobile-day-schedule>button.calendar-available-highlight')
  })
})
