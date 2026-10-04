import React, { useEffect, useState } from 'react'

type Physician = {
  id: number
  first_name: string
  last_name: string
  display_name: string
  email: string
  phone_number: string
  current_contracts: Array<{
    id: number
    name: string
    domain: string
  }>
  role: string
  primary_facility: number | null
  primary_facility_name: string | null
  clinician_type: 'physician' | 'pa' | 'np'
  fte: string
  active: boolean
}

type FacilityOption = {
  id: number
  name: string
  active: boolean
}

type PhysicianFormState = {
  first_name: string
  last_name: string
  display_name: string
  email: string
  phone_number: string
  role: string
  primary_facility: string
  clinician_type: 'physician' | 'pa' | 'np'
  fte: string
  active: boolean
}

const API_BASE = 'http://localhost:8000/api'

const defaultFormState: PhysicianFormState = {
  first_name: '',
  last_name: '',
  display_name: '',
  email: '',
  phone_number: '',
  role: '',
  primary_facility: '',
  clinician_type: 'physician',
  fte: '1.00',
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

export default function PhysiciansView() {
  const [physicians, setPhysicians] = useState<Physician[]>([])
  const [facilities, setFacilities] = useState<FacilityOption[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [isSaving, setIsSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const [isModalOpen, setIsModalOpen] = useState(false)
  const [editingPhysicianId, setEditingPhysicianId] = useState<number | null>(null)
  const [formState, setFormState] = useState<PhysicianFormState>(defaultFormState)
  const activePhysicianCount = physicians.filter(
    (physician) => physician.active === true,
  ).length
  const sortedUsers = [...physicians].sort((left, right) => {
    const lastNameComparison = left.last_name.localeCompare(right.last_name, undefined, { sensitivity: 'base' })
    if (lastNameComparison !== 0) {
      return lastNameComparison
    }
    return left.first_name.localeCompare(right.first_name, undefined, { sensitivity: 'base' })
  })

  const fetchData = async () => {
    try {
      setIsLoading(true)
      setError(null)

      const [physiciansResponse, facilitiesResponse] = await Promise.all([
        fetch(`${API_BASE}/physicians/`, { credentials: 'include' }),
        fetch(`${API_BASE}/facilities/`, { credentials: 'include' }),
      ])

      if (!physiciansResponse.ok) {
        const errorMessage = await getApiErrorMessage(physiciansResponse)
        throw new Error(errorMessage ?? 'Unable to load users')
      }

      if (!facilitiesResponse.ok) {
        const errorMessage = await getApiErrorMessage(facilitiesResponse)
        throw new Error(errorMessage ?? 'Unable to load facilities')
      }

      const physiciansData = await physiciansResponse.json()
      const facilitiesData = await facilitiesResponse.json()

      setPhysicians(physiciansData)
      setFacilities(facilitiesData)
    } catch (fetchError) {
      console.error(fetchError)
      setError(fetchError instanceof Error ? fetchError.message : 'Unable to load user data right now.')
    } finally {
      setIsLoading(false)
    }
  }

  useEffect(() => {
    fetchData()
  }, [])

  const openCreateModal = () => {
    setEditingPhysicianId(null)
    setFormState(defaultFormState)
    setIsModalOpen(true)
  }

  const openEditModal = (physician: Physician) => {
    setEditingPhysicianId(physician.id)
    setFormState({
      first_name: physician.first_name,
      last_name: physician.last_name,
      display_name: physician.display_name,
      email: physician.email,
      phone_number: physician.phone_number,
      role: physician.role,
      primary_facility: physician.primary_facility ? String(physician.primary_facility) : '',
      clinician_type: physician.clinician_type,
      fte: physician.fte,
      active: physician.active,
    })
    setIsModalOpen(true)
  }

  const editingUser = editingPhysicianId === null
    ? null
    : physicians.find((physician) => physician.id === editingPhysicianId) ?? null
  const currentContractLabel = editingUser?.current_contracts.length
    ? editingUser.current_contracts.map((contract) => (
      editingUser.current_contracts.length === 1 ? contract.name : `${contract.domain}: ${contract.name}`
    )).join(', ')
    : 'Not assigned'
  const closeModal = () => {
    setIsModalOpen(false)
    setEditingPhysicianId(null)
    setFormState(defaultFormState)
  }

  const savePhysician = async () => {
    if (!formState.first_name.trim() || !formState.last_name.trim() || !formState.email.trim()) {
      setError('First name, last name, and email are required.')
      return
    }

    try {
      setIsSaving(true)
      setError(null)

      const isEditing = editingPhysicianId !== null
      const url = isEditing
        ? `${API_BASE}/physicians/${editingPhysicianId}/`
        : `${API_BASE}/physicians/`
      const method = isEditing ? 'PATCH' : 'POST'

      const response = await fetch(url, {
        method,
        headers: {
          'Content-Type': 'application/json',
        },
        credentials: 'include',
        body: JSON.stringify({
          first_name: formState.first_name.trim(),
          last_name: formState.last_name.trim(),
          display_name: formState.display_name.trim(),
          email: formState.email.trim(),
          phone_number: formState.phone_number.trim(),
          role: formState.role,
          primary_facility: formState.primary_facility ? Number(formState.primary_facility) : null,
          clinician_type: formState.clinician_type,
          fte: formState.fte,
          active: formState.active,
        }),
      })

      if (!response.ok) {
        const errorMessage = await getApiErrorMessage(response)
        throw new Error(errorMessage ?? 'Unable to save user')
      }

      await fetchData()
      closeModal()
    } catch (saveError) {
      console.error(saveError)
      setError(saveError instanceof Error ? saveError.message : 'Unable to save user profile changes.')
    } finally {
      setIsSaving(false)
    }
  }

  if (isLoading) {
    return <div className="scheduler-loading">Loading users...</div>
  }

  return (
    <div className="facilities-view-card">
      <div className="facilities-header">
        <div className="physician-management-heading">
          <h2>User Management</h2>
          <div className="physician-active-count" aria-label={`${activePhysicianCount} active users`}>
            <span>Total Users</span>
            <strong>{activePhysicianCount}</strong>
          </div>
        </div>
        <button type="button" className="primary-action" onClick={openCreateModal}>
          Add New User
        </button>
      </div>

      {error && <div className="facilities-error">{error}</div>}

      <div className="user-directory" aria-label="Users">
        {sortedUsers.map((physician) => (
          <button
            type="button"
            className={`user-name-button${physician.active ? '' : ' user-name-button-disabled'}`}
            key={physician.id}
            onClick={() => openEditModal(physician)}
          >
            {physician.first_name} {physician.last_name}
          </button>
        ))}
      </div>

      {!physicians.length && <div className="empty-state">No users found</div>}

      {isModalOpen && (
        <div className="shift-modal-overlay" onClick={closeModal}>
          <div className="shift-modal physician-profile-modal" onClick={(event) => event.stopPropagation()}>
            <div className="shift-modal-header">
              <h2>{editingPhysicianId ? 'User Profile' : 'Add New User'}</h2>
            </div>
            <div className="shift-modal-body user-profile-form">
              <label className="facility-field">
                <span>First Name</span>
                <input
                  type="text"
                  value={formState.first_name}
                  onChange={(event) =>
                    setFormState((current) => ({ ...current, first_name: event.target.value }))
                  }
                  placeholder="Ava"
                />
              </label>
              <label className="facility-field">
                <span>Last Name</span>
                <input
                  type="text"
                  value={formState.last_name}
                  onChange={(event) =>
                    setFormState((current) => ({ ...current, last_name: event.target.value }))
                  }
                  placeholder="Patel"
                />
              </label>
              <label className="facility-field">
                <span>Display Name</span>
                <input
                  type="text"
                  value={formState.display_name}
                  onChange={(event) =>
                    setFormState((current) => ({ ...current, display_name: event.target.value }))
                  }
                  placeholder="Dr. Patel"
                />
              </label>
              <label className="facility-field">
                <span>Email (required)</span>
                <input
                  type="email"
                  required
                  value={formState.email}
                  onChange={(event) =>
                    setFormState((current) => ({ ...current, email: event.target.value }))
                  }
                  placeholder="ava.patel@example.com"
                />
              </label>
              <label className="facility-field">
                <span>Phone (optional)</span>
                <input
                  type="tel"
                  value={formState.phone_number}
                  onChange={(event) =>
                    setFormState((current) => ({ ...current, phone_number: event.target.value }))
                  }
                  placeholder="(555) 555-0123"
                />
              </label>
              <label className="facility-field">
                <span>Current Contract</span>
                <input type="text" value={currentContractLabel} readOnly />
              </label>
              <label className="facility-field">
                <span>Role</span>
                <select
                  value={formState.role}
                  onChange={(event) =>
                    setFormState((current) => ({ ...current, role: event.target.value }))
                  }
                >
                  <option value="">Not assigned</option>
                  <option value="org_admin">Org Admin</option>
                  <option value="medical_director">Medical Director</option>
                  <option value="admin">Admin</option>
                  <option value="staff_physician">Staff Physician</option>
                  <option value="app">APP</option>
                  <option value="scheduler">Scheduler</option>
                </select>
              </label>
              <label className="facility-field">
                <span>Primary Facility</span>
                <select
                  value={formState.primary_facility}
                  onChange={(event) =>
                    setFormState((current) => ({ ...current, primary_facility: event.target.value }))
                  }
                >
                  <option value="">Unassigned</option>
                  {facilities.map((facility) => (
                    <option key={facility.id} value={facility.id}>
                      {facility.name} {facility.active ? '' : '(Disabled)'}
                    </option>
                  ))}
                </select>
              </label>
              <label className="facility-field">
                <span>Physician / PA / NP</span>
                <select
                  value={formState.clinician_type}
                  onChange={(event) =>
                    setFormState((current) => ({
                      ...current,
                      clinician_type: event.target.value as 'physician' | 'pa' | 'np',
                    }))
                  }
                >
                  <option value="physician">Physician</option>
                  <option value="pa">PA</option>
                  <option value="np">NP</option>
                </select>
              </label>
              <label className="facility-field">
                <span>Workload FTE</span>
                <input
                  type="number"
                  min="0"
                  max="3"
                  step="0.01"
                  list="workload-fte-options"
                  value={formState.fte}
                  onChange={(event) =>
                    setFormState((current) => ({ ...current, fte: event.target.value }))
                  }
                />
                <datalist id="workload-fte-options">
                  <option value="1.00" />
                  <option value="0.75" />
                  <option value="0.50" />
                  <option value="0.25" />
                </datalist>
                <small>1.00 = full time; 0.50 = half time. Used for per-FTE Schedule Block adjustments.</small>
              </label>
              <label className="facility-field physician-active-field">
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
              <button type="button" onClick={savePhysician} disabled={isSaving}>
                {isSaving ? 'Saving...' : 'Save'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
