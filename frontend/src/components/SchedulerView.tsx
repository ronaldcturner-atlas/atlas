import React, { useEffect, useMemo, useState } from 'react'

type APIShift = {
  id: number
  facility_name: string
  role: string
  physician_name: string
  start_datetime: string
  end_datetime: string
  status: string
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
}

function formatTime(date: Date) {
  const hour = date.getUTCHours()
  const suffix = hour < 12 ? 'a' : 'p'
  const displayHour = hour % 12 || 12
  return `${displayHour}${suffix}`
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

export default function SchedulerView({ facilitiesRefreshToken }: SchedulerViewProps) {
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
  }, [facilitiesRefreshToken])

  const activeFacilities = useMemo(
    () => facilities.filter((facility) => facility.active).sort((a, b) => a.name.localeCompare(b.name)),
    [facilities],
  )

  const groupedShifts = useMemo<ShiftGroup[]>(() => {
    const groupedMap = new Map<string, ShiftGroup>()

    allShifts.forEach((shift) => {
      const startDate = new Date(shift.start_datetime)
      const endDate = new Date(shift.end_datetime)
      const dayKey = startDate.toISOString().slice(0, 10)

      if (!groupedMap.has(dayKey)) {
        groupedMap.set(dayKey, {
          label: formatDateLabel(startDate),
          items: [],
        })
      }

      groupedMap.get(dayKey)!.items.push({
        id: shift.id,
        facility: shift.facility_name,
        shiftTime: `${formatTime(startDate)}-${formatTime(endDate)}`,
        physician: shift.physician_name,
        role: shift.role,
        status: shift.status.charAt(0).toUpperCase() + shift.status.slice(1),
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