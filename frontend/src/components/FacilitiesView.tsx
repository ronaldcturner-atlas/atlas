import React, { useEffect, useState } from 'react'

type Facility = {
  id: number
  name: string
  timezone: string
  color: string
  active: boolean
}

type FacilityFormState = {
  name: string
  timezone: string
  color: string
}

type FacilitiesViewProps = {
  onFacilitiesChanged: () => void
}

const API_BASE = 'http://localhost:8000/api'

const defaultFormState: FacilityFormState = {
  name: '',
  timezone: 'UTC',
  color: '#2563eb',
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

export default function FacilitiesView({ onFacilitiesChanged }: FacilitiesViewProps) {
  const [facilities, setFacilities] = useState<Facility[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [isSaving, setIsSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const [isModalOpen, setIsModalOpen] = useState(false)
  const [editingFacilityId, setEditingFacilityId] = useState<number | null>(null)
  const [formState, setFormState] = useState<FacilityFormState>(defaultFormState)

  const fetchFacilities = async () => {
    try {
      setIsLoading(true)
      setError(null)
      const response = await fetch(`${API_BASE}/facilities/`, {
        credentials: 'include',
      })

      if (!response.ok) {
        const errorMessage = await getApiErrorMessage(response)
        throw new Error(errorMessage ?? 'Unable to load facilities')
      }

      const data = await response.json()
      setFacilities(data)
    } catch (fetchError) {
      console.error(fetchError)
      setError(fetchError instanceof Error ? fetchError.message : 'Unable to load facilities right now.')
    } finally {
      setIsLoading(false)
    }
  }

  useEffect(() => {
    fetchFacilities()
  }, [])

  const openCreateModal = () => {
    setEditingFacilityId(null)
    setFormState(defaultFormState)
    setIsModalOpen(true)
  }

  const openEditModal = (facility: Facility) => {
    setEditingFacilityId(facility.id)
    setFormState({
      name: facility.name,
      timezone: facility.timezone,
      color: facility.color,
    })
    setIsModalOpen(true)
  }

  const closeModal = () => {
    setIsModalOpen(false)
    setEditingFacilityId(null)
    setFormState(defaultFormState)
  }

  const saveFacility = async () => {
    if (!formState.name.trim()) {
      setError('Facility name is required.')
      return
    }

    try {
      setIsSaving(true)
      setError(null)

      const isEditing = editingFacilityId !== null
      const url = isEditing
        ? `${API_BASE}/facilities/${editingFacilityId}/`
        : `${API_BASE}/facilities/`
      const method = isEditing ? 'PATCH' : 'POST'

      const response = await fetch(url, {
        method,
        headers: {
          'Content-Type': 'application/json',
        },
        credentials: 'include',
        body: JSON.stringify({
          name: formState.name.trim(),
          timezone: formState.timezone.trim() || 'UTC',
          color: formState.color,
        }),
      })

      if (!response.ok) {
        const errorMessage = await getApiErrorMessage(response)
        throw new Error(errorMessage ?? 'Unable to save facility')
      }

      await fetchFacilities()
      onFacilitiesChanged()
      closeModal()
    } catch (saveError) {
      console.error(saveError)
      setError(saveError instanceof Error ? saveError.message : 'Unable to save facility changes.')
    } finally {
      setIsSaving(false)
    }
  }

  const disableFacility = async (facility: Facility) => {
    try {
      setError(null)
      const response = await fetch(`${API_BASE}/facilities/${facility.id}/disable/`, {
        method: 'POST',
        credentials: 'include',
      })

      if (!response.ok) {
        const errorMessage = await getApiErrorMessage(response)
        throw new Error(errorMessage ?? 'Unable to disable facility')
      }

      await fetchFacilities()
      onFacilitiesChanged()
    } catch (disableError) {
      console.error(disableError)
      setError(disableError instanceof Error ? disableError.message : `Unable to disable ${facility.name}.`)
    }
  }

  if (isLoading) {
    return <div className="scheduler-loading">Loading facilities...</div>
  }

  return (
    <div className="facilities-view-card">
      <div className="facilities-header">
        <h2>Facility Management</h2>
        <button type="button" className="primary-action" onClick={openCreateModal}>
          Add New Facility
        </button>
      </div>

      {error && <div className="facilities-error">{error}</div>}

      <div className="scheduler-table-wrap">
        <table className="scheduler-table">
          <thead>
            <tr>
              <th>Name</th>
              <th>Timezone</th>
              <th>Color</th>
              <th>Status</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {facilities.map((facility) => (
              <tr key={facility.id}>
                <td>{facility.name}</td>
                <td>{facility.timezone}</td>
                <td>
                  <span className="facility-color-cell">
                    <span
                      className="facility-color-swatch"
                      style={{ backgroundColor: facility.color }}
                      aria-hidden="true"
                    />
                    {facility.color}
                  </span>
                </td>
                <td>{facility.active ? 'Active' : 'Disabled'}</td>
                <td>
                  <div className="facility-actions">
                    <button type="button" onClick={() => openEditModal(facility)}>
                      Edit
                    </button>
                    {facility.active && (
                      <button type="button" onClick={() => disableFacility(facility)}>
                        Disable
                      </button>
                    )}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {!facilities.length && <div className="empty-state">No facilities found</div>}

      {isModalOpen && (
        <div className="shift-modal-overlay" onClick={closeModal}>
          <div className="shift-modal" onClick={(event) => event.stopPropagation()}>
            <div className="shift-modal-header">
              <h2>{editingFacilityId ? 'Edit Facility' : 'Add New Facility'}</h2>
            </div>
            <div className="shift-modal-body">
              <label className="facility-field">
                <span>Name</span>
                <input
                  type="text"
                  value={formState.name}
                  onChange={(event) =>
                    setFormState((current) => ({ ...current, name: event.target.value }))
                  }
                  placeholder="Downtown Medical Center"
                />
              </label>
              <label className="facility-field">
                <span>Timezone</span>
                <input
                  type="text"
                  value={formState.timezone}
                  onChange={(event) =>
                    setFormState((current) => ({ ...current, timezone: event.target.value }))
                  }
                  placeholder="UTC"
                />
              </label>
              <label className="facility-field">
                <span>Color</span>
                <input
                  type="color"
                  value={formState.color}
                  onChange={(event) =>
                    setFormState((current) => ({ ...current, color: event.target.value }))
                  }
                />
              </label>
            </div>
            <div className="shift-modal-actions">
              <button className="secondary" type="button" onClick={closeModal}>
                Cancel
              </button>
              <button type="button" onClick={saveFacility} disabled={isSaving}>
                {isSaving ? 'Saving...' : 'Save'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
