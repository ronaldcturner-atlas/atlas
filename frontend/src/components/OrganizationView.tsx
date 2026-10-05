import React, { useEffect, useState } from 'react'

type Organization = { id: number; name: string; active: boolean; region_count: number; domain_count: number }
type Region = { id: number; organization: number; name: string; active: boolean; domain_count: number }
type Domain = { id: number; region: number; region_name: string; organization: number; name: string; active: boolean; membership_count: number }
const API_BASE = 'http://localhost:8000/api'

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
  const [selectedOrganizationId, setSelectedOrganizationId] = useState<number | null>(null)
  const [organizationName, setOrganizationName] = useState('')
  const [regions, setRegions] = useState<Region[]>([])
  const [domains, setDomains] = useState<Domain[]>([])
  const [regionNames, setRegionNames] = useState<Record<number, string>>({})
  const [domainNames, setDomainNames] = useState<Record<number, string>>({})
  const [newRegionName, setNewRegionName] = useState('')
  const [newDomainNames, setNewDomainNames] = useState<Record<number, string>>({})
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [isSaving, setIsSaving] = useState(false)

  const loadOrganizations = async () => {
    const response = await fetch(`${API_BASE}/organizations/`, { credentials: 'include' })
    if (!response.ok) throw new Error(await apiError(response))
    const data: Organization[] = await response.json()
    setOrganizations(data)
    setSelectedOrganizationId((current) => current ?? data[0]?.id ?? null)
  }

  const loadHierarchy = async (organizationId: number) => {
    const [regionResponse, domainResponse] = await Promise.all([
      fetch(`${API_BASE}/organizations/${organizationId}/regions/`, { credentials: 'include' }),
      fetch(`${API_BASE}/domains/?organization=${organizationId}`, { credentials: 'include' }),
    ])
    if (!regionResponse.ok) throw new Error(await apiError(regionResponse))
    if (!domainResponse.ok) throw new Error(await apiError(domainResponse))
    const nextRegions: Region[] = await regionResponse.json()
    const nextDomains: Domain[] = await domainResponse.json()
    setRegions(nextRegions); setDomains(nextDomains)
    setRegionNames(Object.fromEntries(nextRegions.map((region) => [region.id, region.name])))
    setDomainNames(Object.fromEntries(nextDomains.map((domain) => [domain.id, domain.name])))
  }

  useEffect(() => { loadOrganizations().catch((e) => setError(e.message)) }, [])
  useEffect(() => {
    if (selectedOrganizationId === null) return
    setOrganizationName(organizations.find((item) => item.id === selectedOrganizationId)?.name ?? '')
    loadHierarchy(selectedOrganizationId).catch((e) => setError(e.message))
  }, [organizations, selectedOrganizationId])

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

  return <div className="facilities-view-card organization-view">
    <div className="facilities-header"><h2>Organization</h2></div>
    {error && <div className="facilities-error">{error}</div>}{notice && <div className="organization-notice">{notice}</div>}
    {organizations.length > 1 && <label className="facility-field organization-selector"><span>Organization</span><select value={selectedOrganizationId ?? ''} onChange={(e) => setSelectedOrganizationId(Number(e.target.value))}>{organizations.map((organization) => <option key={organization.id} value={organization.id}>{organization.name}</option>)}</select></label>}
    {selectedOrganizationId !== null && <>
      <div className="organization-name-row"><label className="facility-field"><span>Organization Name</span><input value={organizationName} onChange={(e) => setOrganizationName(e.target.value)} /></label><button type="button" onClick={saveOrganization} disabled={isSaving || !organizationName.trim()}>Save</button></div>
      <div className="organization-section-header"><div><h3>Regions and Domains</h3><span>{regions.filter((region) => region.active).length} active region{regions.filter((region) => region.active).length === 1 ? '' : 's'}</span></div><div className="organization-add-domain"><input value={newRegionName} onChange={(e) => setNewRegionName(e.target.value)} placeholder="New region name" /><button type="button" onClick={addRegion} disabled={isSaving || !newRegionName.trim()}>Add Region</button></div></div>
      <div className="organization-region-list">{regions.map((region) => <section className={`organization-region${region.active ? '' : ' organization-domain-row-inactive'}`} key={region.id}>
        <div className="organization-region-header"><input value={regionNames[region.id] ?? ''} onChange={(e) => setRegionNames((current) => ({ ...current, [region.id]: e.target.value }))} /><span>{region.domain_count} domain{region.domain_count === 1 ? '' : 's'}</span><div className="facility-actions"><button type="button" disabled={isSaving || !(regionNames[region.id] ?? '').trim() || regionNames[region.id]?.trim() === region.name} onClick={async () => { if (await mutate(`${API_BASE}/regions/${region.id}/`, 'PATCH', { name: regionNames[region.id].trim() })) setNotice('Region saved.') }}>Save</button><button type="button" disabled={isSaving} onClick={() => mutate(`${API_BASE}/regions/${region.id}/`, 'PATCH', { active: !region.active })}>{region.active ? 'Deactivate' : 'Reactivate'}</button><button type="button" className="danger" disabled={isSaving} onClick={() => window.confirm(`Delete ${region.name}? This cannot be undone.`) && mutate(`${API_BASE}/regions/${region.id}/`, 'DELETE')}>Delete</button></div></div>
        <div className="organization-domain-list">{domains.filter((domain) => domain.region === region.id).map((domain) => <div className={`organization-domain-row${domain.active ? '' : ' organization-domain-row-inactive'}`} key={domain.id}><input value={domainNames[domain.id] ?? ''} onChange={(e) => setDomainNames((current) => ({ ...current, [domain.id]: e.target.value }))} /><span>{domain.membership_count} users</span><div className="facility-actions"><button type="button" disabled={isSaving || !(domainNames[domain.id] ?? '').trim() || domainNames[domain.id]?.trim() === domain.name} onClick={() => mutate(`${API_BASE}/domains/${domain.id}/`, 'PATCH', { name: domainNames[domain.id].trim() })}>Save</button><button type="button" disabled={isSaving} onClick={() => mutate(`${API_BASE}/domains/${domain.id}/`, 'PATCH', { active: !domain.active })}>{domain.active ? 'Deactivate' : 'Reactivate'}</button><button type="button" className="danger" disabled={isSaving} onClick={() => window.confirm(`Delete ${domain.name}? This cannot be undone.`) && mutate(`${API_BASE}/domains/${domain.id}/`, 'DELETE')}>Delete</button></div></div>)}</div>
        <div className="organization-add-domain organization-add-domain-nested"><input value={newDomainNames[region.id] ?? ''} onChange={(e) => setNewDomainNames((current) => ({ ...current, [region.id]: e.target.value }))} placeholder="New domain name" /><button type="button" onClick={() => addDomain(region.id)} disabled={isSaving || !(newDomainNames[region.id] ?? '').trim()}>Add Domain</button></div>
      </section>)}</div>
    </>}
  </div>
}
