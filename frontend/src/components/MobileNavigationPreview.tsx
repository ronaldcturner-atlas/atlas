import React from 'react'
import { useAuth } from '../contexts/AuthContext'
import './MobileNavigationPreview.css'

type PreviewPage = 'schedule' | 'stats' | 'users' | 'more'
type ScheduleDisplay = 'list' | 'month'
type ListScheduleScope = 'group' | 'mine'
type ShiftTone = 'standard' | 'own' | 'available' | 'posted' | 'trade-sent' | 'trade-received' | 'open'
type ShiftWorkflow = 'post' | 'trade' | 'split' | 'pickup' | 'offer'

const primaryItems: Array<{ id: PreviewPage; label: string; icon: string }> = [
  { id: 'schedule', label: 'Schedule', icon: '▦' },
  { id: 'stats', label: 'Stats', icon: '▥' },
  { id: 'users', label: 'Users', icon: '●' },
  { id: 'more', label: 'More', icon: '•••' },
]

const sampleDays = Array.from({ length: 35 }, (_, index) => {
  const day = index - 3
  return day > 0 && day <= 31 ? day : null
})

type PreviewShift = { facility: string; time: string; physician: string; domain: 'Physician' | 'APP'; tone?: ShiftTone; isMine?: boolean; postingMode?: 'pickup' | 'trade' }
type SelectedPreviewShift = PreviewShift & { date: Date }

const sampleShiftsByDay: Record<number, PreviewShift[]> = {
  11: [
    { facility: 'Berkeley', time: '8a–5p', physician: 'Santos', domain: 'APP' },
  ],
  12: [
    { facility: 'Roper', time: '6a–3p', physician: 'Mullin', domain: 'Physician', tone: 'own', isMine: true },
    { facility: 'Berkeley', time: '7a–4p', physician: 'Moe', domain: 'Physician' },
    { facility: 'St Francis', time: '2p–11p', physician: 'Feingold', domain: 'APP' },
  ],
  13: [
    { facility: 'Roper', time: '6a–3p', physician: 'Taylor', domain: 'Physician', tone: 'available', postingMode: 'pickup' },
    { facility: 'Roper', time: '3p–12a', physician: 'Santos', domain: 'APP' },
  ],
  14: [
    { facility: 'Berkeley', time: '7a–4p', physician: 'Moe', domain: 'Physician' },
    { facility: 'St Francis', time: '9a–6p', physician: 'Turner', domain: 'APP', tone: 'posted', isMine: true },
    { facility: 'Roper', time: '3p–12a', physician: 'Mullin', domain: 'Physician' },
  ],
  15: [
    { facility: 'St Francis', time: '6a–3p', physician: 'Feingold', domain: 'APP' },
    { facility: 'Berkeley', time: '4p–1a', physician: 'Taylor', domain: 'Physician', tone: 'trade-sent', isMine: true },
  ],
  16: [
    { facility: 'Roper', time: '7a–4p', physician: 'Open shift', domain: 'Physician', tone: 'open' },
    { facility: 'St Francis', time: '3p–12a', physician: 'Moe', domain: 'Physician' },
  ],
  17: [
    { facility: 'Berkeley', time: '7a–4p', physician: 'Feingold', domain: 'APP', tone: 'available', postingMode: 'trade' },
  ],
}

const fallbackShifts: PreviewShift[] = [
  { facility: 'Roper', time: '6a–3p', physician: 'Mullin', domain: 'Physician' },
  { facility: 'Berkeley', time: '7a–4p', physician: 'Moe', domain: 'Physician' },
  { facility: 'St Francis', time: '2p–11p', physician: 'Feingold', domain: 'APP' },
]

function dateKey(value: Date) {
  return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, '0')}-${String(value.getDate()).padStart(2, '0')}`
}

function PreviewShiftList({ date, shifts = fallbackShifts, label = 'Selected date', onSelect, currentUserName }: { date: Date; shifts?: PreviewShift[]; label?: string; onSelect?: (shift: SelectedPreviewShift) => void; currentUserName?: string }) {
  return (
    <section className="mobile-day-schedule" aria-label={`Schedule for ${date.toLocaleDateString('en-US')}`}>
      <header><span>{label}</span><strong>{date.toLocaleDateString('en-US', { weekday: 'long', month: 'long', day: 'numeric' })}</strong></header>
      {shifts.length === 0 && <div className="mobile-no-shifts">No shifts scheduled</div>}
      {shifts.map(({ facility, time, physician, domain, tone = 'standard', isMine, postingMode }) => {
        const displayedPhysician = isMine && currentUserName ? currentUserName : physician
        const mobileTone = isMine ? 'own' : tone === 'standard' ? 'standard' : 'other-status'
        const statusLabel = postingMode === 'pickup' ? 'Pickup' : postingMode === 'trade' ? 'Trade' : tone === 'open' ? 'Open' : ''
        return <button type="button" className={`shift-tone-${mobileTone}`} key={`${facility}-${time}`} onClick={() => onSelect?.({ facility, time, physician: displayedPhysician, domain, tone, isMine, postingMode, date })}><i /><span><strong>{facility}</strong><small>{time}</small></span><b>{displayedPhysician}<small>{domain === 'APP' ? 'APP' : 'MD'}{statusLabel ? ` · ${statusLabel}` : ''}</small></b><em>›</em></button>
      })}
    </section>
  )
}

export default function MobileNavigationPreview() {
  const { user } = useAuth()
  const [activePage, setActivePage] = React.useState<PreviewPage>(() => (
    (window.localStorage.getItem('atlas.mobile-preview.page') as PreviewPage) || 'schedule'
  ))
  const [display, setDisplay] = React.useState<ScheduleDisplay>(() => (
    (window.localStorage.getItem('atlas.mobile-preview.display') as ScheduleDisplay) || 'month'
  ))
  const [listScheduleScope, setListScheduleScope] = React.useState<ListScheduleScope>(() => (
    (window.localStorage.getItem('atlas.mobile-preview.list-scope') as ListScheduleScope) || 'group'
  ))
  const [showMore, setShowMore] = React.useState(false)
  const [showScope, setShowScope] = React.useState(false)
  const [selectedShift, setSelectedShift] = React.useState<SelectedPreviewShift | null>(null)
  const [shiftWorkflow, setShiftWorkflow] = React.useState<ShiftWorkflow | null>(null)
  const [postingMode, setPostingMode] = React.useState<'pickup' | 'trade'>('pickup')
  const [postComment, setPostComment] = React.useState('')
  const [tradePartner, setTradePartner] = React.useState('')
  const [tradeTarget, setTradeTarget] = React.useState('')
  const [tradeNote, setTradeNote] = React.useState('')
  const [splitTime, setSplitTime] = React.useState('10:30')
  const [workflowMessage, setWorkflowMessage] = React.useState('')
  const [testUserView, setTestUserView] = React.useState(false)
  const [selectedDay, setSelectedDay] = React.useState(() => {
    const stored = Number(window.localStorage.getItem('atlas.mobile-preview.day'))
    return Number.isInteger(stored) && stored >= 1 && stored <= 31 ? stored : new Date().getDate()
  })
  const [activeListDate, setActiveListDate] = React.useState(() => dateKey(new Date(new Date().getFullYear(), new Date().getMonth(), selectedDay)))
  const [weekIndex, setWeekIndex] = React.useState(() => {
    const monthStart = new Date(new Date().getFullYear(), new Date().getMonth(), 1)
    const monthGridStart = new Date(monthStart.getFullYear(), monthStart.getMonth(), 1 - monthStart.getDay())
    const gridStart = new Date(monthGridStart.getFullYear(), monthGridStart.getMonth(), monthGridStart.getDate() - 28)
    const selectedDate = new Date(new Date().getFullYear(), new Date().getMonth(), selectedDay)
    return Math.max(0, Math.min(15, Math.floor((selectedDate.getTime() - gridStart.getTime()) / 604800000)))
  })
  const contentRef = React.useRef<HTMLElement>(null)
  const programmaticScrollRef = React.useRef(false)
  const pendingWeekJumpRef = React.useRef(false)
  const [selectedRegion, setSelectedRegion] = React.useState(() => (
    window.localStorage.getItem('atlas.mobile-preview.region') || 'Lowcountry Emergency Physicians'
  ))
  const [selectedDomains, setSelectedDomains] = React.useState<string[]>(() => {
    try {
      const value = JSON.parse(window.localStorage.getItem('atlas.mobile-preview.domains') || '[]')
      return Array.isArray(value) && value.length ? value : ['Physician', 'APP']
    } catch {
      return ['Physician', 'APP']
    }
  })

  React.useEffect(() => window.localStorage.setItem('atlas.mobile-preview.page', activePage), [activePage])
  React.useEffect(() => window.localStorage.setItem('atlas.mobile-preview.display', display), [display])
  React.useEffect(() => window.localStorage.setItem('atlas.mobile-preview.list-scope', listScheduleScope), [listScheduleScope])
  React.useEffect(() => window.localStorage.setItem('atlas.mobile-preview.region', selectedRegion), [selectedRegion])
  React.useEffect(() => window.localStorage.setItem('atlas.mobile-preview.domains', JSON.stringify(selectedDomains)), [selectedDomains])
  React.useEffect(() => window.localStorage.setItem('atlas.mobile-preview.day', String(selectedDay)), [selectedDay])

  const selectPrimary = (page: PreviewPage) => {
    if (page === 'more') {
      setShowMore(true)
      setShowScope(false)
      return
    }
    setActivePage(page)
    setShowMore(false)
    setShowScope(false)
  }

  const toggleDomain = (domain: string) => {
    setSelectedDomains((current) => current.includes(domain)
      ? current.length === 1 ? current : current.filter((value) => value !== domain)
      : [...current, domain])
  }

  const currentMonth = new Date().toLocaleDateString('en-US', { month: 'long', year: 'numeric' })
  const currentUserName = [user?.first_name, user?.last_name].filter(Boolean).join(' ') || 'My account'
  const monthStart = new Date(new Date().getFullYear(), new Date().getMonth(), 1)
  const monthGridStart = new Date(monthStart.getFullYear(), monthStart.getMonth(), 1 - monthStart.getDay())
  const listStart = new Date(monthGridStart.getFullYear(), monthGridStart.getMonth(), monthGridStart.getDate() - 28)
  const listDates = Array.from({ length: 112 }, (_, index) => new Date(listStart.getFullYear(), listStart.getMonth(), listStart.getDate() + index))
  const weekDays = listDates.slice(weekIndex * 7, (weekIndex * 7) + 7)
  const listMonthLabel = (weekDays[3] || monthStart).toLocaleDateString('en-US', { month: 'long', year: 'numeric' })
  const shiftsForDate = (date: Date) => date.getMonth() === new Date().getMonth() ? sampleShiftsByDay[date.getDate()] || [] : []
  const datesWithMyShifts = listDates.filter((date) => shiftsForDate(date).some((shift) => shift.isMine))
  const visibleListDates = listScheduleScope === 'mine' ? datesWithMyShifts : listDates
  const todayStart = new Date(new Date().getFullYear(), new Date().getMonth(), new Date().getDate())
  const nextOwnShiftDate = datesWithMyShifts.find((date) => date.getTime() >= todayStart.getTime()) || datesWithMyShifts[datesWithMyShifts.length - 1]
  const markerStateForDate = (date: Date) => {
    const shifts = shiftsForDate(date)
    return {
      hasOwnShift: shifts.some((shift) => shift.isMine),
      hasOtherStatus: listScheduleScope === 'group' && shifts.some((shift) => !shift.isMine && shift.tone && shift.tone !== 'standard'),
    }
  }
  const jumpToListDate = (date: Date) => {
    const key = dateKey(date)
    programmaticScrollRef.current = true
    const content = contentRef.current
    const target = document.getElementById(`mobile-list-day-${key}`)
    if (content && target) content.scrollTo({ top: Math.max(0, target.offsetTop - 128), behavior: 'auto' })
    setActiveListDate(key)
    const nextWeekIndex = Math.max(0, Math.min(15, Math.floor((date.getTime() - listStart.getTime()) / 604800000)))
    setWeekIndex(nextWeekIndex)
  }
  const moveListWeek = (direction: -1 | 1) => {
    pendingWeekJumpRef.current = true
    setWeekIndex((current) => Math.max(0, Math.min(15, current + direction)))
  }

  React.useEffect(() => {
    if (display !== 'list' || !pendingWeekJumpRef.current) return
    pendingWeekJumpRef.current = false
    const date = listDates[weekIndex * 7]
    const key = dateKey(date)
    programmaticScrollRef.current = true
    setActiveListDate(key)
    window.requestAnimationFrame(() => {
      const content = contentRef.current
      const target = document.getElementById(`mobile-list-day-${key}`)
      if (content && target) content.scrollTo({ top: Math.max(0, target.offsetTop - 128), behavior: 'auto' })
    })
  }, [weekIndex])

  React.useEffect(() => {
    if (display !== 'list') return
    const content = contentRef.current
    if (!content) return
    const syncWeekToScroll = () => {
      if (programmaticScrollRef.current) return
      const threshold = content.getBoundingClientRect().top + 132
      const anchors = Array.from(content.querySelectorAll<HTMLElement>('.mobile-list-day-anchor'))
      let active = anchors[0]
      for (const anchor of anchors) {
        if (anchor.getBoundingClientRect().top <= threshold) active = anchor
        else break
      }
      const key = active?.dataset.date
      if (!key) return
      setActiveListDate((current) => current === key ? current : key)
      const date = new Date(`${key}T12:00:00`)
      const nextWeekIndex = Math.max(0, Math.min(15, Math.floor((date.getTime() - listStart.getTime()) / 604800000)))
      setWeekIndex((current) => current === nextWeekIndex ? current : nextWeekIndex)
    }
    const resumeManualTracking = () => { programmaticScrollRef.current = false }
    content.addEventListener('scroll', syncWeekToScroll, { passive: true })
    content.addEventListener('wheel', resumeManualTracking, { passive: true })
    content.addEventListener('touchstart', resumeManualTracking, { passive: true })
    window.requestAnimationFrame(() => {
      const target = document.getElementById(`mobile-list-day-${activeListDate}`)
      if (target) {
        programmaticScrollRef.current = true
        content.scrollTo({ top: Math.max(0, target.offsetTop - 128), behavior: 'auto' })
      }
    })
    return () => {
      content.removeEventListener('scroll', syncWeekToScroll)
      content.removeEventListener('wheel', resumeManualTracking)
      content.removeEventListener('touchstart', resumeManualTracking)
    }
  }, [display])

  React.useLayoutEffect(() => {
    if (display !== 'list') return
    programmaticScrollRef.current = true
    const content = contentRef.current
    const targetDate = listScheduleScope === 'mine' && nextOwnShiftDate ? nextOwnShiftDate : new Date(`${activeListDate}T12:00:00`)
    const targetKey = dateKey(targetDate)
    if (targetKey !== activeListDate) setActiveListDate(targetKey)
    if (listScheduleScope === 'mine') {
      const nextWeekIndex = Math.max(0, Math.min(15, Math.floor((targetDate.getTime() - listStart.getTime()) / 604800000)))
      setWeekIndex((current) => current === nextWeekIndex ? current : nextWeekIndex)
    }
    const target = document.getElementById(`mobile-list-day-${targetKey}`)
    if (content && target) content.scrollTop = Math.max(0, target.offsetTop - 128)
  }, [display, listScheduleScope])
  const openShift = (shift: SelectedPreviewShift) => {
    setSelectedShift(shift)
    setShiftWorkflow(null)
    setWorkflowMessage('')
    setTradeTarget('')
  }
  const closeShift = () => {
    setSelectedShift(null)
    setShiftWorkflow(null)
    setWorkflowMessage('')
  }
  const showWorkflow = (workflow: ShiftWorkflow) => {
    setShiftWorkflow(workflow)
    setWorkflowMessage('')
  }

  return (
    <div className="mobile-preview-workbench">
      <header className="mobile-preview-heading">
        <div><strong>Atlas mobile preview</strong><span>Local prototype · nothing has been published</span></div>
        <a href="/">Return to desktop Atlas</a>
      </header>

      <div className="mobile-preview-phone" aria-label="Atlas mobile schedule preview">
        <main ref={contentRef} className="mobile-preview-content">
          {activePage === 'schedule' ? (
            <>
              <header className="mobile-schedule-toolbar">
                <div className="mobile-display-switcher" role="group" aria-label="Schedule display">
                  <button type="button" className={display === 'list' ? 'active' : ''} onClick={() => setDisplay('list')}>List</button>
                  <button type="button" className={display === 'month' ? 'active' : ''} onClick={() => setDisplay('month')}>Month</button>
                </div>
                <div className="mobile-schedule-view-switcher" role="group" aria-label="Schedule selection">
                  <button type="button" className={listScheduleScope === 'group' ? 'active' : ''} onClick={() => setListScheduleScope('group')}>Group</button>
                  <button type="button" className={listScheduleScope === 'mine' ? 'active' : ''} onClick={() => setListScheduleScope('mine')}>My schedule</button>
                </div>
                <div className="mobile-month-heading">
                  <button type="button" aria-label="Previous month">‹</button><strong>{display === 'list' ? listMonthLabel : currentMonth}</strong><button type="button" aria-label="Next month">›</button>
                </div>
              </header>

              {display === 'month' ? (
                <section className="mobile-month-preview" aria-label={`${currentMonth} calendar preview`}>
                  <div className="mobile-shift-legend" aria-label="Shift colors">
                    <span><i className="own" />My shifts</span>{listScheduleScope === 'group' && <span><i className="other-status" />Available or open</span>}
                  </div>
                  <div className="mobile-weekdays">{['S','M','T','W','T','F','S'].map((day, index) => <span key={`${day}-${index}`}>{day}</span>)}</div>
                  <div className="mobile-month-grid">
                    {sampleDays.map((day, index) => {
                      const { hasOwnShift, hasOtherStatus } = day ? markerStateForDate(new Date(new Date().getFullYear(), new Date().getMonth(), day)) : { hasOwnShift: false, hasOtherStatus: false }
                      return <button type="button" key={index} className={`${day === new Date().getDate() ? 'today ' : ''}${day === selectedDay ? 'selected' : ''}`} disabled={day == null} onClick={() => day && setSelectedDay(day)}>
                        {day && <><span>{day}</span>{(hasOwnShift || hasOtherStatus) && <i className="mobile-date-shift-dots">{hasOwnShift && <b className="own" aria-label="My shift" />}{hasOtherStatus && <b className="other-status" aria-label="Available or open shift" />}</i>}</>}
                      </button>
                    })}
                  </div>
                  <PreviewShiftList date={new Date(new Date().getFullYear(), new Date().getMonth(), selectedDay)} shifts={(sampleShiftsByDay[selectedDay] || fallbackShifts).filter((shift) => listScheduleScope === 'group' || shift.isMine)} onSelect={openShift} currentUserName={currentUserName} />
                </section>
              ) : (
                <section className="mobile-list-preview" aria-label="List view preview">
                  <div className="mobile-week-navigator" aria-label="Week navigation">
                    <button type="button" className="week-step" aria-label="Previous week" onClick={() => moveListWeek(-1)}>‹</button>
                    {weekDays.map((date) => {
                      const key = dateKey(date)
                      const { hasOwnShift, hasOtherStatus } = markerStateForDate(date)
                      const adjacentMonth = date.getMonth() !== weekDays[3].getMonth()
                      return <button type="button" key={key} data-week-date={key} className={`${key === activeListDate ? 'active ' : ''}${key === dateKey(new Date()) ? 'today ' : ''}${adjacentMonth ? 'adjacent' : ''}`} onClick={() => jumpToListDate(date)} aria-label={`Jump to ${date.toLocaleDateString('en-US', { weekday: 'long', month: 'long', day: 'numeric' })}`}>
                        <span>{date.toLocaleDateString('en-US', { weekday: 'narrow' })}</span><strong>{date.getDate()}</strong>{adjacentMonth && <small>{date.toLocaleDateString('en-US', { month: 'short' })}</small>}<i>{hasOwnShift && <b className="own" />}{hasOtherStatus && <b className="other-status" />}</i>
                      </button>
                    })}
                    <button type="button" className="week-step" aria-label="Next week" onClick={() => moveListWeek(1)}>›</button>
                  </div>
                  {visibleListDates.map((date) => {
                    const key = dateKey(date)
                    const currentMonthDate = date.getMonth() === new Date().getMonth()
                    const allShifts = shiftsForDate(date).length ? shiftsForDate(date) : fallbackShifts.slice(0, date.getDate() % 3 === 0 ? 3 : 2)
                    const shifts = listScheduleScope === 'mine' ? allShifts.filter((shift) => shift.isMine) : allShifts
                    return <div id={`mobile-list-day-${key}`} data-date={key} className={`mobile-list-day-anchor${currentMonthDate ? '' : ' adjacent-month'}`} key={key}><PreviewShiftList date={date} shifts={shifts} label={key === dateKey(new Date()) ? 'Today' : 'Schedule'} onSelect={openShift} currentUserName={currentUserName} /></div>
                  })}
                </section>
              )}
            </>
          ) : (
            <section className="mobile-secondary-preview">
              <div>{primaryItems.find((item) => item.id === activePage)?.icon}</div>
              <strong>{primaryItems.find((item) => item.id === activePage)?.label}</strong>
              <p>This page is retained as a user-facing destination, without mobile administrative controls.</p>
            </section>
          )}
        </main>

        {selectedShift && (
          <div className="mobile-sheet-backdrop" onClick={closeShift}>
            <section className="mobile-sheet mobile-shift-sheet" onClick={(event) => event.stopPropagation()}>
              <div className="mobile-sheet-handle" />
              <header><div><span>{shiftWorkflow ? 'Shift workflow' : 'Published shift'}</span><strong>{shiftWorkflow === 'post' ? 'Post this shift' : shiftWorkflow === 'trade' ? 'Propose a trade' : shiftWorkflow === 'split' ? 'Split shift' : shiftWorkflow === 'pickup' ? 'Pickup shift' : shiftWorkflow === 'offer' ? 'Offer a trade' : selectedShift.facility}</strong></div><button type="button" onClick={closeShift} aria-label="Close shift details">×</button></header>
              {shiftWorkflow && <button type="button" className="mobile-workflow-back" onClick={() => { setShiftWorkflow(null); setWorkflowMessage('') }}>‹ Shift details</button>}
              <div className="mobile-shift-summary">
                <div><span>Date &amp; time</span><strong>{selectedShift.date.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' })} · {selectedShift.time}</strong></div>
                <div><span>Scheduled</span><strong>{selectedShift.physician} · {selectedShift.domain}</strong></div>
                {!selectedShift.isMine && selectedShift.postingMode && <div><span>Availability</span><strong>{selectedShift.postingMode === 'pickup' ? 'Posted for pickup' : 'Trade only'}</strong></div>}
                {!selectedShift.isMine && selectedShift.tone === 'open' && <div><span>Status</span><strong>Open shift</strong></div>}
              </div>
              {shiftWorkflow === 'post' ? (
                <div className="mobile-workflow-form">
                  <p>Choose how other clinicians may respond to this shift.</p>
                  <label><input type="radio" name="posting-mode" checked={postingMode === 'pickup'} onChange={() => setPostingMode('pickup')} /><span><strong>Available for pickup</strong><small>A clinician may request this shift without offering one back.</small></span></label>
                  <label><input type="radio" name="posting-mode" checked={postingMode === 'trade'} onChange={() => setPostingMode('trade')} /><span><strong>Trade only</strong><small>A clinician must offer one of their shifts in return.</small></span></label>
                  <label className="field">Comment (optional)<textarea value={postComment} onChange={(event) => setPostComment(event.target.value)} placeholder="Add a note for other clinicians" /></label>
                  <button type="button" className="mobile-workflow-primary" onClick={() => setWorkflowMessage('Preview complete — this would post the shift.')}>Post shift</button>
                  {workflowMessage && <div className="mobile-workflow-message">{workflowMessage}</div>}
                </div>
              ) : shiftWorkflow === 'trade' ? (
                <div className="mobile-workflow-form">
                  <label className="field">Trade with<select value={tradePartner} onChange={(event) => { setTradePartner(event.target.value); setTradeTarget('') }}><option value="">Choose clinician</option><option value="moe">Moe</option><option value="taylor">Taylor</option></select></label>
                  <label className="field">Shift requested<select value={tradeTarget} disabled={!tradePartner} onChange={(event) => setTradeTarget(event.target.value)}><option value="">Choose shift</option><option value="oct-13">Tue, Oct 13 · Roper · 6a–3p</option><option value="oct-15">Thu, Oct 15 · Berkeley · 4p–1a</option></select></label>
                  <label className="field">Comments (optional)<textarea value={tradeNote} onChange={(event) => setTradeNote(event.target.value)} /></label>
                  <button type="button" className="mobile-workflow-primary" disabled={!tradeTarget} onClick={() => setWorkflowMessage('Preview complete — this would send the trade proposal.')}>Send trade proposal</button>
                  {workflowMessage && <div className="mobile-workflow-message">{workflowMessage}</div>}
                </div>
              ) : shiftWorkflow === 'split' ? (
                <div className="mobile-workflow-form">
                  <label className="field">Split at<input type="time" value={splitTime} onChange={(event) => setSplitTime(event.target.value)} /></label>
                  <div className="mobile-split-preview"><span>Resulting portions</span><strong>6a–{splitTime || '—'} · {splitTime || '—'}–3p</strong></div>
                  <button type="button" className="mobile-workflow-primary" disabled={!splitTime} onClick={() => setWorkflowMessage('Preview complete — this would split the shift.')}>Split shift</button>
                  {workflowMessage && <div className="mobile-workflow-message">{workflowMessage}</div>}
                </div>
              ) : shiftWorkflow === 'pickup' ? (
                <div className="mobile-workflow-form">
                  <div className="mobile-workflow-warning"><strong>You will be responsible for this shift.</strong><span>Confirm only if you are able to work the complete scheduled time shown above.</span></div>
                  <button type="button" className="mobile-workflow-primary" onClick={() => setWorkflowMessage('Preview complete — this shift would now be assigned to you.')}>Confirm pickup</button>
                  {workflowMessage && <div className="mobile-workflow-message">{workflowMessage}</div>}
                </div>
              ) : shiftWorkflow === 'offer' ? (
                <div className="mobile-workflow-form">
                  <p>This shift is available only in exchange for one of your eligible shifts.</p>
                  <label className="field">Your shift offered<select value={tradeTarget} onChange={(event) => setTradeTarget(event.target.value)}><option value="">Choose one of your shifts</option><option value="oct-14">Wed, Oct 14 · St Francis · 9a–6p</option><option value="oct-15">Thu, Oct 15 · Berkeley · 4p–1a</option></select></label>
                  <button type="button" className="mobile-workflow-primary" disabled={!tradeTarget} onClick={() => setWorkflowMessage('Preview complete — this would send the trade request.')}>Send trade request</button>
                  {workflowMessage && <div className="mobile-workflow-message">{workflowMessage}</div>}
                </div>
              ) : user?.can_manage_schedules && !testUserView && selectedShift.tone === 'open' ? (
                <div className="mobile-shift-actions"><strong>Manage open shift</strong><button type="button">Assign user <span>›</span></button></div>
              ) : user?.can_manage_schedules && !testUserView ? (
                <div className="mobile-shift-actions">
                  <strong>Manage this shift</strong>
                  <button type="button">Change scheduled user <span>›</span></button>
                  <button type="button">Adjust actual times <span>›</span></button>
                  <button type="button">Swap scheduled users <span>›</span></button>
                  <button type="button">Split shift <span>›</span></button>
                  <button type="button" className="caution">Open shift <span>›</span></button>
                </div>
              ) : selectedShift.isMine ? (
                <div className="mobile-shift-actions">
                  <strong>Shift actions</strong>
                  <button type="button" onClick={() => showWorkflow('post')}>{selectedShift.tone === 'posted' ? 'Review posting' : 'Post this shift'} <span>›</span></button>
                  <button type="button" onClick={() => showWorkflow('trade')}>Propose a trade <span>›</span></button>
                  <button type="button" onClick={() => showWorkflow('split')}>Split shift <span>›</span></button>
                </div>
              ) : selectedShift.postingMode === 'pickup' || selectedShift.tone === 'open' ? (
                <div className="mobile-shift-actions"><strong>{selectedShift.tone === 'open' ? 'Open shift' : 'Available shift'}</strong><button type="button" onClick={() => showWorkflow('pickup')}>Pickup Shift <span>›</span></button></div>
              ) : selectedShift.postingMode === 'trade' ? (
                <div className="mobile-shift-actions"><strong>Trade-only shift</strong><button type="button" onClick={() => showWorkflow('offer')}>Offer one of my shifts <span>›</span></button></div>
              ) : <p className="mobile-shift-readonly">No actions are available for this shift.</p>}
            </section>
          </div>
        )}

        {showScope && (
          <div className="mobile-sheet-backdrop" onClick={() => setShowScope(false)}>
            <section className="mobile-sheet" onClick={(event) => event.stopPropagation()}>
              <div className="mobile-sheet-handle" />
              <header><strong>Schedule scope</strong><button type="button" onClick={() => setShowScope(false)} aria-label="Close schedule scope">×</button></header>
              <label>Region<select value={selectedRegion} onChange={(event) => setSelectedRegion(event.target.value)}><option>Lowcountry Emergency Physicians</option><option>Roper North</option></select></label>
              <fieldset><legend>Domains</legend>{['Physician', 'APP'].map((domain) => <label key={domain}><input type="checkbox" checked={selectedDomains.includes(domain)} onChange={() => toggleDomain(domain)} />{domain}</label>)}</fieldset>
              <button type="button" className="mobile-sheet-done" onClick={() => setShowScope(false)}>Done</button>
            </section>
          </div>
        )}

        {showMore && (
          <div className="mobile-sheet-backdrop" onClick={() => setShowMore(false)}>
            <section className="mobile-sheet mobile-more-sheet" onClick={(event) => event.stopPropagation()}>
              <div className="mobile-sheet-handle" />
              <header><strong>More</strong><button type="button" onClick={() => setShowMore(false)} aria-label="Close More menu">×</button></header>
              <div className="mobile-account-summary"><strong>{user?.first_name} {user?.last_name}</strong><span>Signed in on this device</span></div>
              {user?.can_manage_schedules && <div className="mobile-more-view-choice"><span>Interface view</span><div className="mobile-testing-switch" role="group" aria-label="Interface view"><button type="button" className={!testUserView ? 'active' : ''} onClick={() => setTestUserView(false)}>Scheduler</button><button type="button" className={testUserView ? 'active' : ''} onClick={() => setTestUserView(true)}>User</button></div></div>}
              <button type="button" className="mobile-more-scope-action" onClick={() => { setShowMore(false); setShowScope(true) }}><span><strong>Schedule scope</strong><small>{selectedRegion.replace(' Emergency Physicians', '')} · {selectedDomains.length === 2 ? 'All domains' : selectedDomains.join(', ')}</small></span><b>›</b></button>
              <button type="button" className="mobile-more-action">Account settings <span>›</span></button>
              <button type="button" className="mobile-more-action">Sign out <span>›</span></button>
            </section>
          </div>
        )}

        <nav className="mobile-bottom-nav" aria-label="Primary mobile navigation">
          {primaryItems.map((item) => (
            <button type="button" key={item.id} className={(item.id === activePage && !showMore) || (item.id === 'more' && showMore) ? 'active' : ''} aria-current={item.id === activePage && !showMore ? 'page' : undefined} onClick={() => selectPrimary(item.id)}><span aria-hidden="true">{item.icon}</span>{item.label}</button>
          ))}
        </nav>
      </div>

      <aside className="mobile-preview-notes"><strong>Updated direction</strong><ul><li>Light, schedule-first interface</li><li>No branding or identity bar</li><li>Compact List/Month and Group/My schedule controls</li><li>Region and domain scope lives in More</li><li>No Schedule Block or administrative navigation</li><li>Last page and view are remembered on this device</li></ul></aside>
    </div>
  )
}
