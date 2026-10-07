import React from 'react'
import { readSessionNumber, writeSessionSelection } from '../utils/sessionSelection'

const API_BASE = 'http://localhost:8000/api'

type Organization = { id: number; name: string }
type Region = { id: number; organization: number; name: string; active: boolean }
type PermissionOption = { code: string; label: string }
type PermissionGroup = { name: string; permissions: PermissionOption[] }
type RoleTemplate = {
  id: number
  region: number
  region_name: string
  name: string
  system_key: string
  permissions: string[]
  active: boolean
  assigned_user_count: number
}

async function apiError(response: Response) {
  try {
    const data = await response.json()
    if (typeof data?.detail === 'string') return data.detail
    const first = Object.values(data ?? {}).flat().find((value) => typeof value === 'string')
    if (typeof first === 'string') return first
  } catch { /* fallback */ }
  return 'Unable to complete that change.'
}

export default function RolesView() {
  const [organizations, setOrganizations] = React.useState<Organization[]>([])
  const [regions, setRegions] = React.useState<Region[]>([])
  const [groups, setGroups] = React.useState<PermissionGroup[]>([])
  const [organizationId, setOrganizationId] = React.useState<number | null>(() => readSessionNumber('atlas.roles.organization'))
  const [regionId, setRegionId] = React.useState<number | null>(() => readSessionNumber('atlas.roles.region'))
  const [roles, setRoles] = React.useState<RoleTemplate[]>([])
  const [editing, setEditing] = React.useState<RoleTemplate | null>(null)
  const [isCreating, setIsCreating] = React.useState(false)
  const [name, setName] = React.useState('')
  const [permissions, setPermissions] = React.useState<Set<string>>(new Set())
  const [active, setActive] = React.useState(true)
  const [error, setError] = React.useState<string | null>(null)
  const [notice, setNotice] = React.useState<string | null>(null)
  const [saving, setSaving] = React.useState(false)

  const loadOrganizations = React.useCallback(async () => {
    const [organizationResponse, catalogResponse] = await Promise.all([
      fetch(`${API_BASE}/organizations/`, { credentials: 'include' }),
      fetch(`${API_BASE}/permissions/catalog/`, { credentials: 'include' }),
    ])
    if (!organizationResponse.ok) throw new Error(await apiError(organizationResponse))
    if (!catalogResponse.ok) throw new Error(await apiError(catalogResponse))
    const organizationData: Organization[] = await organizationResponse.json()
    setOrganizations(organizationData)
    setGroups(await catalogResponse.json())
    setOrganizationId((current) => organizationData.some((organization) => organization.id === current) ? current : organizationData[0]?.id ?? null)
  }, [])

  const loadRegions = React.useCallback(async (nextOrganizationId: number) => {
    const response = await fetch(`${API_BASE}/organizations/${nextOrganizationId}/regions/`, { credentials: 'include' })
    if (!response.ok) throw new Error(await apiError(response))
    const data: Region[] = await response.json()
    setRegions(data)
    setRegionId((current) => data.some((region) => region.id === current) ? current : data[0]?.id ?? null)
  }, [])

  const loadRoles = React.useCallback(async (nextRegionId: number) => {
    const response = await fetch(`${API_BASE}/regions/${nextRegionId}/roles/`, { credentials: 'include' })
    if (!response.ok) throw new Error(await apiError(response))
    setRoles(await response.json())
  }, [])

  React.useEffect(() => { loadOrganizations().catch((e) => setError(e.message)) }, [loadOrganizations])
  React.useEffect(() => {
    if (organizationId !== null) loadRegions(organizationId).catch((e) => setError(e.message))
  }, [organizationId, loadRegions])
  React.useEffect(() => {
    if (regionId !== null) loadRoles(regionId).catch((e) => setError(e.message))
  }, [regionId, loadRoles])
  React.useEffect(() => writeSessionSelection('atlas.roles.organization', organizationId), [organizationId])
  React.useEffect(() => writeSessionSelection('atlas.roles.region', regionId), [regionId])

  const openCreate = () => {
    setEditing(null); setIsCreating(true); setName(''); setPermissions(new Set()); setActive(true); setError(null)
  }
  const openEdit = (role: RoleTemplate) => {
    setEditing(role); setIsCreating(false); setName(role.name); setPermissions(new Set(role.permissions)); setActive(role.active); setError(null)
  }
  const closeModal = () => { setEditing(null); setIsCreating(false) }
  const togglePermission = (code: string) => setPermissions((current) => {
    const next = new Set(current)
    if (next.has(code)) next.delete(code); else next.add(code)
    return next
  })
  const save = async () => {
    if (regionId === null || !name.trim()) return
    if (editing?.assigned_user_count && !window.confirm(`This change affects ${editing.assigned_user_count} assigned user${editing.assigned_user_count === 1 ? '' : 's'}. Continue?`)) return
    setSaving(true); setError(null); setNotice(null)
    try {
      const response = await fetch(
        editing ? `${API_BASE}/roles/${editing.id}/` : `${API_BASE}/regions/${regionId}/roles/`,
        {
          method: editing ? 'PATCH' : 'POST', credentials: 'include',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: name.trim(), permissions: [...permissions], active }),
        },
      )
      if (!response.ok) throw new Error(await apiError(response))
      await loadRoles(regionId)
      closeModal()
      setNotice(editing ? 'Role updated.' : 'Role created.')
    } catch (e) { setError(e instanceof Error ? e.message : 'Unable to save Role.') }
    finally { setSaving(false) }
  }
  const remove = async (role: RoleTemplate) => {
    if (!window.confirm(`Delete ${role.name}? This cannot be undone.`)) return
    const response = await fetch(`${API_BASE}/roles/${role.id}/`, { method: 'DELETE', credentials: 'include' })
    if (!response.ok) { setError(await apiError(response)); return }
    if (regionId !== null) await loadRoles(regionId)
    setNotice('Role deleted.')
  }

  return <div className="facilities-view-card roles-view">
    <div className="facilities-header roles-toolbar">
      <div><h2>Roles</h2><p>Regional templates assigned independently within each Domain.</p></div>
      <div className="roles-scope-controls">
        {organizations.length > 1 ? <label><span>Organization</span><select value={organizationId ?? ''} onChange={(e) => { setOrganizationId(Number(e.target.value)); setRegionId(null) }}>{organizations.map((organization) => <option key={organization.id} value={organization.id}>{organization.name}</option>)}</select></label> : <div className="roles-single-region"><span>Organization</span><strong>{organizations[0]?.name ?? '—'}</strong></div>}
        {regions.length > 1 ? <label><span>Region</span><select value={regionId ?? ''} onChange={(e) => setRegionId(Number(e.target.value))}>{regions.map((region) => <option key={region.id} value={region.id}>{region.name}</option>)}</select></label> : <div className="roles-single-region"><span>Region</span><strong>{regions[0]?.name ?? '—'}</strong></div>}
        <button type="button" className="primary-action" onClick={openCreate}>Add Role</button>
      </div>
    </div>
    {error && <div className="facilities-error">{error}</div>}
    {notice && <div className="organization-notice">{notice}</div>}
    <div className="roles-table">
      <div className="roles-table-header"><span>Role</span><span>Users</span><span>Status</span><span>Actions</span></div>
      {roles.map((role) => <div className={`roles-table-row${role.active ? '' : ' roles-table-row-inactive'}`} key={role.id}>
        <div><strong>{role.name}</strong>{role.system_key && <small>Default template</small>}</div>
        <span>{role.assigned_user_count}</span>
        <span>{role.active ? 'Active' : 'Inactive'}</span>
        <div className="facility-actions"><button type="button" onClick={() => openEdit(role)}>Edit</button>{!role.system_key && <button type="button" className="danger" onClick={() => remove(role)} disabled={role.assigned_user_count > 0}>Delete</button>}</div>
      </div>)}
    </div>
    {(editing || isCreating) && <div className="shift-modal-overlay" onClick={closeModal}>
      <div className="shift-modal role-modal" onClick={(e) => e.stopPropagation()}>
        <div className="shift-modal-header"><h2>{editing ? `Edit ${editing.name}` : 'Add Role'}</h2></div>
        <div className="shift-modal-body">
          <label className="facility-field"><span>Role name</span><input value={name} onChange={(e) => setName(e.target.value)} /></label>
          <label className="role-active-toggle"><input type="checkbox" checked={active} onChange={(e) => setActive(e.target.checked)} /><span>Active</span></label>
          <div className="role-permission-groups">{groups.map((group) => <section key={group.name}>
            <div className="role-permission-group-header"><strong>{group.name}</strong><button type="button" onClick={() => setPermissions((current) => { const next = new Set(current); const allSelected = group.permissions.every((permission) => next.has(permission.code)); group.permissions.forEach((permission) => allSelected ? next.delete(permission.code) : next.add(permission.code)); return next })}>Toggle all</button></div>
            <div className="role-permission-grid">{group.permissions.map((permission) => <label key={permission.code}><input type="checkbox" checked={permissions.has(permission.code)} onChange={() => togglePermission(permission.code)} /><span>{permission.label}</span></label>)}</div>
          </section>)}</div>
        </div>
        <div className="shift-modal-actions"><button type="button" className="secondary" onClick={closeModal}>Cancel</button><button type="button" onClick={save} disabled={saving || !name.trim()}>{saving ? 'Saving...' : 'Save Role'}</button></div>
      </div>
    </div>}
  </div>
}
