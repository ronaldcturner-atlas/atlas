import React, { useState, useEffect, useRef } from 'react'
import { useAuth } from '../contexts/AuthContext'

type APIShift = {
  id: number
  facility: number
  facility_name: string
  facility_short_name: string
  facility_sort_order: number
  physician: number
  physician_name: string
  role: string
  role_display: string
  date: string
  start_time: string
  end_time: string
  status: string
  status_display: string
}

type Shift = {
  facility: string
  facilityOrder: number
  shift: string
  role: string
  physician_name: string
  date: string
  status: string
}

type PhysicianOption = {
  id: number
  first_name: string
  last_name: string
  display_name: string
  active: boolean
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

  // Fetch shifts from API
  useEffect(() => {
    const fetchShifts = async () => {
      try {
        setLoadError(null)
        const [shiftsResponse, physiciansResponse] = await Promise.all([
          fetch('http://localhost:8000/api/published-schedule/', { credentials: 'include' }),
          fetch('http://localhost:8000/api/physicians/', { credentials: 'include' }),
        ])
        if (!shiftsResponse.ok || !physiciansResponse.ok) {
          throw new Error('Unable to load the schedule filters')
        }
        const [shiftsData, physiciansData] = await Promise.all([
          shiftsResponse.json(),
          physiciansResponse.json(),
        ])
        setAllShifts(shiftsData)
        setPhysicians(physiciansData)
      } catch (error) {
        console.error('Error fetching shifts:', error)
        setLoadError(error instanceof Error ? error.message : 'Unable to load the schedule')
      } finally {
        setIsLoading(false)
      }
    }

    fetchShifts()
  }, [shiftsRefreshToken])

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
        facility: apiShift.facility_short_name || apiShift.facility_name,
        facilityOrder: apiShift.facility_sort_order,
        shift,
        role: apiShift.role_display,
        physician_name: apiShift.physician_name,
        date: dateStr,
        status: statusCapitalized
      })
    }
  })

  Object.values(shifts).forEach((dayShifts) => {
    dayShifts.sort((left, right) => (
      left.facilityOrder - right.facilityOrder
      || left.facility.localeCompare(right.facility)
      || left.shift.localeCompare(right.shift)
      || left.physician_name.localeCompare(right.physician_name)
    ))
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

  return (
    <div className="calendar-card">
      <div className="calendar-header">
        <div style={{display: 'flex', alignItems: 'center', gap: 8}}>
          <button onClick={goPrev} aria-label="Previous month">◀</button>
          <div className="month-label">{viewDate.toLocaleString(undefined, { month: 'long', year: 'numeric' })}</div>
          <button onClick={goNext} aria-label="Next month">▶</button>
        </div>
        <div className="schedule-toolbar">
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
                        className={`shift-item shift-item-compact clickable ${getShiftTone(shift.role)}`}
                        onClick={() => setSelectedShift(shift)}
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
            </div>
            <div className="shift-modal-actions">
              <button className="secondary" onClick={() => setSelectedShift(null)}>Close</button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
