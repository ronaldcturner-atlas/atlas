import React, { useEffect, useState } from 'react'

type Physician = {
  id: number
  user_id: number
  first_name: string
  last_name: string
  display_name: string
  email: string
  phone_number: string
  current_contracts: Array<{
    id: number
    name: string
    domain_id: number
    domain: string
  }>
  domain_memberships: Array<{
    id: number
    domain_id: number
    domain_name: string
    organization_id: number
    region_id: number
    region_name: string
    role: string
  }>
  organization_memberships: Array<{
    id: number
    organization_id: number
    organization_name: string
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

type DomainOption = {
  id: number
  region: number
  region_name: string
  organization: number
  organization_name: string
  name: string
  active: boolean
}

type OrganizationOption = {
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
const DOMAIN_ROLE_OPTIONS = [
  ['org_admin', 'Org Admin'],
  ['medical_director', 'Medical Director'],
  ['admin', 'Admin'],
  ['staff_physician', 'Staff Physician'],
  ['app', 'APP'],
  ['scheduler', 'Scheduler'],
  ['view_only', 'View Only'],
] as const

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
  const [domains, setDomains] = useState<DomainOption[]>([])
  const [organizations, setOrganizations] = useState<OrganizationOption[]>([])
  const [selectedOrganizationId, setSelectedOrganizationId] = useState<number | null>(null)
  const [selectedRegionId, setSelectedRegionId] = useState<number | null>(null)
  const [selectedDomainId, setSelectedDomainId] = useState<number | 'all'>('all')
  const [isLoading, setIsLoading] = useState(true)
  const [isSaving, setIsSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const [isModalOpen, setIsModalOpen] = useState(false)
  const [editingPhysicianId, setEditingPhysicianId] = useState<number | null>(null)
  const [formState, setFormState] = useState<PhysicianFormState>(defaultFormState)
  const [domainRoles, setDomainRoles] = useState<Record<number, string>>({})
  const selectedOrganizationDomains = domains.filter(
    (domain) => domain.organization === selectedOrganizationId,
  )
  const regions = Array.from(new Map(
    selectedOrganizationDomains.map((domain) => [domain.region, {
      id: domain.region,
      name: domain.region_name,
    }]),
  ).values())
  const selectedRegionDomains = selectedOrganizationDomains.filter(
    (domain) => domain.region === selectedRegionId,
  )
  const filteredUsers = physicians.filter((physician) => physician.domain_memberships.some((membership) => (
    membership.organization_id === selectedOrganizationId
    && membership.region_id === selectedRegionId
    && membership.role !== 'view_only'
    && (selectedDomainId === 'all' || membership.domain_id === selectedDomainId)
  )))
  const activePhysicianCount = filteredUsers.filter((physician) => physician.active).length
  const sortedUsers = [...filteredUsers].sort((left, right) => {
    const lastNameComparison = left.last_name.localeCompare(right.last_name, undefined, { sensitivity: 'base' })
    if (lastNameComparison !== 0) {
      return lastNameComparison
    }
    return left.first_name.localeCompare(right.first_name, undefined, { sensitivity: 'base' })
  })
  const editingUser = editingPhysicianId === null
    ? null
    : physicians.find((physician) => physician.id === editingPhysicianId) ?? null

  const fetchData = async () => {
    try {
      setIsLoading(true)
      setError(null)

      const [physiciansResponse, facilitiesResponse, organizationsResponse] = await Promise.all([
        fetch(`${API_BASE}/physicians/`, { credentials: 'include' }),
        fetch(`${API_BASE}/facilities/`, { credentials: 'include' }),
        fetch(`${API_BASE}/organizations/`, { credentials: 'include' }),
      ])

      if (!physiciansResponse.ok) {
        const errorMessage = await getApiErrorMessage(physiciansResponse)
        throw new Error(errorMessage ?? 'Unable to load users')
      }

      if (!facilitiesResponse.ok) {
        const errorMessage = await getApiErrorMessage(facilitiesResponse)
        throw new Error(errorMessage ?? 'Unable to load facilities')
      }

      if (!organizationsResponse.ok) {
        const errorMessage = await getApiErrorMessage(organizationsResponse)
        throw new Error(errorMessage ?? 'Unable to load organizations')
      }

      const physiciansData = await physiciansResponse.json()
      const facilitiesData = await facilitiesResponse.json()
      const organizationsData: OrganizationOption[] = await organizationsResponse.json()
      const domainResponses = await Promise.all(organizationsData.map((organization) => (
        fetch(`${API_BASE}/domains/?organization=${organization.id}`, { credentials: 'include' })
      )))
      const failedDomainResponse = domainResponses.find((response) => !response.ok)
      if (failedDomainResponse) {
        throw new Error(await getApiErrorMessage(failedDomainResponse) ?? 'Unable to load domains')
      }
      const domainsData = (await Promise.all(domainResponses.map((response) => response.json()))).flat()

      setPhysicians(physiciansData)
      setFacilities(facilitiesData)
      setOrganizations(organizationsData)
      setDomains(domainsData)
      setSelectedOrganizationId((current) => (
        organizationsData.some((organization) => organization.id === current)
          ? current
          : organizationsData[0]?.id ?? null
      ))
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

  useEffect(() => {
    const availableRegions = Array.from(new Set(
      domains.filter((domain) => domain.organization === selectedOrganizationId).map((domain) => domain.region),
    ))
    setSelectedRegionId((current) => availableRegions.includes(current ?? -1) ? current : availableRegions[0] ?? null)
    setSelectedDomainId('all')
  }, [domains, selectedOrganizationId])

  useEffect(() => {
    if (selectedDomainId === 'all') return
    if (!selectedRegionDomains.some((domain) => domain.id === selectedDomainId)) {
      setSelectedDomainId('all')
    }
  }, [selectedRegionId, selectedDomainId, selectedRegionDomains])

  const openCreateModal = () => {
    setEditingPhysicianId(null)
    setFormState(defaultFormState)
    setDomainRoles({})
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
    setDomainRoles(Object.fromEntries(
      physician.domain_memberships.map((membership) => [membership.domain_id, membership.role]),
    ))
    setIsModalOpen(true)
  }

  const closeModal = () => {
    setIsModalOpen(false)
    setEditingPhysicianId(null)
    setFormState(defaultFormState)
    setDomainRoles({})
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


      const savedUser: Physician = await response.json()
      const requiredOrganizationIds = new Set(
        domains.filter((domain) => Boolean(domainRoles[domain.id])).map((domain) => domain.organization),
      )
      for (const organizationId of requiredOrganizationIds) {
        const organizationMembership = savedUser.organization_memberships.find(
          (membership) => membership.organization_id === organizationId,
        )
        if (organizationMembership) continue
        const createOrganizationMembership = await fetch(`${API_BASE}/organizations/${organizationId}/memberships/`, {
          method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ user: savedUser.user_id }),
        })
        if (!createOrganizationMembership.ok) {
          throw new Error(await getApiErrorMessage(createOrganizationMembership) ?? 'Unable to add the user to the organization.')
        }
      }
      const existingMemberships = new Map(
        savedUser.domain_memberships.map((membership) => [membership.domain_id, membership]),
      )
      for (const domain of domains) {
        const nextRole = domainRoles[domain.id] ?? ''
        const membership = existingMemberships.get(domain.id)
        let membershipResponse: Response | null = null
        if (membership && !nextRole) {
          membershipResponse = await fetch(`${API_BASE}/domain-memberships/${membership.id}/`, {
            method: 'DELETE', credentials: 'include',
          })
        } else if (membership && nextRole !== membership.role) {
          membershipResponse = await fetch(`${API_BASE}/domain-memberships/${membership.id}/`, {
            method: 'PATCH', credentials: 'include', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ role: nextRole }),
          })
        } else if (!membership && nextRole) {
          membershipResponse = await fetch(`${API_BASE}/domains/${domain.id}/memberships/`, {
            method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ user: savedUser.user_id, role: nextRole }),
          })
        }
        if (membershipResponse && !membershipResponse.ok) {
          throw new Error(await getApiErrorMessage(membershipResponse) ?? `Unable to save ${domain.name} access.`)
        }
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
          {regions.length > 1 && (
            <label className="user-filter-select">
              <span>Region</span>
              <select
                value={selectedRegionId ?? ''}
                onChange={(event) => {
                  setSelectedRegionId(Number(event.target.value))
                  setSelectedDomainId('all')
                }}
              >
                {regions.map((region) => <option key={region.id} value={region.id}>{region.name}</option>)}
              </select>
            </label>
          )}
          {organizations.length > 1 && (
            <label className="user-filter-select">
              <span>Organization</span>
              <select
                value={selectedOrganizationId ?? ''}
                onChange={(event) => setSelectedOrganizationId(Number(event.target.value))}
              >
                {organizations.map((organization) => <option key={organization.id} value={organization.id}>{organization.name}</option>)}
              </select>
            </label>
          )}
          <div className="user-domain-filters" aria-label="Filter users by domain">
            <button
              type="button"
              className={selectedDomainId === 'all' ? 'active' : ''}
              onClick={() => setSelectedDomainId('all')}
            >
              View All
            </button>
            {selectedRegionDomains.map((domain) => (
              <button
                type="button"
                className={selectedDomainId === domain.id ? 'active' : ''}
                key={domain.id}
                onClick={() => setSelectedDomainId(domain.id)}
              >
                {domain.name}
              </button>
            ))}
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

      {!filteredUsers.length && <div className="empty-state">No users found for this selection</div>}

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
              <div className="user-domain-access">
                {organizations.map((organization) => {
                  const organizationDomains = domains.filter((domain) => domain.organization === organization.id)
                  const organizationRegions = Array.from(new Map(organizationDomains.map((domain) => [
                    domain.region, { id: domain.region, name: domain.region_name },
                  ])).values())
                  if (!organizationDomains.length) return null
                  return <div className="user-domain-access-group" key={organization.id}>
                    <div className="user-domain-access-header">
                      <strong>{organization.name}</strong>
                      <span>Domain Access</span>
                    </div>
                    {organizationRegions.map((region) => <React.Fragment key={region.id}>
                      {organizationRegions.length > 1 && <div className="user-domain-region-label">{region.name}</div>}
                      {organizationDomains.filter((domain) => domain.region === region.id).map((domain) => {
                        const currentContract = editingUser?.current_contracts.find((contract) => contract.domain_id === domain.id)
                        return <div className="user-domain-access-row" key={domain.id}>
                          <span>{domain.name}{domain.active ? '' : ' (Inactive)'}</span>
                          <select value={domainRoles[domain.id] ?? ''} onChange={(event) => setDomainRoles((current) => ({ ...current, [domain.id]: event.target.value }))}>
                            <option value="">No access</option>
                            {DOMAIN_ROLE_OPTIONS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
                          </select>
                          <span className="user-domain-contract">{currentContract?.name ?? 'No contract'}</span>
                        </div>
                      })}
                    </React.Fragment>)}
                  </div>
                })}
              </div>
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
