import React from 'react'
import { readSessionNumber, writeSessionSelection } from '../utils/sessionSelection'
import { API_BASE } from '../api'

type DomainOption = { id: number; name: string; region_id: number; region_name: string }
type DirectoryProfile = {
  id: number
  name: string
  display_name: string
  email: string | null
  phone_number: string | null
  clinician_type: string
  primary_facility: string | null
  role_name: string | null
  clinically_active: boolean
  contract: string | null
  is_org_admin: boolean
  domain_access?: Array<{
    domain_id: number
    domain_name: string
    region_name: string
    role_name: string
    clinically_active: boolean
  }>
}

type DirectoryResponse = {
  self_profile: DirectoryProfile
  domains: DomainOption[]
  selected_domain_id: number | null
  users: DirectoryProfile[]
}


export default function UserDirectoryView() {
  const [data, setData] = React.useState<DirectoryResponse | null>(null)
  const [domainId, setDomainId] = React.useState<number | ''>(() => readSessionNumber('atlas.user-directory.domain') ?? '')
  const [selectedUser, setSelectedUser] = React.useState<DirectoryProfile | null>(null)
  const [error, setError] = React.useState('')
  const [loading, setLoading] = React.useState(true)

  const load = React.useCallback(async (nextDomainId?: number | '') => {
    setLoading(true)
    setError('')
    try {
      const query = nextDomainId ? `?domain=${nextDomainId}` : ''
      let response = await fetch(`${API_BASE}/user-directory/${query}`, { credentials: 'include' })
      if (!response.ok && nextDomainId) {
        response = await fetch(`${API_BASE}/user-directory/`, { credentials: 'include' })
      }
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(body.detail || 'Unable to load users.')
      }
      const body: DirectoryResponse = await response.json()
      setData(body)
      setDomainId(body.selected_domain_id ?? '')
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : 'Unable to load users.')
    } finally {
      setLoading(false)
    }
  }, [])

  React.useEffect(() => { void load(domainId) }, [load])
  React.useEffect(() => writeSessionSelection('atlas.user-directory.domain', domainId), [domainId])

  if (loading && !data) return <div className="scheduler-loading">Loading users...</div>
  if (!data) return <div className="facilities-error">{error || 'Unable to load users.'}</div>

  const profile = data.self_profile
  return <div className="user-directory-view">
    {error && <div className="facilities-error">{error}</div>}
    <section className="user-self-profile">
      <div className="user-self-heading"><div><span>My Profile</span><h2>{profile.name}{profile.is_org_admin && <small className="org-admin-badge">Org Admin</small>}</h2></div><span className="user-profile-type">{profile.clinician_type}</span></div>
      <div className="user-self-details">
        <div><span>Email</span><strong>{profile.email || '—'}</strong></div>
        <div><span>Phone</span><strong>{profile.phone_number || 'Not provided'}</strong></div>
        <div><span>Primary facility</span><strong>{profile.primary_facility || 'Unassigned'}</strong></div>
      </div>
      {!!profile.domain_access?.length && <div className="user-self-access">{profile.domain_access.map((access) => <div key={access.domain_id}><strong>{access.region_name} / {access.domain_name}</strong><span>{access.role_name} · {access.clinically_active ? 'Clinically active' : 'View only'}</span></div>)}</div>}
    </section>

    <section className="user-directory-card">
      <div className="user-directory-heading">
        <div><h2>Users</h2><p>{data.domains.length ? 'Users in your selected Domain.' : 'Your role does not include access to a Domain user directory.'}</p></div>
        {data.domains.length > 1 && <label><span>Domain</span><select value={domainId} onChange={(event) => { const next = Number(event.target.value); setDomainId(next); void load(next) }}>{data.domains.map((domain) => <option key={domain.id} value={domain.id}>{domain.region_name} / {domain.name}</option>)}</select></label>}
        {data.domains.length === 1 && <div className="user-directory-domain"><span>Domain</span><strong>{data.domains[0].region_name} / {data.domains[0].name}</strong></div>}
      </div>
      {!!data.users.length && <div className="user-directory-table">
        <div className="user-directory-row user-directory-table-head"><span>Name</span><span>Role</span><span>Phone</span><span>Email</span></div>
        {data.users.map((user) => <button type="button" className="user-directory-row" key={user.id} onClick={() => setSelectedUser(user)}>
          <strong>{user.name}{user.is_org_admin && <small className="org-admin-badge">Org Admin</small>}</strong><span>{user.role_name || '—'}</span><span>{user.phone_number || '—'}</span><span>{user.email || '—'}</span>
        </button>)}
      </div>}
    </section>

    {selectedUser && <div className="shift-modal-overlay" onClick={() => setSelectedUser(null)}><div className="shift-modal user-directory-modal" onClick={(event) => event.stopPropagation()}>
      <div className="shift-modal-header"><h2>{selectedUser.name}</h2></div>
      <div className="shift-modal-body">
        {selectedUser.is_org_admin && <div className="org-admin-profile-notice">Organization-wide Org Admin</div>}
        <div className="user-directory-profile-grid"><div><span>Domain role</span><strong>{selectedUser.role_name || '—'}</strong></div><div><span>Clinical status</span><strong>{selectedUser.clinically_active ? 'Clinically active' : 'Not clinically active'}</strong></div><div><span>Email</span><strong>{selectedUser.email || 'Not available'}</strong></div><div><span>Phone</span><strong>{selectedUser.phone_number || 'Not available'}</strong></div><div><span>Contract</span><strong>{selectedUser.contract || 'No contract'}</strong></div><div><span>Primary facility</span><strong>{selectedUser.primary_facility || 'Unassigned'}</strong></div></div>
      </div>
      <div className="shift-modal-actions"><button type="button" onClick={() => setSelectedUser(null)}>Close</button></div>
    </div></div>}
  </div>
}
