import React, { useState, useEffect } from 'react'

type APIShift = {
  id: number
  facility: number
  facility_name: string
  role: string
  physician: number
  physician_name: string
  start_datetime: string
  end_datetime: string
  status: string
}

type Shift = {
  facility: string
  shift: string
  role: string
  physician_name: string
  date: string
  status: string
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
  const hour = date.getUTCHours()
  const suffix = hour < 12 ? 'a' : 'p'
  const displayHour = hour % 12 || 12
  return `${displayHour}${suffix}`
}

export default function Calendar(){
  const today = new Date()

  // viewDate represents the first day of the currently displayed month
  const [viewDate, setViewDate] = useState<Date>(new Date(today.getFullYear(), today.getMonth(), 1))
  const [selectedShift, setSelectedShift] = useState<Shift | null>(null)
  const [allShifts, setAllShifts] = useState<APIShift[]>([])
  const [isLoading, setIsLoading] = useState(true)

  // Fetch shifts from API
  useEffect(() => {
    const fetchShifts = async () => {
      try {
        const response = await fetch('http://localhost:8000/api/shifts/')
        const data = await response.json()
        setAllShifts(data)
      } catch (error) {
        console.error('Error fetching shifts:', error)
      } finally {
        setIsLoading(false)
      }
    }

    fetchShifts()
  }, [])

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
    const startDate = new Date(apiShift.start_datetime)
    const endDate = new Date(apiShift.end_datetime)
    
    // Only show shifts from the current month
    if (startDate.getUTCFullYear() === year && startDate.getUTCMonth() === month) {
      const dayNum = startDate.getUTCDate()
      
      // Format time range (e.g., "7a–7p")
      const shift = `${formatDisplayTime(startDate)}–${formatDisplayTime(endDate)}`
      
      // Format date string
      const dateStr = startDate.toLocaleDateString('en-US', { year: 'numeric', month: 'long', day: 'numeric', timeZone: 'UTC' })
      
      // Capitalize status
      const statusCapitalized = apiShift.status.charAt(0).toUpperCase() + apiShift.status.slice(1)
      
      if (!shifts[dayNum]) {
        shifts[dayNum] = []
      }
      
      shifts[dayNum].push({
        facility: apiShift.facility_name,
        shift,
        role: apiShift.role,
        physician_name: apiShift.physician_name,
        date: dateStr,
        status: statusCapitalized
      })
    }
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
        <div className="controls">
          <button onClick={goToday}>Today</button>
          <button className="primary">Month</button>
          <button>Week</button>
          <button>Day</button>
        </div>
      </div>

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
                        className={`shift-item clickable ${getShiftTone(shift.role)}`}
                        onClick={() => setSelectedShift(shift)}
                      >
                        <div className="shift-time-row">
                          <div className="shift-time">{shift.shift}</div>
                          <div className="shift-role-badge">{shift.role}</div>
                        </div>
                        <div className="shift-physician">{shift.physician_name}</div>
                        <div className="shift-facility">{shift.facility}</div>
                      </div>
                    ))}
                  </div>
                )}
              </>
            )}
          </div>
        ))}
      </div>

      {!hasShifts && (
        <div style={{marginTop:16}}>
          <div className="empty-state">No shifts scheduled</div>
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
