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
    expect(sidebarSource).toContain('Available pickup')
    expect(calendarStyles).toMatch(/\.sidebar-shift-legend\{[^}]*border-top:/)
  })

  it('keeps the shared sidebar fixed while the page content scrolls', () => {
    expect(calendarStyles).toMatch(/\.sidebar\{[^}]*position:fixed;[^}]*height:100vh;/)
    expect(calendarStyles).toMatch(/\.main-area\{[^}]*margin-left:250px;/)
    expect(calendarStyles).toMatch(/\.sidebar\.sidebar-collapsed \+ \.main-area\{[^}]*margin-left:76px/)
  })
})
