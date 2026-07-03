import React, { useEffect, useMemo, useState } from 'react'

type ShiftTemplate = {
  id: number
  facility: number
  facility_name: string
  name: string
  start_time: string
  end_time: string
  active_days_of_week: DayOfWeek[]
  weekend_days: WeekendDay[]
  night_shift: boolean
  default_staffing_count: number
  active: boolean
}

type FacilityOption = {
  id: number
  name: string
  active: boolean
}

type DayOfWeek =
  | 'Sunday'
  | 'Monday'
  | 'Tuesday'
  | 'Wednesday'
  | 'Thursday'
  | 'Friday'
  | 'Saturday'

type WeekendDay = 'Friday' | 'Saturday' | 'Sunday'

type ShiftTemplateFormState = {
  facility: string
  start_time: string
  end_time: string
  active_days_of_week: DayOfWeek[]
  weekend_days: WeekendDay[]
  night_shift: boolean
  default_staffing_count: string
  active: boolean
}

const API_BASE = 'http://localhost:8000/api'

const DAYS_OF_WEEK: DayOfWeek[] = [
  'Sunday',
  'Monday',
  'Tuesday',
  'Wednesday',
  'Thursday',
  'Friday',
  'Saturday',
]

const WEEKEND_DAY_OPTIONS: WeekendDay[] = ['Friday', 'Saturday', 'Sunday']

const defaultFormState: ShiftTemplateFormState = {
  facility: '',
  start_time: '07:00',
  end_time: '19:00',
  active_days_of_week: ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday'],
  weekend_days: ['Friday'],
  night_shift: false,
  default_staffing_count: '1',
  active: true,
}

async function getApiErrorMessage(response: Response) {
  try {
    const data = await response.json()

    if (typeof data === 'string') {
      return data
    }

    if (data?.error && typeof data.error === 'string') {
      return data.error
    }

    if (data?.detail && typeof data.detail === 'string') {
      return data.detail
    }

    if (data && typeof data === 'object') {
      const validationMessages = Object.entries(data)
        .flatMap(([field, value]) => {
          if (Array.isArray(value)) {
            return value.map((message) => `${field}: ${message}`)
          }

          if (typeof value === 'string') {
            return `${field}: ${value}`
          }

          return []
        })

      if (validationMessages.length) {
        return validationMessages.join(' ')
      }
    }
  } catch {
    return null
  }

  return null
}

function formatTimeLabel(timeValue: string) {
  const [hoursRaw, minutesRaw] = timeValue.split(':')
  const hours = Number(hoursRaw)
  const minutes = Number(minutesRaw)
  if (Number.isNaN(hours) || Number.isNaN(minutes)) {
    return timeValue
  }

  const suffix = hours >= 12 ? 'PM' : 'AM'
  const displayHour = hours % 12 || 12
  return `${displayHour}:${String(minutes).padStart(2, '0')} ${suffix}`
}

export default function ShiftsView() {
  const [templates, setTemplates] = useState<ShiftTemplate[]>([])
  const [facilities, setFacilities] = useState<FacilityOption[]>([])

  const [isLoading, setIsLoading] = useState(true)
  const [isSaving, setIsSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const [isModalOpen, setIsModalOpen] = useState(false)
  const [editingTemplateId, setEditingTemplateId] = useState<number | null>(null)
  const [formState, setFormState] = useState<ShiftTemplateFormState>(defaultFormState)

  const fetchFacilities = async () => {
    const response = await fetch(`${API_BASE}/facilities/`, { credentials: 'include' })
    if (!response.ok) {
      const errorMessage = await getApiErrorMessage(response)
      throw new Error(errorMessage ?? 'Unable to load facilities')
    }

    const data = await response.json()
    setFacilities(data)
  }

  const fetchTemplates = async () => {
    const response = await fetch(`${API_BASE}/shift-templates/`, { credentials: 'include' })
    if (!response.ok) {
      const errorMessage = await getApiErrorMessage(response)
      throw new Error(errorMessage ?? 'Unable to load shift templates')
    }

    const data = await response.json()
    const normalizedTemplates = data.map((template: ShiftTemplate) => ({
      ...template,
      active_days_of_week: Array.isArray(template.active_days_of_week) ? template.active_days_of_week : [],
      weekend_days: Array.isArray(template.weekend_days) ? template.weekend_days : [],
    }))
    setTemplates(normalizedTemplates)
  }

  useEffect(() => {
    const loadData = async () => {
      try {
        setIsLoading(true)
        setError(null)
        await Promise.all([fetchFacilities(), fetchTemplates()])
      } catch (loadError) {
        console.error(loadError)
        setError(loadError instanceof Error ? loadError.message : 'Unable to load shift builder data.')
      } finally {
        setIsLoading(false)
      }
    }

    loadData()
  }, [])

  const activeFacilities = useMemo(
    () => facilities.filter((facility) => facility.active),
    [facilities],
  )

  const sortedTemplates = useMemo(
    () => [...templates].sort((a, b) => {
      if (a.active !== b.active) {
        return a.active ? -1 : 1
      }
      if (a.facility_name !== b.facility_name) {
        return a.facility_name.localeCompare(b.facility_name)
      }
      return a.name.localeCompare(b.name)
    }),
    [templates],
  )

  const openCreateModal = () => {
    setEditingTemplateId(null)
    setFormState(defaultFormState)
    setIsModalOpen(true)
  }

  const openEditModal = (template: ShiftTemplate) => {
    setEditingTemplateId(template.id)
    setFormState({
      facility: String(template.facility),
      start_time: template.start_time.slice(0, 5),
      end_time: template.end_time.slice(0, 5),
      active_days_of_week: template.active_days_of_week,
      weekend_days: template.weekend_days,
      night_shift: template.night_shift,
      default_staffing_count: String(template.default_staffing_count),
      active: template.active,
    })
    setIsModalOpen(true)
  }

  const closeModal = () => {
    setIsModalOpen(false)
    setEditingTemplateId(null)
    setFormState(defaultFormState)
  }

  const toggleActiveDay = (day: DayOfWeek) => {
    setFormState((current) => {
      const selected = new Set(current.active_days_of_week)
      if (selected.has(day)) {
        selected.delete(day)
      } else {
        selected.add(day)
      }

      const nextDays = DAYS_OF_WEEK.filter((dayOfWeek) => selected.has(dayOfWeek))
      const nextWeekendDays = current.weekend_days.filter((weekendDay) => nextDays.includes(weekendDay))
      return {
        ...current,
        active_days_of_week: nextDays,
        weekend_days: nextWeekendDays,
      }
    })
  }

  const toggleWeekendDay = (day: WeekendDay) => {
    setFormState((current) => {
      if (!current.active_days_of_week.includes(day)) {
        return current
      }

      const selected = new Set(current.weekend_days)
      if (selected.has(day)) {
        selected.delete(day)
      } else {
        selected.add(day)
      }

      const nextWeekendDays = WEEKEND_DAY_OPTIONS.filter((weekendDay) => selected.has(weekendDay))
      return {
        ...current,
        weekend_days: nextWeekendDays,
      }
    })
  }

  const saveTemplate = async () => {
    const staffingCount = Number(formState.default_staffing_count)

    if (!formState.facility || !formState.start_time || !formState.end_time) {
      setError('Facility, start time, and end time are required.')
      return
    }

    if (!Number.isInteger(staffingCount) || staffingCount < 1) {
      setError('Required staffing must be a whole number of at least 1.')
      return
    }

    if (!formState.active_days_of_week.length) {
      setError('Select at least one active day.')
      return
    }

    const invalidWeekendDays = formState.weekend_days.filter((day) => !formState.active_days_of_week.includes(day))
    if (invalidWeekendDays.length) {
      setError('Weekend designation days must also be selected as active days.')
      return
    }

    try {
      setIsSaving(true)
      setError(null)

      const isEditing = editingTemplateId !== null
      const url = isEditing
        ? `${API_BASE}/shift-templates/${editingTemplateId}/`
        : `${API_BASE}/shift-templates/`
      const method = isEditing ? 'PATCH' : 'POST'

      const response = await fetch(url, {
        method,
        headers: {
          'Content-Type': 'application/json',
        },
        credentials: 'include',
        body: JSON.stringify({
          facility: Number(formState.facility),
          start_time: formState.start_time,
          end_time: formState.end_time,
          active_days_of_week: formState.active_days_of_week,
          weekend_days: formState.weekend_days,
          night_shift: formState.night_shift,
          default_staffing_count: staffingCount,
          active: formState.active,
        }),
      })

      if (!response.ok) {
        const errorMessage = await getApiErrorMessage(response)
        throw new Error(errorMessage ?? 'Unable to save shift template')
      }

      await fetchTemplates()
      closeModal()
    } catch (saveError) {
      console.error(saveError)
      setError(saveError instanceof Error ? saveError.message : 'Unable to save shift template changes.')
    } finally {
      setIsSaving(false)
    }
  }

  const disableTemplate = async (template: ShiftTemplate) => {
    if (!template.active) {
      return
    }

    const confirmed = window.confirm(`Disable template "${template.name}"?`)
    if (!confirmed) {
      return
    }

    try {
      setError(null)
      const response = await fetch(`${API_BASE}/shift-templates/${template.id}/`, {
        method: 'PATCH',
        headers: {
          'Content-Type': 'application/json',
        },
        credentials: 'include',
        body: JSON.stringify({ active: false }),
      })

      if (!response.ok) {
        const errorMessage = await getApiErrorMessage(response)
        throw new Error(errorMessage ?? 'Unable to disable shift template')
      }

      await fetchTemplates()
    } catch (disableError) {
      console.error(disableError)
      setError(disableError instanceof Error ? disableError.message : 'Unable to disable template right now.')
    }
  }

  if (isLoading) {
    return <div className="scheduler-loading">Loading shift builder...</div>
  }

  return (
    <div className="facilities-view-card">
      <div className="facilities-header">
        <h2>Shift Builder</h2>
        <button type="button" className="primary-action" onClick={openCreateModal}>
          Add Shift Template
        </button>
      </div>

      {error && <div className="facilities-error">{error}</div>}

      <div className="scheduler-table-wrap">
        <table className="scheduler-table">
          <thead>
            <tr>
              <th>Name</th>
              <th>Facility</th>
              <th>Start Time</th>
              <th>End Time</th>
              <th>Active Days</th>
              <th>Weekend</th>
              <th>Night</th>
              <th>Required Staffing</th>
              <th>Status</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {sortedTemplates.map((template) => (
              <tr key={template.id}>
                <td>{template.name}</td>
                <td>{template.facility_name}</td>
                <td>{formatTimeLabel(template.start_time)}</td>
                <td>{formatTimeLabel(template.end_time)}</td>
                <td>
                  <div className="active-days-list">
                    {template.active_days_of_week.map((day) => (
                      <span key={`${template.id}-${day}`} className="active-day-chip">
                        {day.slice(0, 3)}
                      </span>
                    ))}
                  </div>
                </td>
                <td>
                  {template.weekend_days.length ? template.weekend_days.join(', ') : '-'}
                </td>
                <td>{template.night_shift ? 'Yes' : 'No'}</td>
                <td>{template.default_staffing_count}</td>
                <td>{template.active ? 'Active' : 'Disabled'}</td>
                <td>
                  <div className="facility-actions">
                    <button type="button" onClick={() => openEditModal(template)}>
                      Edit
                    </button>
                    <button
                      type="button"
                      onClick={() => disableTemplate(template)}
                      disabled={!template.active}
                    >
                      Disable
                    </button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {!sortedTemplates.length && <div className="empty-state">No shift templates found</div>}

      {isModalOpen && (
        <div className="shift-modal-overlay" onClick={closeModal}>
          <div className="shift-modal shift-modal-wide" onClick={(event) => event.stopPropagation()}>
            <div className="shift-modal-header">
              <h2>{editingTemplateId ? 'Edit Shift Template' : 'Add Shift Template'}</h2>
            </div>

            <div className="shift-modal-body">
              <label className="facility-field">
                <span>Facility</span>
                <select
                  value={formState.facility}
                  onChange={(event) =>
                    setFormState((current) => ({ ...current, facility: event.target.value }))
                  }
                >
                  <option value="">Select facility</option>
                  {activeFacilities.map((facility) => (
                    <option key={facility.id} value={facility.id}>
                      {facility.name}
                    </option>
                  ))}
                </select>
              </label>

              <label className="facility-field">
                <span>Start Time</span>
                <input
                  type="time"
                  value={formState.start_time}
                  onChange={(event) =>
                    setFormState((current) => ({ ...current, start_time: event.target.value }))
                  }
                />
              </label>

              <label className="facility-field">
                <span>End Time</span>
                <input
                  type="time"
                  value={formState.end_time}
                  onChange={(event) =>
                    setFormState((current) => ({ ...current, end_time: event.target.value }))
                  }
                />
              </label>

              <fieldset className="days-fieldset">
                <legend>Active Days</legend>
                <div className="days-grid">
                  {DAYS_OF_WEEK.map((day) => (
                    <label key={day} className="day-option">
                      <input
                        type="checkbox"
                        checked={formState.active_days_of_week.includes(day)}
                        onChange={() => toggleActiveDay(day)}
                      />
                      <span>{day}</span>
                    </label>
                  ))}
                </div>
              </fieldset>

              <fieldset className="days-fieldset">
                <legend>Weekend Designation Days</legend>
                <div className="days-grid">
                  {WEEKEND_DAY_OPTIONS.map((day) => {
                    const activeDaySelected = formState.active_days_of_week.includes(day)
                    return (
                      <label key={day} className="day-option">
                        <input
                          type="checkbox"
                          checked={formState.weekend_days.includes(day)}
                          onChange={() => toggleWeekendDay(day)}
                          disabled={!activeDaySelected}
                        />
                        <span>{day}</span>
                      </label>
                    )
                  })}
                </div>
              </fieldset>

              <label className="facility-field inline-checkbox-field">
                <span>Night Shift</span>
                <input
                  type="checkbox"
                  checked={formState.night_shift}
                  onChange={(event) =>
                    setFormState((current) => ({ ...current, night_shift: event.target.checked }))
                  }
                />
              </label>

              <label className="facility-field">
                <span>Required Staffing</span>
                <input
                  type="number"
                  min={1}
                  step={1}
                  value={formState.default_staffing_count}
                  onChange={(event) =>
                    setFormState((current) => ({ ...current, default_staffing_count: event.target.value }))
                  }
                />
              </label>

              <label className="facility-field inline-checkbox-field">
                <span>Active</span>
                <input
                  type="checkbox"
                  checked={formState.active}
                  onChange={(event) =>
                    setFormState((current) => ({ ...current, active: event.target.checked }))
                  }
                />
              </label>
            </div>

            <div className="shift-modal-actions">
              <button className="secondary" type="button" onClick={closeModal}>
                Cancel
              </button>
              <button type="button" onClick={saveTemplate} disabled={isSaving}>
                {isSaving ? 'Saving...' : 'Save'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
