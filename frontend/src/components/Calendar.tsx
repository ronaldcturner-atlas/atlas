import React, { useState, useEffect, useRef } from 'react'
import { useAuth } from '../contexts/AuthContext'

type APIShift = {
  id: number | null
  shift_instance_id: number
  facility: number
  facility_name: string
  facility_short_name: string
  facility_sort_order: number
  physician: number | null
  physician_name: string
  role: string
  role_display: string
  date: string
  start_time: string
  end_time: string
  status: string
  status_display: string
  posting_mode: 'PICKUP' | 'TRADE_ONLY' | null
  split_group_id: number | null
  split_group_start_time: string | null
  is_split: boolean
}

type Shift = {
  assignmentId: number | null
  instanceId: number
  physicianId: number | null
  facility: string
  facilityOrder: number
  shift: string
  role: string
  physician_name: string
  date: string
  status: string
  postingMode: 'PICKUP' | 'TRADE_ONLY' | null
  startTime: string
  endTime: string
  splitGroupId: number | null
  splitGroupStartTime: string | null
  isSplit: boolean
}

type Trade = {
  id: number
  trade_type: 'PICKUP' | 'TRADE'
  status_display: string
  status: 'PENDING_RECIPIENT' | 'PENDING_SCHEDULER' | 'DECLINED' | 'APPROVED' | 'CANCELLED'
  offered_assignment: { id: number; physician_name: string; date: string; facility: string; start_time: string; end_time: string }
  requested_assignment: { id: number; physician_name: string; date: string; facility: string; start_time: string; end_time: string } | null
  can_accept: boolean
  can_cancel: boolean
  can_review: boolean
}

type PhysicianOption = {
  id: number
  first_name: string
  last_name: string
  display_name: string
  active: boolean
}

type TradeOption = {
  id: number
  physician_id: number
  physician_name: string
  date: string
  facility: string
  start_time: string
  end_time: string
}

const SHIFT_TONE_CLASS: Record<string, string> = {
  '7a-7p': 'shift-tone-day',
  '7p-7a': 'shift-tone-night',
  '9a-9p': 'shift-tone-long-day',
  '1p-1a': 'shift-tone-swing',
  'fast-track': 'shift-tone-fast-track',
  midday: 'shift-tone-midday',
}

function getShiftTone(role: string) {
  const normalized = role.toLowerCase().replace(/[^a-z0-9]+/g, '-')
  return SHIFT_TONE_CLASS[normalized] ?? 'shift-tone-default'
}

function formatDisplayTime(date: Date) {
  const hour = date.getHours()
  const suffix = hour < 12 ? 'a' : 'p'
  const displayHour = hour % 12 || 12
  const minutes = date.getMinutes()
  return `${displayHour}${minutes ? `:${String(minutes).padStart(2, '0')}` : ''}${suffix}`
}

function parseDateTime(dateValue: string, timeValue: string) {
  return new Date(`${dateValue}T${timeValue}`)
}

function formatClockValue(timeValue: string) {
  return formatDisplayTime(parseDateTime('2000-01-01', timeValue))
}

type CalendarProps = {
  shiftsRefreshToken: number
}

export default function Calendar({ shiftsRefreshToken }: CalendarProps){
  const { user } = useAuth()
  const today = new Date()
  const physicianFilterRef = useRef<HTMLDetailsElement>(null)

  // viewDate represents the first day of the currently displayed month
  const [viewDate, setViewDate] = useState<Date>(new Date(today.getFullYear(), today.getMonth(), 1))
  const [selectedShift, setSelectedShift] = useState<Shift | null>(null)
  const [allShifts, setAllShifts] = useState<APIShift[]>([])
  const [physicians, setPhysicians] = useState<PhysicianOption[]>([])
  const [selectedPhysicianIds, setSelectedPhysicianIds] = useState<number[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [trades, setTrades] = useState<Trade[]>([])
  const [tradePolicy, setTradePolicy] = useState({ require_scheduler_approval: true, can_manage: false })
  const [showTrades, setShowTrades] = useState(false)
  const [splitTime, setSplitTime] = useState('12:00')
  const [tradeOnlyPosting, setTradeOnlyPosting] = useState(false)
  const [tradeOptions, setTradeOptions] = useState<TradeOption[]>([])
  const [tradePartnerId, setTradePartnerId] = useState<number | ''>('')
  const [tradeTargetId, setTradeTargetId] = useState<number | ''>('')
  const [tradeNote, setTradeNote] = useState('')
  const [offeredAssignmentId, setOfferedAssignmentId] = useState<number | ''>('')
  const [reassignPhysicianId, setReassignPhysicianId] = useState<number | ''>('')
  const [actualStartTime, setActualStartTime] = useState('')
  const [actualEndTime, setActualEndTime] = useState('')
  const [isMutating, setIsMutating] = useState(false)
  const [localRefreshToken, setLocalRefreshToken] = useState(0)

  // Fetch shifts from API
  useEffect(() => {
    const fetchShifts = async () => {
      try {
        setLoadError(null)
        const [shiftsResponse, physiciansResponse, tradesResponse, policyResponse] = await Promise.all([
          fetch('http://localhost:8000/api/published-schedule/', { credentials: 'include' }),
          fetch('http://localhost:8000/api/physicians/', { credentials: 'include' }),
          fetch('http://localhost:8000/api/shift-trades/', { credentials: 'include' }),
          fetch('http://localhost:8000/api/shift-trade-policy/', { credentials: 'include' }),
        ])
        if (!shiftsResponse.ok || !physiciansResponse.ok) {
          throw new Error('Unable to load the schedule filters')
        }
        const [shiftsData, physiciansData, tradesData, policyData] = await Promise.all([
          shiftsResponse.json(),
          physiciansResponse.json(),
          tradesResponse.ok ? tradesResponse.json() : [],
          policyResponse.ok ? policyResponse.json() : tradePolicy,
        ])
        setAllShifts(shiftsData)
        setPhysicians(physiciansData)
        setTrades(tradesData)
        setTradePolicy(policyData)
      } catch (error) {
        console.error('Error fetching shifts:', error)
        setLoadError(error instanceof Error ? error.message : 'Unable to load the schedule')
      } finally {
        setIsLoading(false)
      }
    }

    fetchShifts()
  }, [shiftsRefreshToken, localRefreshToken])

  useEffect(() => {
    const closePhysicianFilter = (event: MouseEvent) => {
      const menu = physicianFilterRef.current
      if (menu?.open && event.target instanceof Node && !menu.contains(event.target)) {
        menu.removeAttribute('open')
      }
    }

    document.addEventListener('mousedown', closePhysicianFilter)
    return () => document.removeEventListener('mousedown', closePhysicianFilter)
  }, [])

  const sortedPhysicians = [...physicians].sort((left, right) => {
    const leftName = left.display_name || `${left.first_name} ${left.last_name}`
    const rightName = right.display_name || `${right.first_name} ${right.last_name}`
    return leftName.localeCompare(rightName)
  })
  const normalizedUserName = `${user?.first_name ?? ''} ${user?.last_name ?? ''}`.trim().toLowerCase()
  const userLastName = (user?.last_name ?? '').trim().toLowerCase()
  const exactNamePhysician = normalizedUserName
    ? physicians.find((physician) => (
        `${physician.first_name} ${physician.last_name}`.trim().toLowerCase() === normalizedUserName
        || physician.display_name.trim().toLowerCase() === normalizedUserName
      ))
    : undefined
  const lastNameMatches = userLastName
    ? physicians.filter((physician) => (
        physician.last_name.trim().toLowerCase() === userLastName
        || physician.display_name.trim().toLowerCase() === userLastName
      ))
    : []
  const myPhysicianId = user?.physician_id
    ?? exactNamePhysician?.id
    ?? (lastNameMatches.length === 1 ? lastNameMatches[0].id : null)

  useEffect(() => {
    setTradeOptions([])
    setTradePartnerId('')
    setTradeTargetId('')
    setTradeNote('')
    if (!selectedShift || selectedShift.physicianId !== myPhysicianId) return
    fetch(`http://localhost:8000/api/schedule-assignments/${selectedShift.assignmentId}/trade-options/`, { credentials: 'include' })
      .then(async (response) => {
        const data = await response.json()
        if (!response.ok) throw new Error(data.detail ?? 'Unable to load trade options.')
        setTradeOptions(data)
      })
      .catch((error) => setLoadError(error instanceof Error ? error.message : 'Unable to load trade options.'))
  }, [selectedShift?.assignmentId, selectedShift?.physicianId, myPhysicianId])

  const selectedPhysicianSet = new Set(selectedPhysicianIds)
  const isGroupSchedule = selectedPhysicianIds.length === 0
  const isMySchedule = myPhysicianId != null
    && selectedPhysicianIds.length === 1
    && selectedPhysicianIds[0] === myPhysicianId

  const toggleMySchedule = () => {
    if (myPhysicianId == null) {
      return
    }
    setSelectedPhysicianIds(isMySchedule ? [] : [myPhysicianId])
  }

  const togglePhysician = (physicianId: number) => {
    setSelectedPhysicianIds((current) => (
      current.includes(physicianId)
        ? current.filter((id) => id !== physicianId)
        : [...current, physicianId]
    ))
  }

  const year = viewDate.getFullYear()
  const month = viewDate.getMonth() // 0 = January

  // compute month layout dynamically
  const startingDayOfWeek = new Date(year, month, 1).getDay()
  const daysInMonth = new Date(year, month + 1, 0).getDate()
  const totalCells = startingDayOfWeek + daysInMonth
  const rows = Math.ceil(totalCells / 7)
  const cells = rows * 7

  // Convert API shifts to calendar format
  const shifts: Record<number, Shift[]> = {}
  allShifts.forEach((apiShift) => {
    if (!isGroupSchedule && !selectedPhysicianSet.has(apiShift.physician)) {
      return
    }
    const startDate = parseDateTime(apiShift.date, apiShift.start_time)
    const endDate = parseDateTime(apiShift.date, apiShift.end_time)
    
    // Only show shifts from the current month
    if (startDate.getFullYear() === year && startDate.getMonth() === month) {
      const dayNum = startDate.getDate()
      
      // Format time range (e.g., "7a–7p")
      const shift = `${formatDisplayTime(startDate)}-${formatDisplayTime(endDate)}`
      
      // Format date string
      const dateStr = startDate.toLocaleDateString('en-US', { year: 'numeric', month: 'long', day: 'numeric' })
      
      // Capitalize status
      const statusCapitalized = apiShift.status_display
      
      if (!shifts[dayNum]) {
        shifts[dayNum] = []
      }
      
      shifts[dayNum].push({
        assignmentId: apiShift.id,
        instanceId: apiShift.shift_instance_id,
        physicianId: apiShift.physician,
        facility: apiShift.facility_short_name || apiShift.facility_name,
        facilityOrder: apiShift.facility_sort_order,
        shift,
        role: apiShift.role_display,
        physician_name: apiShift.physician_name,
        date: dateStr,
        status: statusCapitalized,
        postingMode: apiShift.posting_mode,
        startTime: apiShift.start_time,
        endTime: apiShift.end_time,
        splitGroupId: apiShift.split_group_id,
        splitGroupStartTime: apiShift.split_group_start_time,
        isSplit: apiShift.is_split,
      })
    }
  })

  Object.values(shifts).forEach((dayShifts) => {
    dayShifts.sort((left, right) => {
      const facilityComparison = left.facilityOrder - right.facilityOrder
        || left.facility.localeCompare(right.facility)
      if (facilityComparison) return facilityComparison
      const leftAnchor = left.splitGroupStartTime || left.startTime
      const rightAnchor = right.splitGroupStartTime || right.startTime
      const anchorComparison = leftAnchor.localeCompare(rightAnchor)
      if (anchorComparison) return anchorComparison
      if (left.splitGroupId != null && left.splitGroupId === right.splitGroupId) {
        return left.startTime.localeCompare(right.startTime)
      }
      return left.startTime.localeCompare(right.startTime)
        || left.endTime.localeCompare(right.endTime)
        || left.role.localeCompare(right.role)
        || left.physician_name.localeCompare(right.physician_name)
    })
  })

  // Create grid mapping day numbers to cells
  const days = Array.from({length: cells}).map((_, i) => {
    const dayNum = i - startingDayOfWeek + 1
    return dayNum >= 1 && dayNum <= daysInMonth ? dayNum : null
  })

  const hasShifts = Object.keys(shifts).length > 0

  const goPrev = () => setViewDate(d => new Date(d.getFullYear(), d.getMonth() - 1, 1))
  const goNext = () => setViewDate(d => new Date(d.getFullYear(), d.getMonth() + 1, 1))
  const goToday = () => setViewDate(new Date(today.getFullYear(), today.getMonth(), 1))
  const canManage = Boolean(user?.is_staff || user?.is_superuser || user?.groups.some((group) => ['admin', 'scheduler'].includes(group.toLowerCase())))
  const myAssignments = allShifts.filter((shift) => shift.physician === myPhysicianId)
  const pendingTradeCount = trades.filter((trade) => trade.can_accept || trade.can_review).length
  const pendingTrades = trades.filter((trade) => ['PENDING_RECIPIENT', 'PENDING_SCHEDULER'].includes(trade.status))
  const pendingAssignmentIds = new Set(pendingTrades.flatMap((trade) => [
    trade.offered_assignment.id,
    ...(trade.requested_assignment ? [trade.requested_assignment.id] : []),
  ]))
  const statusClassForShift = (shift: Shift) => {
    if (shift.status.toLowerCase() === 'open') return 'shift-status-open'
    const isOwn = shift.physicianId === myPhysicianId
    if (isOwn && shift.assignmentId != null && pendingAssignmentIds.has(shift.assignmentId)) return 'shift-status-own-pending'
    if (isOwn && shift.postingMode) return 'shift-status-own-posted'
    if (!isOwn && shift.postingMode) return 'shift-status-posted-other'
    if (isOwn) return 'shift-status-own'
    return getShiftTone(shift.role)
  }
  const tradePartners = Array.from(new Map(
    tradeOptions.map((option) => [option.physician_id, option.physician_name]),
  ).entries()).sort((left, right) => left[1].localeCompare(right[1]))
  const selectedPartnerShifts = tradePartnerId === ''
    ? []
    : tradeOptions.filter((option) => option.physician_id === tradePartnerId)

  const mutate = async (url: string, body: Record<string, unknown>, method = 'POST') => {
    try {
      setIsMutating(true)
      setLoadError(null)
      const response = await fetch(`http://localhost:8000/api/${url}`, {
        method, credentials: 'include', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
      })
      const data = await response.json().catch(() => ({}))
      if (!response.ok) throw new Error(data.detail ?? 'Unable to complete that action.')
      setSelectedShift(null)
      setLocalRefreshToken((current) => current + 1)
    } catch (error) {
      setLoadError(error instanceof Error ? error.message : 'Unable to complete that action.')
    } finally {
      setIsMutating(false)
    }
  }

  return (
    <div className="calendar-card">
      <div className="calendar-header">
        <div style={{display: 'flex', alignItems: 'center', gap: 8}}>
          <button onClick={goPrev} aria-label="Previous month">◀</button>
          <div className="month-label">{viewDate.toLocaleString(undefined, { month: 'long', year: 'numeric' })}</div>
          <button onClick={goNext} aria-label="Next month">▶</button>
        </div>
        <div className="schedule-toolbar">
          <button type="button" className="trade-center-button" onClick={() => setShowTrades(true)}>
            Trade requests{pendingTradeCount ? ` (${pendingTradeCount})` : ''}
          </button>
          <label className={`my-schedule-filter ${isMySchedule ? 'selected' : ''} ${myPhysicianId == null ? 'disabled' : ''}`}>
            <input
              type="checkbox"
              checked={isMySchedule}
              disabled={myPhysicianId == null}
              onChange={toggleMySchedule}
            />
            My Schedule
          </label>
          <details ref={physicianFilterRef} className="physician-filter-menu">
            <summary>
              {isGroupSchedule
                ? 'All physicians'
                : `${selectedPhysicianIds.length} physician${selectedPhysicianIds.length === 1 ? '' : 's'}`}
            </summary>
            <div className="physician-filter-popover">
              <div className="physician-filter-heading">
                <strong>Show schedules</strong>
                <button type="button" onClick={() => setSelectedPhysicianIds([])} disabled={isGroupSchedule}>Clear</button>
              </div>
              <div className="physician-filter-list">
                {sortedPhysicians.map((physician) => {
                  const name = physician.display_name || `${physician.first_name} ${physician.last_name}`.trim()
                  return (
                    <label key={physician.id} className={!physician.active ? 'inactive' : ''}>
                      <input
                        type="checkbox"
                        checked={selectedPhysicianSet.has(physician.id)}
                        onChange={() => togglePhysician(physician.id)}
                      />
                      <span>{name}</span>
                      {!physician.active && <small>Inactive</small>}
                    </label>
                  )
                })}
              </div>
            </div>
          </details>
          <div className="controls">
          <button onClick={goToday}>Today</button>
          <button className="primary">Month</button>
          </div>
        </div>
      </div>

      {loadError && <div className="schedule-filter-error">{loadError}</div>}
      <div className="schedule-status-legend" aria-label="Schedule highlight legend">
        <span className="shift-status-own">Your shift</span>
        <span className="shift-status-posted-other">Posted by another user</span>
        <span className="shift-status-own-posted">Your posted shift</span>
        <span className="shift-status-own-pending">Your pending trade</span>
        <span className="shift-status-open">Open shift</span>
      </div>
      {!isGroupSchedule && (
        <div className="schedule-filter-status">
          Showing {selectedPhysicianIds.length} selected physician{selectedPhysicianIds.length === 1 ? '' : 's'}.
          <button type="button" onClick={() => setSelectedPhysicianIds([])}>Return to group schedule</button>
        </div>
      )}

      <div className="grid">
        {days.map((dayNum, i) => (
          <div key={i} className="day-cell">
            {dayNum && (
              <>
                <div className="day-number">{dayNum}</div>
                {shifts[dayNum] && (
                  <div className="shifts-container">
                    {shifts[dayNum].map((shift, idx) => (
                      <div
                        key={idx}
                        className={`shift-item shift-item-compact clickable ${statusClassForShift(shift)}`}
                        onClick={() => {
                          setSelectedShift(shift)
                          setTradeOnlyPosting(shift.postingMode === 'TRADE_ONLY')
                          setActualStartTime(shift.startTime.slice(0, 5))
                          setActualEndTime(shift.endTime.slice(0, 5))
                        }}
                      >
                        <span>{shift.facility} {shift.shift} {shift.physician_name}</span>
                      </div>
                    ))}
                  </div>
                )}
              </>
            )}
          </div>
        ))}
      </div>

      {!isLoading && !hasShifts && (
        <div style={{marginTop:16}}>
          <div className="empty-state">{isGroupSchedule ? 'No shifts scheduled' : 'No scheduled shifts for the selected physicians'}</div>
        </div>
      )}

      {selectedShift && (
        <div className="shift-modal-overlay" onClick={() => setSelectedShift(null)}>
          <div className="shift-modal" onClick={(e) => e.stopPropagation()}>
            <div className="shift-modal-header">
              <h2>Shift details</h2>
            </div>
            <div className="shift-modal-body">
              <div className="detail-row"><span>Facility</span><span>{selectedShift.facility}</span></div>
              <div className="detail-row"><span>Physician</span><span>{selectedShift.physician_name}</span></div>
              <div className="detail-row"><span>Role</span><span>{selectedShift.role}</span></div>
              <div className="detail-row"><span>Date</span><span>{selectedShift.date}</span></div>
              <div className="detail-row"><span>Time</span><span>{selectedShift.shift}</span></div>
              <div className="detail-row"><span>Status</span><span>{selectedShift.status}</span></div>
              {selectedShift.postingMode && <div className="detail-row"><span>Posted</span><span>{selectedShift.postingMode === 'PICKUP' ? 'Available for pickup' : 'Trade only'}</span></div>}
              {selectedShift.assignmentId != null && (selectedShift.physicianId === myPhysicianId || canManage) && (
                <div className="schedule-shift-actions">
                  <strong>Post this shift</strong>
                  <div className="shift-post-controls">
                    <label><input type="radio" checked={tradeOnlyPosting} onChange={() => setTradeOnlyPosting(true)} /> Trade only</label>
                    <button disabled={isMutating} onClick={() => mutate(`schedule-assignments/${selectedShift.assignmentId}/posting/`, { mode: tradeOnlyPosting ? 'TRADE_ONLY' : 'PICKUP' })}>Post</button>
                    {selectedShift.postingMode && <button disabled={isMutating} onClick={() => mutate(`schedule-assignments/${selectedShift.assignmentId}/posting/`, { mode: 'CLOSE' })}>Remove posting</button>}
                  </div>
                  <strong>Split shift</strong>
                  <div><input type="time" value={splitTime} onChange={(event) => setSplitTime(event.target.value)} /><button disabled={isMutating} onClick={() => mutate(`schedule-assignments/${selectedShift.assignmentId}/split/`, { split_time: splitTime })}>Split</button></div>
                  {selectedShift.isSplit && <button disabled={isMutating} onClick={() => mutate(`schedule-assignments/${selectedShift.assignmentId}/unsplit/`, {})}>Unsplit shift</button>}
                  {selectedShift.physicianId === myPhysicianId && (
                    <div className="propose-trade-controls">
                      <strong>Propose a trade</strong>
                      <label>
                        Trade with
                        <select value={tradePartnerId} onChange={(event) => { setTradePartnerId(Number(event.target.value) || ''); setTradeTargetId('') }}>
                          <option value="">Choose physician</option>
                          {tradePartners.map(([physicianId, physicianName]) => <option key={physicianId} value={physicianId}>{physicianName}</option>)}
                        </select>
                      </label>
                      <label>
                        Shift requested
                        <select value={tradeTargetId} disabled={tradePartnerId === ''} onChange={(event) => setTradeTargetId(Number(event.target.value) || '')}>
                          <option value="">Choose shift</option>
                          {selectedPartnerShifts.map((option) => (
                            <option key={option.id} value={option.id}>
                              {option.date} · {option.facility} · {formatClockValue(option.start_time)}-{formatClockValue(option.end_time)}
                            </option>
                          ))}
                        </select>
                      </label>
                      <label>
                        Comments (optional)
                        <textarea value={tradeNote} onChange={(event) => setTradeNote(event.target.value)} />
                      </label>
                      <button disabled={isMutating || !tradeTargetId} onClick={() => mutate('shift-trades/', { offered_assignment_id: selectedShift.assignmentId, target_assignment_id: tradeTargetId, note: tradeNote })}>Send trade proposal</button>
                      {!tradeOptions.length && <small>No conflict-free trade options are currently available.</small>}
                    </div>
                  )}
                </div>
              )}
              {selectedShift.assignmentId != null && selectedShift.physicianId !== myPhysicianId && selectedShift.postingMode && myPhysicianId != null && (
                <div className="schedule-shift-actions">
                  <strong>{selectedShift.postingMode === 'PICKUP' ? 'Request pickup' : 'Offer a trade'}</strong>
                  {selectedShift.postingMode === 'TRADE_ONLY' && (
                    <select value={offeredAssignmentId} onChange={(event) => setOfferedAssignmentId(Number(event.target.value) || '')}>
                      <option value="">Choose one of your shifts</option>
                      {myAssignments.map((shift) => <option key={shift.id} value={shift.id}>{shift.date} · {shift.facility_short_name} · {shift.start_time}-{shift.end_time}</option>)}
                    </select>
                  )}
                  <button disabled={isMutating || (selectedShift.postingMode === 'TRADE_ONLY' && !offeredAssignmentId)} onClick={() => mutate('shift-trades/', { target_assignment_id: selectedShift.assignmentId, offered_assignment_id: offeredAssignmentId || null })}>Send request</button>
                </div>
              )}
              {selectedShift.assignmentId != null && canManage && (
                <div className="schedule-shift-actions">
                  <strong>Actual shift times</strong>
                  <small>Changes this date only. The recurring Shift Builder template will not change.</small>
                  <div className="actual-shift-time-controls">
                    <label>Start<input type="time" value={actualStartTime} onChange={(event) => setActualStartTime(event.target.value)} /></label>
                    <label>End<input type="time" value={actualEndTime} onChange={(event) => setActualEndTime(event.target.value)} /></label>
                    <button disabled={isMutating || !actualStartTime || !actualEndTime} onClick={() => mutate(`shift-instances/${selectedShift.instanceId}/times/`, { start_time: actualStartTime, end_time: actualEndTime }, 'PATCH')}>Update times</button>
                  </div>
                  <strong>Change scheduled user</strong>
                  <select value={reassignPhysicianId} onChange={(event) => setReassignPhysicianId(Number(event.target.value) || '')}>
                    <option value="">Choose physician</option>
                    {sortedPhysicians.filter((physician) => physician.active).map((physician) => <option key={physician.id} value={physician.id}>{physician.display_name}</option>)}
                  </select>
                  <button disabled={isMutating || !reassignPhysicianId} onClick={() => mutate(`schedule-assignments/${selectedShift.assignmentId}/reassign/`, { physician_id: reassignPhysicianId })}>Change user</button>
                </div>
              )}
              {selectedShift.assignmentId != null && pendingTrades.filter((trade) => (
                trade.can_cancel && (
                  trade.offered_assignment.id === selectedShift.assignmentId
                  || trade.requested_assignment?.id === selectedShift.assignmentId
                )
              )).map((trade) => (
                <div className="schedule-shift-actions" key={trade.id}>
                  <strong>Pending trade offer</strong>
                  <button disabled={isMutating} onClick={() => mutate(`shift-trades/${trade.id}/cancel/`, {})}>Cancel trade offer</button>
                </div>
              ))}
            </div>
            <div className="shift-modal-actions">
              <button className="secondary" onClick={() => setSelectedShift(null)}>Close</button>
            </div>
          </div>
        </div>
      )}

      {showTrades && (
        <div className="shift-modal-overlay" onClick={() => setShowTrades(false)}>
          <div className="shift-modal shift-trade-modal" onClick={(event) => event.stopPropagation()}>
            <div className="shift-modal-header"><h2>Shift trade requests</h2></div>
            {tradePolicy.can_manage && <label className="trade-policy"><input type="checkbox" checked={tradePolicy.require_scheduler_approval} onChange={(event) => mutate('shift-trade-policy/', { require_scheduler_approval: event.target.checked }, 'PATCH')} /> Require scheduler approval</label>}
            <div className="trade-request-list">
              {trades.map((trade) => (
                <div className="trade-request-card" key={trade.id}>
                  <strong>{trade.trade_type === 'PICKUP' ? 'Pickup' : 'Trade'} · {trade.status_display}</strong>
                  <span>{trade.offered_assignment.date} · {trade.offered_assignment.facility} · {trade.offered_assignment.physician_name}</span>
                  {trade.requested_assignment && <span>For {trade.requested_assignment.date} · {trade.requested_assignment.facility} · {trade.requested_assignment.physician_name}</span>}
                  <div>{trade.can_accept && <><button onClick={() => mutate(`shift-trades/${trade.id}/accept/`, {})}>Accept</button><button onClick={() => mutate(`shift-trades/${trade.id}/decline/`, {})}>Decline</button></>}{trade.can_review && <><button onClick={() => mutate(`shift-trades/${trade.id}/approve/`, {})}>Approve</button><button onClick={() => mutate(`shift-trades/${trade.id}/reject/`, {})}>Reject</button></>}{trade.can_cancel && <button onClick={() => mutate(`shift-trades/${trade.id}/cancel/`, {})}>Cancel</button>}</div>
                </div>
              ))}
              {!trades.length && <div className="empty-state">No trade requests</div>}
            </div>
            <div className="shift-modal-actions"><button onClick={() => setShowTrades(false)}>Close</button></div>
          </div>
        </div>
      )}
    </div>
  )
}
