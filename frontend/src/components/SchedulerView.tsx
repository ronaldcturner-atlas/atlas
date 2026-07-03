import React, { useEffect, useMemo, useState } from 'react'

type APIShift = {
  id: number
  facility_name: string
  physician_name: string
  role_display: string
  date: string
  start_time: string
  end_time: string
  status_display: string
}

type APIFacility = {
  id: number
  name: string
  timezone: string
  color: string
  active: boolean
}

type ShiftGroup = {
  label: string
  items: Array<{
    id: number
    facility: string
    shiftTime: string
    physician: string
    role: string
    status: string
  }>
}

type SchedulerViewProps = {
  facilitiesRefreshToken: number
  shiftsRefreshToken: number
}

function formatTime(timeValue: string) {
  const [hoursRaw, minutesRaw] = timeValue.split(':')
  const hour = Number(hoursRaw)
  const minutes = Number(minutesRaw)
  if (Number.isNaN(hour) || Number.isNaN(minutes)) {
    return timeValue
  }

  const suffix = hour < 12 ? 'a' : 'p'
  const displayHour = hour % 12 || 12
  return `${displayHour}:${String(minutes).padStart(2, '0')}${suffix}`
}

function formatDateLabel(date: Date) {
  return date.toLocaleDateString('en-US', {
    weekday: 'long',
    year: 'numeric',
    month: 'long',
    day: 'numeric',
    timeZone: 'UTC',
  })
}

export default function SchedulerView({ facilitiesRefreshToken, shiftsRefreshToken }: SchedulerViewProps) {
  const [allShifts, setAllShifts] = useState<APIShift[]>([])
  const [facilities, setFacilities] = useState<APIFacility[]>([])
  const [isLoading, setIsLoading] = useState(true)

  useEffect(() => {
    const fetchSchedulerData = async () => {
      try {
        const [shiftsResponse, facilitiesResponse] = await Promise.all([
          fetch('http://localhost:8000/api/shifts/', { credentials: 'include' }),
          fetch('http://localhost:8000/api/facilities/', { credentials: 'include' }),
        ])

        if (!shiftsResponse.ok || !facilitiesResponse.ok) {
          throw new Error('Unable to load scheduler data')
        }

        const shiftsData = await shiftsResponse.json()
        const facilitiesData = await facilitiesResponse.json()

        setAllShifts(shiftsData)
        setFacilities(facilitiesData)
      } catch (error) {
        console.error('Error fetching scheduler data:', error)
      } finally {
        setIsLoading(false)
      }
    }

    fetchSchedulerData()
  }, [facilitiesRefreshToken, shiftsRefreshToken])

  const activeFacilities = useMemo(
    () => facilities.filter((facility) => facility.active).sort((a, b) => a.name.localeCompare(b.name)),
    [facilities],
  )

  const groupedShifts = useMemo<ShiftGroup[]>(() => {
    const groupedMap = new Map<string, ShiftGroup>()

    allShifts.forEach((shift) => {
      const startDate = new Date(`${shift.date}T00:00:00`)
      const dayKey = shift.date

      if (!groupedMap.has(dayKey)) {
        groupedMap.set(dayKey, {
          label: formatDateLabel(startDate),
          items: [],
        })
      }

      groupedMap.get(dayKey)!.items.push({
        id: shift.id,
        facility: shift.facility_name,
        shiftTime: `${formatTime(shift.start_time)}-${formatTime(shift.end_time)}`,
        physician: shift.physician_name,
        role: shift.role_display,
        status: shift.status_display,
      })
    })

    return Array.from(groupedMap.entries())
      .sort(([dateA], [dateB]) => dateA.localeCompare(dateB))
      .map(([, group]) => ({
        ...group,
        items: group.items.sort((a, b) => a.shiftTime.localeCompare(b.shiftTime)),
      }))
  }, [allShifts])

  if (isLoading) {
    return <div className="scheduler-loading">Loading scheduler view...</div>
  }

  return (
    <div className="scheduler-view-card">
      <section className="scheduler-day-group">
        <h2>Active Facilities</h2>
        <div className="facility-chip-list">
          {activeFacilities.map((facility) => (
            <div key={facility.id} className="facility-chip">
              <span
                className="facility-color-swatch"
                style={{ backgroundColor: facility.color }}
                aria-hidden="true"
              />
              <span>{facility.name}</span>
              <span className="facility-timezone">{facility.timezone}</span>
            </div>
          ))}
          {!activeFacilities.length && <div className="empty-state">No active facilities</div>}
        </div>
      </section>

      {groupedShifts.map((group) => (
        <section key={group.label} className="scheduler-day-group">
          <h2>{group.label}</h2>
          <div className="scheduler-table-wrap">
            <table className="scheduler-table">
              <thead>
                <tr>
                  <th>Facility</th>
                  <th>Shift Time</th>
                  <th>Physician</th>
                  <th>Role</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {group.items.map((item) => (
                  <tr key={item.id}>
                    <td>{item.facility}</td>
                    <td>{item.shiftTime}</td>
                    <td>{item.physician}</td>
                    <td>{item.role}</td>
                    <td>{item.status}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      ))}

      {!groupedShifts.length && <div className="empty-state">No shifts scheduled</div>}
    </div>
  )
}