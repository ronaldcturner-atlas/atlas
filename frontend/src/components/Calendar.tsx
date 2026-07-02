import React, { useState } from 'react'

type Shift = {
  facility: string
  shift: string
  role: string
  date: string
  status: string
}

export default function Calendar(){
  // November 2026 starts on Sunday (day 0)
  // 35 cells for full 5-week grid
  const daysInMonth = 30
  const startingDayOfWeek = 0 // Sunday
  
  // Demo shift data
  const shifts: Record<number, Shift[]> = {
    12: [
      {
        facility: 'Berkeley',
        shift: '7a–7p',
        role: 'Physician',
        date: 'November 12, 2026',
        status: 'Scheduled'
      }
    ]
  }

  const [selectedShift, setSelectedShift] = useState<Shift | null>(null)

  // Create grid mapping day numbers to cells
  const days = Array.from({length: 35}).map((_, i) => {
    const dayNum = i - startingDayOfWeek + 1
    return dayNum > 0 && dayNum <= daysInMonth ? dayNum : null
  })

  const hasShifts = Object.keys(shifts).length > 0

  return (
    <div className="calendar-card">
      <div className="calendar-header">
        <div className="month-label">November 2026</div>
        <div className="controls">
          <button>Today</button>
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
                        className="shift-item clickable"
                        onClick={() => setSelectedShift(shift)}
                      >
                        <div className="shift-time">{shift.shift}</div>
                        <div className="shift-facility">{shift.facility}</div>
                        <div className="shift-role">{shift.role}</div>
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
