import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const calendarSource = readFileSync(
  new URL('../src/components/Calendar.tsx', import.meta.url),
  'utf8',
)
const calendarStyles = readFileSync(
  new URL('../src/index.css', import.meta.url),
  'utf8',
)
const sidebarSource = readFileSync(
  new URL('../src/components/Sidebar.tsx', import.meta.url),
  'utf8',
)
const dashboardSource = readFileSync(
  new URL('../src/components/Dashboard.tsx', import.meta.url),
  'utf8',
)
const loginStyles = readFileSync(
  new URL('../src/components/Login.css', import.meta.url),
  'utf8',
)

describe('desktop calendar controls', () => {
  it('uses the compact current-month action without the obsolete Today and Month buttons', () => {
    expect(calendarSource).toContain('This Month')
    expect(calendarSource).toContain('aria-label="Calendar month"')
    expect(calendarSource).toContain('aria-label="Calendar year"')
    expect(calendarSource).not.toMatch(/<button[^>]*>Today<\/button>/)
    expect(calendarSource).not.toMatch(/<button[^>]*className="primary"[^>]*>Month<\/button>/)
  })

  it('reloads schedule ownership without browser caching after mutations', () => {
    expect(calendarSource).toContain("fetch(`${API_BASE}/published-schedule/`, { credentials: 'include', cache: 'no-store' })")
    expect(calendarSource).toContain('setAllShifts(await scheduleResponse.json())')
  })

  it('uses explicit pickup confirmation for posted and eligible open shifts', () => {
    expect(calendarSource).toContain('You will be responsible for this shift.')
    expect(calendarSource).toContain("setPickupConfirmation('POSTED')")
    expect(calendarSource).toContain("setPickupConfirmation('OPEN')")
    expect(calendarSource).toContain('>Pickup Shift</button>')
    expect(calendarSource).toContain('shift-instances/${selectedShift.instanceId}/pickup/')
    expect(calendarSource).toContain('>Send trade request</button>')
  })

  it('retains an optional posting comment and shows it in shift details', () => {
    expect(calendarSource).toContain('Comment (optional)<textarea')
    expect(calendarSource).toContain('comment: postingComment')
    expect(calendarSource).toContain('Posting comment')
  })

  it('uses the Requests Open gold border only for unseen incoming trades', () => {
    expect(calendarSource).toContain('trade-center-button-unseen')
    expect(calendarSource).toContain('shift-trades/mark-seen/')
    expect(calendarStyles).toMatch(/\.trade-center-button-unseen\{[^}]*border-color:#f6c344;/)
  })

  it('offers three highlighted calendar-view buttons and a view-name banner', () => {
    expect(calendarSource).toContain('Mine &amp; Available Shifts')
    expect(calendarSource).toContain('Group Schedule')
    expect(calendarSource).toContain('schedule-view-button')
    expect(calendarSource).toContain('scheduleViewLabel')
    expect(calendarSource).not.toContain('Return to group schedule')
    expect(calendarSource).toContain('isVisibleInAvailableShifts')
    expect(calendarStyles).toMatch(/\.schedule-view-button:hover,\.schedule-view-button\.selected/)
  })

  it('keeps the Schedule controls and weekday labels visible while scrolling', () => {
    expect(calendarSource).toContain('calendar-sticky-controls')
    expect(calendarStyles).toMatch(/\.calendar-sticky-controls\{[^}]*position:sticky;[^}]*top:52px;/)
    expect(calendarStyles).toMatch(/\.schedule-page-heading\{[^}]*position:sticky;[^}]*top:0;/)
  })

  it('keeps calendar popups above the sticky desktop controls and within the viewport', () => {
    expect(calendarStyles).toMatch(/\.shift-modal-overlay\{[^}]*padding:16px;[^}]*z-index:100;/)
    expect(calendarStyles).toMatch(/\.shift-trade-modal\{[^}]*max-height:calc\(100vh - 32px\);[^}]*overflow:hidden/)
    expect(calendarStyles).toMatch(/\.trade-request-list\{[^}]*min-height:0;[^}]*overflow:auto/)
  })

  it('separates complete shift and user names without changing the shift name', () => {
    expect(calendarSource).toContain('<span className="calendar-shift-name">{shift.facility} {shift.shift}</span>')
    expect(calendarSource).toContain('<span className="calendar-shift-user">{shift.physician_name}</span>')
    expect(calendarStyles).toMatch(/\.calendar-shift-label\{[^}]*flex-wrap:wrap;[^}]*column-gap:1\.5em;/)
    expect(calendarStyles).toMatch(/\.calendar-shift-name,[^{]*\.calendar-shift-user\{[^}]*white-space:nowrap;[^}]*text-overflow:clip/)
  })

  it('uses the configured facility color only for each desktop shift left edge', () => {
    expect(calendarSource).toContain("fetch(`${API_BASE}/facilities/?active=true`")
    expect(calendarSource).toContain("facilityColor: facilityColorById.get(apiShift.facility) || '#94a3b8'")
    expect(calendarSource).toContain('style={{ borderLeftColor: shift.facilityColor }}')
  })

  it('uses effective schedule-management permissions instead of legacy groups', () => {
    expect(calendarSource).toContain('Boolean(user?.can_manage_schedules)')
    expect(calendarSource).not.toContain("groups.some((group) => ['admin', 'scheduler']")
  })

  it('keeps personal trade options separate from permission-gated scheduler swap options', () => {
    expect(calendarSource).toContain("hasSelectedShiftPermission('propose_trade')")
    expect(calendarSource).toContain("hasSelectedShiftPermission('manage_any_shift_posting')")
    expect(calendarSource).toContain("hasSelectedShiftPermission('split_any_published_shift')")
    expect(calendarSource).toContain('trade-options/?purpose=scheduler_swap')
    expect(calendarSource).toContain('{adminSwapOptions.map((option) => (')
  })
})

describe('calendar highlight styling', () => {
  it.each([
    'shift-status-own',
    'shift-status-posted-other',
    'shift-status-own-posted',
    'shift-status-trade-sent',
    'shift-status-trade-received',
    'shift-status-open',
  ])('keeps the %s background on hover', (className) => {
    expect(calendarStyles).toMatch(new RegExp(`\\.${className}\\{[^}]*--shift-hover:`))
  })

  it('shows a passive legend in the sidebar only on the Schedule page', () => {
    expect(calendarSource).not.toContain('schedule-status-legend')
    expect(sidebarSource).toContain("activeView === 'my-schedule'")
    expect(sidebarSource).toContain('sidebar-shift-legend')
    expect(sidebarSource).toContain('My assigned shift')
    expect(sidebarSource).toContain('Available for pickup')
    expect(sidebarSource).toContain('My posted shift')
    expect(sidebarSource).toContain('Trade sent')
    expect(sidebarSource).toContain('Trade received')
    expect(sidebarSource).toContain('Open shift')
    expect(calendarStyles).toContain('.sidebar-legend-own{background:#1f65a6}')
    expect(calendarStyles).toContain('.sidebar-legend-available{background:#34d399}')
    expect(calendarStyles).toContain('.sidebar-legend-posted{background:#fbbf24}')
    expect(calendarStyles).toContain('.sidebar-legend-trade-sent{background:#c084fc}')
    expect(calendarStyles).toContain('.sidebar-legend-trade-received{background:#f472b6}')
    expect(calendarStyles).toContain('.sidebar-legend-open{background:#f87171}')
    expect(calendarStyles).toMatch(/\.sidebar-shift-legend\{[^}]*border-top:/)
  })

  it('keeps an assigned shift blue before considering posting or pending-trade states', () => {
    const resolver = calendarSource.slice(
      calendarSource.indexOf('const statusClassForShift'),
      calendarSource.indexOf('const tradePartners'),
    )
    expect(resolver.indexOf("if (isOwn) return 'shift-status-own'")).toBeGreaterThan(-1)
    expect(resolver.indexOf("if (isOwn) return 'shift-status-own'")).toBeLessThan(resolver.indexOf('sentAssignmentIds.has'))
    expect(resolver.indexOf("if (isOwn) return 'shift-status-own'")).toBeLessThan(resolver.indexOf('shift.postingMode'))
  })

  it('uses the approved light visual language throughout the desktop application', () => {
    expect(dashboardSource).toContain('<div className="app-root schedule-light-theme">')
    expect(calendarStyles).toMatch(/\.schedule-light-theme\{[^}]*--app-bg:#f1f5f9;[^}]*--surface:#fff;[^}]*--muted:#64748b;/)
    expect(calendarStyles).toMatch(/\.schedule-light-theme \.calendar-card\{[^}]*background:#fff;/)
    expect(calendarStyles).toContain('.schedule-light-theme .day-cell{border:1px solid #dbe2ea;background:#fff}')
    expect(calendarStyles).toMatch(/\.schedule-light-theme \.stats-page-card,[\s\S]*\.schedule-light-theme \.contract-page,[\s\S]*\.schedule-light-theme \.user-directory-card\{[^}]*background:#fff;/)
    expect(calendarStyles).toContain('.schedule-light-theme .contract-page-tabs button.active{border-bottom-color:#4f46e5;color:#3730a3}')
    expect(calendarStyles).toContain('.schedule-light-theme .user-name-button{color:#334155}')
    expect(calendarStyles).toContain('.schedule-light-theme .feasibility-compact-summary{border-color:#dbe2ea;background:#fff}')
    expect(calendarStyles).toContain('.schedule-light-theme .build-workspace-maintenance-actions .secondary:not(.danger-action):disabled{')
    expect(calendarStyles).toContain('.schedule-light-theme .danger-action:disabled,')
    expect(calendarStyles).toContain('.schedule-light-theme .build-violations-link:disabled{')
    expect(loginStyles).toContain('background: linear-gradient(135deg, #eef2ff 0%, #f8fafc 55%, #e2e8f0 100%);')
    expect(loginStyles).toContain('background: #fff;')
  })

  it('keeps the shared sidebar fixed while the page content scrolls', () => {
    expect(calendarStyles).toMatch(/\.sidebar\{[^}]*position:fixed;[^}]*height:100vh;/)
    expect(calendarStyles).toMatch(/\.main-area\{[^}]*margin-left:250px;/)
    expect(calendarStyles).toMatch(/\.sidebar\.sidebar-collapsed \+ \.main-area\{[^}]*margin-left:76px/)
  })
})
