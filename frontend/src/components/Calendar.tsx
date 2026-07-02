import React from 'react'

export default function Calendar(){
  // November 2026 starts on Sunday (day 0)
  // 35 cells for full 5-week grid
  const daysInMonth = 30
  const startingDayOfWeek = 0 // Sunday
  
  // Demo shift data
  const shifts = {
    12: [
      {
        facility: 'Berkeley',
        shift: '7a–7p',
        role: 'Physician'
      }
    ]
  }

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
                      <div key={idx} className="shift-item">
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
    </div>
  )
}
