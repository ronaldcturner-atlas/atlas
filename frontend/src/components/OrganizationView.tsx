import React, { useEffect, useState } from 'react'
import { readSessionNumber, writeSessionSelection } from '../utils/sessionSelection'
import { API_BASE } from '../api'

type Organization = { id: number; name: string; active: boolean; region_count: number; domain_count: number }
type Region = { id: number; organization: number; name: string; active: boolean; domain_count: number }
type Domain = { id: number; region: number; region_name: string; organization: number; name: string; active: boolean; membership_count: number }
type OrganizationMembership = { id: number; user: number; user_name: string; user_email: string; is_org_admin: boolean; active: boolean }
type OrganizationAdminData = { admins: OrganizationMembership[]; candidates: OrganizationMembership[] }

async function apiError(response: Response) {
  try {
    const data = await response.json()
    if (typeof data?.detail === 'string') return data.detail
    const first = Object.values(data ?? {}).flat().find((value) => typeof value === 'string')
    if (typeof first === 'string') return first
  } catch { /* use fallback */ }
  return 'Unable to complete that change.'
}

export default function OrganizationView() {
  const [organizations, setOrganizations] = useState<Organization[]>([])
  const [selectedOrganizationId, setSelectedOrganizationId] = useState<number | null>(() => readSessionNumber('atlas.organization.organization'))
  const [organizationName, setOrganizationName] = useState('')
  const [regions, setRegions] = useState<Region[]>([])
  const [domains, setDomains] = useState<Domain[]>([])
  const [regionNames, setRegionNames] = useState<Record<number, string>>({})
  const [domainNames, setDomainNames] = useState<Record<number, string>>({})
  const [newRegionName, setNewRegionName] = useState('')
  const [newDomainNames, setNewDomainNames] = useState<Record<number, string>>({})
  const [organizationAdmins, setOrganizationAdmins] = useState<OrganizationAdminData>({ admins: [], candidates: [] })
  const [newOrgAdminUserId, setNewOrgAdminUserId] = useState<number | ''>('')
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [isSaving, setIsSaving] = useState(false)

  const loadOrganizations = async () => {
    const response = await fetch(`${API_BASE}/organizations/`, { credentials: 'include' })
    if (!response.ok) throw new Error(await apiError(response))
    const data: Organization[] = await response.json()
    setOrganizations(data)
    setSelectedOrganizationId((current) => data.some((organization) => organization.id === current) ? current : data[0]?.id ?? null)
  }

  const loadHierarchy = async (organizationId: number) => {
    const [regionResponse, domainResponse, adminResponse] = await Promise.all([
      fetch(`${API_BASE}/organizations/${organizationId}/regions/`, { credentials: 'include' }),
      fetch(`${API_BASE}/domains/?organization=${organizationId}`, { credentials: 'include' }),
      fetch(`${API_BASE}/organizations/${organizationId}/org-admins/`, { credentials: 'include' }),
    ])
    if (!regionResponse.ok) throw new Error(await apiError(regionResponse))
    if (!domainResponse.ok) throw new Error(await apiError(domainResponse))
    if (!adminResponse.ok) throw new Error(await apiError(adminResponse))
    const nextRegions: Region[] = await regionResponse.json()
    const nextDomains: Domain[] = await domainResponse.json()
    const nextAdmins: OrganizationAdminData = await adminResponse.json()
    setRegions(nextRegions); setDomains(nextDomains)
    setRegionNames(Object.fromEntries(nextRegions.map((region) => [region.id, region.name])))
    setDomainNames(Object.fromEntries(nextDomains.map((domain) => [domain.id, domain.name])))
    setOrganizationAdmins(nextAdmins)
    setNewOrgAdminUserId((current) => nextAdmins.candidates.some((candidate) => candidate.user === current) ? current : '')
  }

  useEffect(() => { loadOrganizations().catch((e) => setError(e.message)) }, [])
  useEffect(() => {
    if (selectedOrganizationId === null || !organizations.some((item) => item.id === selectedOrganizationId)) return
    setOrganizationName(organizations.find((item) => item.id === selectedOrganizationId)?.name ?? '')
    loadHierarchy(selectedOrganizationId).catch((e) => setError(e.message))
  }, [organizations, selectedOrganizationId])
  useEffect(() => writeSessionSelection('atlas.organization.organization', selectedOrganizationId), [selectedOrganizationId])

  const mutate = async (url: string, method: string, body?: object) => {
    setIsSaving(true); setError(null); setNotice(null)
    try {
      const response = await fetch(url, { method, credentials: 'include', headers: body ? { 'Content-Type': 'application/json' } : undefined, body: body ? JSON.stringify(body) : undefined })
      if (!response.ok) throw new Error(await apiError(response))
      if (selectedOrganizationId !== null) await loadHierarchy(selectedOrganizationId)
      await loadOrganizations()
      return true
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Unable to save change.')
      return false
    } finally { setIsSaving(false) }
  }

  const saveOrganization = async () => {
    if (selectedOrganizationId !== null && organizationName.trim() && await mutate(`${API_BASE}/organizations/${selectedOrganizationId}/`, 'PATCH', { name: organizationName.trim() })) setNotice('Organization saved.')
  }
  const addRegion = async () => {
    if (selectedOrganizationId !== null && newRegionName.trim() && await mutate(`${API_BASE}/organizations/${selectedOrganizationId}/regions/`, 'POST', { name: newRegionName.trim() })) { setNewRegionName(''); setNotice('Region added.') }
  }
  const addDomain = async (regionId: number) => {
    const name = (newDomainNames[regionId] ?? '').trim()
    if (selectedOrganizationId !== null && name && await mutate(`${API_BASE}/domains/`, 'POST', { organization: selectedOrganizationId, region: regionId, name })) { setNewDomainNames((current) => ({ ...current, [regionId]: '' })); setNotice('Domain added.') }
  }
  const addOrgAdmin = async () => {
    if (selectedOrganizationId === null || !newOrgAdminUserId) return
    if (await mutate(`${API_BASE}/organizations/${selectedOrganizationId}/org-admins/`, 'POST', { user_id: newOrgAdminUserId })) {
      setNewOrgAdminUserId('')
      setNotice('Org Admin assigned.')
    }
  }

  return <div className="facilities-view-card organization-view">
    <div className="facilities-header"><h2>Organization</h2>{organizations.length === 1 && <div className="context-static-field organization-selector"><span>Organization</span><strong>{organizations[0].name}</strong></div>}</div>
    {error && <div className="facilities-error">{error}</div>}{notice && <div className="organization-notice">{notice}</div>}
    {organizations.length > 1 && <label className="facility-field organization-selector"><span>Organization</span><select value={selectedOrganizationId ?? ''} onChange={(e) => setSelectedOrganizationId(Number(e.target.value))}>{organizations.map((organization) => <option key={organization.id} value={organization.id}>{organization.name}</option>)}</select></label>}
    {selectedOrganizationId !== null && <>
      <div className="organization-name-row"><label className="facility-field"><span>Organization Name</span><input value={organizationName} onChange={(e) => setOrganizationName(e.target.value)} /></label><button type="button" onClick={saveOrganization} disabled={isSaving || !organizationName.trim()}>Save</button></div>
      <section className="organization-admin-section">
        <div className="organization-section-header"><div><h3>Org Admins</h3><span>Protected organization-wide access · at least one active Org Admin is required</span></div></div>
        <div className="organization-admin-list">{organizationAdmins.admins.map((membership) => <div key={membership.id}><span className="org-admin-badge">Org Admin</span><strong>{membership.user_name}</strong><span>{membership.user_email}</span></div>)}</div>
        {!!organizationAdmins.candidates.length && <div className="organization-admin-add"><select value={newOrgAdminUserId} onChange={(event) => setNewOrgAdminUserId(event.target.value ? Number(event.target.value) : '')}><option value="">Select an active user</option>{organizationAdmins.candidates.map((membership) => <option key={membership.id} value={membership.user}>{membership.user_name} · {membership.user_email}</option>)}</select><button type="button" onClick={addOrgAdmin} disabled={isSaving || !newOrgAdminUserId}>Assign Org Admin</button></div>}
      </section>
      <div className="organization-section-header"><div><h3>Regions and Domains</h3><span>{regions.filter((region) => region.active).length} active region{regions.filter((region) => region.active).length === 1 ? '' : 's'}</span></div><div className="organization-add-domain"><input value={newRegionName} onChange={(e) => setNewRegionName(e.target.value)} placeholder="New region name" /><button type="button" onClick={addRegion} disabled={isSaving || !newRegionName.trim()}>Add Region</button></div></div>
      <div className="organization-region-list">{regions.map((region) => <section className={`organization-region${region.active ? '' : ' organization-domain-row-inactive'}`} key={region.id}>
        <div className="organization-region-header"><input value={regionNames[region.id] ?? ''} onChange={(e) => setRegionNames((current) => ({ ...current, [region.id]: e.target.value }))} /><span>{region.domain_count} domain{region.domain_count === 1 ? '' : 's'}</span><div className="facility-actions"><button type="button" disabled={isSaving || !(regionNames[region.id] ?? '').trim() || regionNames[region.id]?.trim() === region.name} onClick={async () => { if (await mutate(`${API_BASE}/regions/${region.id}/`, 'PATCH', { name: regionNames[region.id].trim() })) setNotice('Region saved.') }}>Save</button><button type="button" disabled={isSaving} onClick={() => mutate(`${API_BASE}/regions/${region.id}/`, 'PATCH', { active: !region.active })}>{region.active ? 'Deactivate' : 'Reactivate'}</button><button type="button" className="danger" disabled={isSaving} onClick={() => window.confirm(`Delete ${region.name}? This cannot be undone.`) && mutate(`${API_BASE}/regions/${region.id}/`, 'DELETE')}>Delete</button></div></div>
        <div className="organization-domain-list">{domains.filter((domain) => domain.region === region.id).map((domain) => <div className={`organization-domain-row${domain.active ? '' : ' organization-domain-row-inactive'}`} key={domain.id}><input value={domainNames[domain.id] ?? ''} onChange={(e) => setDomainNames((current) => ({ ...current, [domain.id]: e.target.value }))} /><span>{domain.membership_count} users</span><div className="facility-actions"><button type="button" disabled={isSaving || !(domainNames[domain.id] ?? '').trim() || domainNames[domain.id]?.trim() === domain.name} onClick={() => mutate(`${API_BASE}/domains/${domain.id}/`, 'PATCH', { name: domainNames[domain.id].trim() })}>Save</button><button type="button" disabled={isSaving} onClick={() => mutate(`${API_BASE}/domains/${domain.id}/`, 'PATCH', { active: !domain.active })}>{domain.active ? 'Deactivate' : 'Reactivate'}</button><button type="button" className="danger" disabled={isSaving} onClick={() => window.confirm(`Delete ${domain.name}? This cannot be undone.`) && mutate(`${API_BASE}/domains/${domain.id}/`, 'DELETE')}>Delete</button></div></div>)}</div>
        <div className="organization-add-domain organization-add-domain-nested"><input value={newDomainNames[region.id] ?? ''} onChange={(e) => setNewDomainNames((current) => ({ ...current, [region.id]: e.target.value }))} placeholder="New domain name" /><button type="button" onClick={() => addDomain(region.id)} disabled={isSaving || !(newDomainNames[region.id] ?? '').trim()}>Add Domain</button></div>
      </section>)}</div>
    </>}
  </div>
}
