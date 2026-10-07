import React, { useEffect, useMemo, useState } from 'react'
import RequestBuilderView from './RequestBuilderView'

type BuildStatus = 'PRE_BUILD' | 'BUILD' | 'PREVIEW' | 'ARCHIVE'

type ScheduleBlock = {
  id: number
  name: string
  domain: number
  domain_name: string
  region: number
  region_name: string
  start_date: string
  end_date: string
  request_open_datetime: string
  request_close_datetime: string
  build_status: BuildStatus
  created_at: string
  updated_at: string
  published_at: string | null
  published_runs: Array<{
    schedule_version_id: number
    domain_name: string
    run_id: number | null
    run_number: number | null
  }>
  can_manage_build_workspace: boolean
  can_administer_requests: boolean
  can_submit_own_requests: boolean
  can_view_preview: boolean
  can_open_build_workspace: boolean
  can_publish_schedule: boolean
  can_unpublish_schedule: boolean
  my_requests?: Array<{
    id: number
    date: string
    request_type: 'DAY_OFF' | 'SHIFT_OFF' | 'DAY_ON' | 'SHIFT_ON'
    weight: 'LOW' | 'MEDIUM' | 'HIGH' | 'FIXED'
  }>
}

type DomainOption = {
  id: number
  name: string
  region: number
  region_name: string
  active: boolean
}

type ScheduleBlockFormState = {
  start_date: string
  end_date: string
  request_open_datetime: string
  request_close_datetime: string
}

type ScheduleBlocksViewProps = {
  requestUserView?: boolean
  previewPhysicianId?: number | null
  requestBlockId?: number | null
  onOpenRequests?: (blockId: number) => void
  onCloseRequests?: () => void
  onOpenBuild?: (blockId: number) => void
}

const API_BASE = 'http://localhost:8000/api'
const SCHEDULE_BLOCK_REGION_KEY = 'atlas.scheduleBlocks.region'
const SCHEDULE_BLOCK_DOMAIN_KEY = 'atlas.scheduleBlocks.domain'

const defaultFormState: ScheduleBlockFormState = {
  start_date: '',
  end_date: '',
  request_open_datetime: '',
  request_close_datetime: '',
}

function formatDate(isoDate: string) {
  const parsed = new Date(`${isoDate}T00:00:00`)
  if (Number.isNaN(parsed.getTime())) {
    return isoDate
  }
  return parsed.toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
    timeZone: 'UTC',
  })
}

function formatDateTime(value: string | null) {
  if (!value) {
    return '-'
  }

  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) {
    return value
  }

  return parsed.toLocaleString('en-US', {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
    timeZoneName: 'short',
  })
}

function toLocalDatetimeInputValue(isoValue: string) {
  const parsed = new Date(isoValue)
  if (Number.isNaN(parsed.getTime())) {
    return ''
  }

  const offsetMs = parsed.getTimezoneOffset() * 60 * 1000
  const local = new Date(parsed.getTime() - offsetMs)
  return local.toISOString().slice(0, 16)
}

function toIsoFromDatetimeLocal(localValue: string) {
  if (!localValue) {
    return ''
  }

  const parsed = new Date(localValue)
  if (Number.isNaN(parsed.getTime())) {
    return ''
  }

  return parsed.toISOString()
}

function buildGeneratedName(startDate: string, endDate: string) {
  if (!startDate || !endDate) {
    return 'Name will be generated automatically'
  }

  const start = new Date(`${startDate}T00:00:00`)
  const end = new Date(`${endDate}T00:00:00`)
  if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime())) {
    return 'Name will be generated automatically'
  }

  const startLabel = start.toLocaleDateString('en-US', { month: 'short', year: 'numeric', timeZone: 'UTC' })
  const endLabel = end.toLocaleDateString('en-US', { month: 'short', year: 'numeric', timeZone: 'UTC' })

  return startLabel === endLabel ? startLabel : `${startLabel}-${endLabel}`
}

function getRequestStatus(requestOpenDatetime: string, requestCloseDatetime: string) {
  const now = Date.now()
  const open = new Date(requestOpenDatetime).getTime()
  const close = new Date(requestCloseDatetime).getTime()

  if (Number.isNaN(open) || Number.isNaN(close)) {
    return 'Unknown'
  }

  if (now < open) {
    return 'Not Open'
  }

  if (now <= close) {
    return 'Open'
  }

  return 'Closed'
}

async function parseApiResponseError(response: Response) {
  try {
    const data = await response.json()

    if (data?.requires_acknowledgement && typeof data.warning === 'string') {
      return {
        message: data.warning,
        requiresAcknowledgement: true,
      }
    }

    if (typeof data === 'string') {
      return { message: data, requiresAcknowledgement: false }
    }

    if (data?.detail && typeof data.detail === 'string') {
      return { message: data.detail, requiresAcknowledgement: false }
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
        return {
          message: validationMessages.join(' '),
          requiresAcknowledgement: false,
        }
      }
    }
  } catch {
    return { message: null, requiresAcknowledgement: false }
  }

  return { message: null, requiresAcknowledgement: false }
}

export default function ScheduleBlocksView({
  requestUserView = false,
  previewPhysicianId = null,
  requestBlockId = null,
  onOpenRequests,
  onCloseRequests,
  onOpenBuild,
}: ScheduleBlocksViewProps) {
  const [blocks, setBlocks] = useState<ScheduleBlock[]>([])
  const [domains, setDomains] = useState<DomainOption[]>([])
  const [managedDomainIds, setManagedDomainIds] = useState<Set<number>>(new Set())
  const [selectedRegionId, setSelectedRegionId] = useState<number | null>(() => {
    const stored = Number(window.sessionStorage.getItem(SCHEDULE_BLOCK_REGION_KEY))
    return Number.isInteger(stored) && stored > 0 ? stored : null
  })
  const [selectedDomainId, setSelectedDomainId] = useState<number | null>(() => {
    const stored = Number(window.sessionStorage.getItem(SCHEDULE_BLOCK_DOMAIN_KEY))
    return Number.isInteger(stored) && stored > 0 ? stored : null
  })
  const [isLoading, setIsLoading] = useState(true)
  const [isSaving, setIsSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const [isModalOpen, setIsModalOpen] = useState(false)
  const [editingBlockId, setEditingBlockId] = useState<number | null>(null)
  const [isReadOnlyOpen, setIsReadOnlyOpen] = useState(false)
  const [formState, setFormState] = useState<ScheduleBlockFormState>(defaultFormState)
  const [openedBlock, setOpenedBlock] = useState<ScheduleBlock | null>(null)
  const [activeModalTab, setActiveModalTab] = useState<'details' | 'requests'>('details')

  const fetchBlocks = async () => {
    try {
      setIsLoading(true)
      setError(null)

      const [response, domainsResponse, managedDomainsResponse] = await Promise.all([
        fetch(`${API_BASE}/schedule-blocks/`, { credentials: 'include' }),
        fetch(`${API_BASE}/domains/?active=true&permissions=manage_build_workspace,administer_requests,submit_own_requests,view_preview`, { credentials: 'include' }),
        fetch(`${API_BASE}/domains/?active=true&permission=manage_build_workspace`, { credentials: 'include' }),
      ])

      if (!response.ok) {
        const parsed = await parseApiResponseError(response)
        throw new Error(parsed.message ?? 'Unable to load Schedule Blocks.')
      }

      if (!domainsResponse.ok) {
        const parsed = await parseApiResponseError(domainsResponse)
        throw new Error(parsed.message ?? 'Unable to load scheduling Domains.')
      }
      if (!managedDomainsResponse.ok) {
        const parsed = await parseApiResponseError(managedDomainsResponse)
        throw new Error(parsed.message ?? 'Unable to load Build Workspace permissions.')
      }

      const data = await response.json()
      const domainData = await domainsResponse.json()
      const managedDomainData = await managedDomainsResponse.json()
      setBlocks(data)
      setDomains(domainData.filter((domain: DomainOption) => domain.active))
      setManagedDomainIds(new Set(managedDomainData.map((domain: DomainOption) => domain.id)))
    } catch (fetchError) {
      console.error(fetchError)
      setError(fetchError instanceof Error ? fetchError.message : 'Unable to load Schedule Blocks right now.')
    } finally {
      setIsLoading(false)
    }
  }

  useEffect(() => {
    fetchBlocks()
  }, [])

  const regions = useMemo(() => Array.from(
    new Map(domains.map((domain) => [domain.region, domain.region_name])).entries(),
  ).map(([id, name]) => ({ id, name })), [domains])

  const domainsForRegion = useMemo(
    () => domains.filter((domain) => domain.region === selectedRegionId),
    [domains, selectedRegionId],
  )

  useEffect(() => {
    if (!domains.length) return

    const requestedBlock = requestBlockId === null
      ? null
      : blocks.find((block) => block.id === requestBlockId) ?? null
    if (requestedBlock) {
      setSelectedRegionId(requestedBlock.region)
      setSelectedDomainId(requestedBlock.domain)
      return
    }

    const regionId = regions.some((region) => region.id === selectedRegionId)
      ? selectedRegionId
      : regions[0]?.id ?? null
    const eligibleDomains = domains.filter((domain) => domain.region === regionId)
    const domainId = eligibleDomains.some((domain) => domain.id === selectedDomainId)
      ? selectedDomainId
      : eligibleDomains[0]?.id ?? null
    if (regionId !== selectedRegionId) setSelectedRegionId(regionId)
    if (domainId !== selectedDomainId) setSelectedDomainId(domainId)
  }, [blocks, domains, regions, requestBlockId, selectedDomainId, selectedRegionId])

  useEffect(() => {
    if (selectedRegionId) window.sessionStorage.setItem(SCHEDULE_BLOCK_REGION_KEY, String(selectedRegionId))
  }, [selectedRegionId])

  useEffect(() => {
    if (selectedDomainId) window.sessionStorage.setItem(SCHEDULE_BLOCK_DOMAIN_KEY, String(selectedDomainId))
  }, [selectedDomainId])

  useEffect(() => {
    if (editingBlockId === null) {
      return
    }

    const updatedBlock = blocks.find((block) => block.id === editingBlockId) ?? null
    setOpenedBlock(updatedBlock)
  }, [blocks, editingBlockId])

  useEffect(() => {
    if (requestBlockId === null) {
      if (activeModalTab === 'requests') {
        setIsModalOpen(false)
        setEditingBlockId(null)
        setIsReadOnlyOpen(false)
        setOpenedBlock(null)
        setActiveModalTab('details')
        setFormState(defaultFormState)
      }
      return
    }

    const block = blocks.find((item) => item.id === requestBlockId)
    if (!block) {
      return
    }

    setEditingBlockId(block.id)
    setIsReadOnlyOpen(block.build_status === 'ARCHIVE')
    setOpenedBlock(block)
    setActiveModalTab('requests')
    setFormState({
      start_date: block.start_date,
      end_date: block.end_date,
      request_open_datetime: toLocalDatetimeInputValue(block.request_open_datetime),
      request_close_datetime: toLocalDatetimeInputValue(block.request_close_datetime),
    })
    setIsModalOpen(true)
  }, [blocks, requestBlockId])

  const sortedBlocks = useMemo(() => {
    const newestScheduleFirst = (left: ScheduleBlock, right: ScheduleBlock) => (
      right.start_date.localeCompare(left.start_date)
      || right.end_date.localeCompare(left.end_date)
      || right.created_at.localeCompare(left.created_at)
    )
    const sorted = blocks
      .filter((block) => block.domain === selectedDomainId)
      .sort(newestScheduleFirst)
    if (!requestUserView) {
      return sorted
    }
    const latestPublished = sorted
      .filter((block) => block.published_at)
      .sort((a, b) => (b.published_at ?? '').localeCompare(a.published_at ?? ''))[0]
    const today = new Date().toISOString().slice(0, 10)
    const upcoming = sorted
      .filter((block) => !block.published_at && block.end_date >= today)
      .sort((a, b) => a.start_date.localeCompare(b.start_date))[0]
    return Array.from(new Map(
      [latestPublished, upcoming].filter((block): block is ScheduleBlock => Boolean(block))
        .map((block) => [block.id, block]),
    ).values()).sort(newestScheduleFirst)
  }, [blocks, requestUserView, selectedDomainId])

  const requestTypeLabel = (requestType: string) => requestType
    .toLowerCase()
    .split('_')
    .map((part) => `${part.charAt(0).toUpperCase()}${part.slice(1)}`)
    .join(' ')

  const openCreateModal = () => {
    if (!selectedDomainId) {
      setError('Select a Domain before creating a Schedule Block.')
      return
    }
    setEditingBlockId(null)
    setIsReadOnlyOpen(false)
    setOpenedBlock(null)
    setActiveModalTab('details')
    setFormState(defaultFormState)
    setIsModalOpen(true)
  }

  const openBlock = (block: ScheduleBlock, readOnly = false, initialTab: 'details' | 'requests' = 'details') => {
    setEditingBlockId(block.id)
    setIsReadOnlyOpen(readOnly || block.build_status === 'ARCHIVE')
    setOpenedBlock(block)
    setActiveModalTab(initialTab)
    setFormState({
      start_date: block.start_date,
      end_date: block.end_date,
      request_open_datetime: toLocalDatetimeInputValue(block.request_open_datetime),
      request_close_datetime: toLocalDatetimeInputValue(block.request_close_datetime),
    })
    setIsModalOpen(true)
  }

  const closeModal = () => {
    const shouldReturnToScheduleBlocks = activeModalTab === 'requests' && requestBlockId !== null
    setIsModalOpen(false)
    setEditingBlockId(null)
    setIsReadOnlyOpen(false)
    setOpenedBlock(null)
    setActiveModalTab('details')
    setFormState(defaultFormState)
    if (shouldReturnToScheduleBlocks) {
      onCloseRequests?.()
    }
  }

  const openRequests = (block: ScheduleBlock) => {
    if (onOpenRequests) {
      onOpenRequests(block.id)
      return
    }
    openBlock(block, block.build_status === 'ARCHIVE', 'requests')
  }

  const saveBlock = async () => {
    if (!formState.start_date || !formState.end_date || !formState.request_open_datetime || !formState.request_close_datetime) {
      setError('All Schedule Block fields are required.')
      return
    }

    if (editingBlockId === null && !selectedDomainId) {
      setError('Select a Domain before creating a Schedule Block.')
      return
    }

    const payload = {
      start_date: formState.start_date,
      end_date: formState.end_date,
      request_open_datetime: toIsoFromDatetimeLocal(formState.request_open_datetime),
      request_close_datetime: toIsoFromDatetimeLocal(formState.request_close_datetime),
      ...(editingBlockId === null ? { domain: selectedDomainId } : {}),
    }

    try {
      setIsSaving(true)
      setError(null)

      const isEditing = editingBlockId !== null
      const url = isEditing ? `${API_BASE}/schedule-blocks/${editingBlockId}/` : `${API_BASE}/schedule-blocks/`
      const method = isEditing ? 'PATCH' : 'POST'

      let response = await fetch(url, {
        method,
        headers: {
          'Content-Type': 'application/json',
        },
        credentials: 'include',
        body: JSON.stringify(payload),
      })

      let parsedError: { message: string | null; requiresAcknowledgement: boolean } | null = null
      if (!response.ok) {
        parsedError = await parseApiResponseError(response)

        if (!isEditing && parsedError.requiresAcknowledgement && parsedError.message) {
          const acknowledged = window.confirm(parsedError.message)
          if (acknowledged) {
            response = await fetch(url, {
              method,
              headers: {
                'Content-Type': 'application/json',
              },
              credentials: 'include',
              body: JSON.stringify({
                ...payload,
                acknowledge_overlap: true,
              }),
            })
            parsedError = null
          }
        }
      }

      if (!response.ok) {
        if (!parsedError) {
          parsedError = await parseApiResponseError(response)
        }
        throw new Error(parsedError.message ?? 'Unable to save Schedule Block.')
      }

      await fetchBlocks()
      closeModal()
    } catch (saveError) {
      console.error(saveError)
      setError(saveError instanceof Error ? saveError.message : 'Unable to save Schedule Block.')
    } finally {
      setIsSaving(false)
    }
  }

  const deleteBlock = async (block: ScheduleBlock) => {
    if (block.published_at) {
      setError('Unpublish this Schedule Block before deleting it.')
      return
    }

    const confirmed = window.confirm(
      `Permanently delete Schedule Block ${block.name}? This will remove its schedule versions, optimizer runs, assignments, requests, and trade records. This cannot be undone.`,
    )
    if (!confirmed) {
      return
    }

    try {
      setError(null)
      const response = await fetch(`${API_BASE}/schedule-blocks/${block.id}/`, {
        method: 'DELETE',
        credentials: 'include',
      })

      if (!response.ok) {
        const parsed = await parseApiResponseError(response)
        throw new Error(parsed.message ?? 'Unable to delete Schedule Block.')
      }

      await fetchBlocks()
    } catch (deleteError) {
      console.error(deleteError)
      setError(deleteError instanceof Error ? deleteError.message : 'Unable to delete Schedule Block.')
    }
  }

  const moveBackToBuild = async (block: ScheduleBlock) => {
    const confirmed = window.confirm(
      'Move this schedule block back to BUILD? Users will no longer be viewing it as preview, and scheduler edits/optimization will be enabled again.',
    )
    if (!confirmed) return

    try {
      setError(null)
      const response = await fetch(`${API_BASE}/schedule-blocks/${block.id}/move-back-to-build/`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({}),
      })
      if (!response.ok) {
        const parsed = await parseApiResponseError(response)
        throw new Error(parsed.message ?? 'Unable to move Schedule Block back to BUILD.')
      }
      await fetchBlocks()
    } catch (moveError) {
      console.error(moveError)
      setError(moveError instanceof Error ? moveError.message : 'Unable to move Schedule Block back to BUILD.')
    }
  }

  if (isLoading) {
    return <div className="scheduler-loading">Loading Schedule Blocks...</div>
  }

  const selectedRegion = regions.find((region) => region.id === selectedRegionId) ?? null
  const selectedDomain = domains.find((domain) => domain.id === selectedDomainId) ?? null
  const modalRegionName = openedBlock?.region_name ?? selectedRegion?.name ?? ''
  const modalDomainName = openedBlock?.domain_name ?? selectedDomain?.name ?? ''
  const schedulingScopeControls = (
    <div className="schedule-block-scope" aria-label="Schedule Block scope">
      <div className="schedule-block-scope-field">
        <span>Region</span>
        {regions.length > 1 ? (
          <select
            value={selectedRegionId ?? ''}
            onChange={(event) => {
              const nextRegionId = Number(event.target.value)
              const firstDomain = domains.find((domain) => domain.region === nextRegionId)
              setSelectedRegionId(nextRegionId)
              setSelectedDomainId(firstDomain?.id ?? null)
            }}
          >
            {regions.map((region) => <option key={region.id} value={region.id}>{region.name}</option>)}
          </select>
        ) : (
          <strong>{selectedRegion?.name ?? 'No active Region'}</strong>
        )}
      </div>
      <div className="schedule-block-scope-field">
        <span>Domain</span>
        {domainsForRegion.length > 1 ? (
          <select
            value={selectedDomainId ?? ''}
            onChange={(event) => setSelectedDomainId(Number(event.target.value))}
          >
            {domainsForRegion.map((domain) => <option key={domain.id} value={domain.id}>{domain.name}</option>)}
          </select>
        ) : (
          <strong>{selectedDomain?.name ?? 'No active Domain'}</strong>
        )}
      </div>
    </div>
  )

  const unpublishBlock = async (block: ScheduleBlock) => {
    const confirmed = window.confirm(
      'Unpublish this schedule and return it to BUILD? It will be removed from the live Schedule page, but all draft assignments and optimizer runs will be preserved.',
    )
    if (!confirmed) return

    try {
      setError(null)
      const response = await fetch(`${API_BASE}/schedule-blocks/${block.id}/unpublish/`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({}),
      })
      if (!response.ok) {
        const parsed = await parseApiResponseError(response)
        throw new Error(parsed.message ?? 'Unable to unpublish this Schedule Block.')
      }
      await fetchBlocks()
    } catch (unpublishError) {
      console.error(unpublishError)
      setError(unpublishError instanceof Error ? unpublishError.message : 'Unable to unpublish this Schedule Block.')
    }
  }

  if (requestUserView) {
    return (
      <div className="facilities-view-card">
        <div className="facilities-header">
          <div>
            <h2>Schedule Blocks</h2>
            <p className="user-view-helper">Choose Requests while a request period is open.</p>
          </div>
          {schedulingScopeControls}
        </div>
        {error && <div className="facilities-error">{error}</div>}
        <div className="scheduler-table-wrap">
          <table className="scheduler-table schedule-blocks-table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Requests Open</th>
                <th>Requests Close</th>
                <th>Requests</th>
              </tr>
            </thead>
            <tbody>
              {sortedBlocks.map((block) => {
                const requestStatus = getRequestStatus(block.request_open_datetime, block.request_close_datetime)
                return (
                  <tr key={block.id}>
                    <td>{block.name}</td>
                    <td>{formatDateTime(block.request_open_datetime)}</td>
                    <td>{formatDateTime(block.request_close_datetime)}</td>
                    <td>
                      <div className="facility-actions">
                        {block.can_submit_own_requests && (
                          <button
                            type="button"
                            className={`user-request-button ${requestStatus === 'Open' ? 'user-request-button-open' : 'user-request-button-closed'}`}
                            onClick={() => openRequests(block)}
                            disabled={requestStatus !== 'Open' || (block.build_status !== 'PRE_BUILD' && block.build_status !== 'BUILD')}
                            title={requestStatus === 'Open' ? 'Enter schedule requests' : `Request period is ${requestStatus.toLowerCase()}`}
                          >
                            Requests
                          </button>
                        )}
                        {block.build_status === 'PREVIEW' && block.can_view_preview && (
                          <button type="button" onClick={() => onOpenBuild?.(block.id)}>
                            View Preview
                          </button>
                        )}
                      </div>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
        {!sortedBlocks.length && <div className="empty-state">No request periods are available.</div>}

        {isModalOpen && activeModalTab === 'requests' && openedBlock && (
          <div className="shift-modal-overlay" onClick={closeModal}>
            <div className="shift-modal shift-modal-wide schedule-block-modal request-builder-modal" onClick={(event) => event.stopPropagation()}>
              <div className="shift-modal-header"><h2>Enter Requests</h2></div>
              <div className="shift-modal-body">
                <RequestBuilderView
                  block={openedBlock}
                  forceUserView
                  physicianId={previewPhysicianId}
                />
              </div>
              <div className="shift-modal-actions">
                <button className="secondary" type="button" onClick={closeModal}>Back to My Requests</button>
              </div>
            </div>
          </div>
        )}
      </div>
    )
  }

  return (
    <div className="facilities-view-card">
      <div className="facilities-header">
        <h2>Schedule Blocks</h2>
        <div className="schedule-block-header-actions">
          {schedulingScopeControls}
          {selectedDomainId && managedDomainIds.has(selectedDomainId) && (
            <button type="button" className="primary-action" onClick={openCreateModal}>
              Create New
            </button>
          )}
        </div>
      </div>

      {error && <div className="facilities-error">{error}</div>}

      <div className="scheduler-table-wrap">
        <table className="scheduler-table schedule-blocks-table">
          <thead>
            <tr>
              <th>Name</th>
              <th>Schedule Dates</th>
              <th>Request Opens</th>
              <th>Request Closes</th>
              <th>Request Status</th>
              <th>Build Status</th>
              <th>Published</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {sortedBlocks.map((block) => (
              <tr key={block.id}>
                <td>{block.name}</td>
                <td>{`${formatDate(block.start_date)} - ${formatDate(block.end_date)}`}</td>
                <td>{formatDateTime(block.request_open_datetime)}</td>
                <td>{formatDateTime(block.request_close_datetime)}</td>
                <td>{getRequestStatus(block.request_open_datetime, block.request_close_datetime)}</td>
                <td>{block.build_status}</td>
                <td>
                  <div>{formatDateTime(block.published_at)}</div>
                  {block.published_runs.map((published) => (
                    <div className="muted" key={published.schedule_version_id}>
                      {published.run_number === null ? 'Manual schedule' : `Run ${published.run_number}`} · {published.domain_name}
                    </div>
                  ))}
                </td>
                <td>
                  <div className="facility-actions">
                    {block.can_open_build_workspace && (
                      <button type="button" onClick={() => onOpenBuild?.(block.id)}>
                        Open
                      </button>
                    )}
                    {(block.can_administer_requests || block.can_submit_own_requests) && (
                      <button type="button" onClick={() => openRequests(block)}>
                        Requests
                      </button>
                    )}
                    {block.can_manage_build_workspace && (block.build_status === 'PRE_BUILD' || block.build_status === 'BUILD') && (
                      <button type="button" onClick={() => onOpenBuild?.(block.id)}>
                        Build Schedule
                      </button>
                    )}
                    {block.can_manage_build_workspace && (block.build_status === 'PRE_BUILD' || block.build_status === 'BUILD') && (
                      <>
                        <button type="button" onClick={() => openBlock(block)}>Edit Dates</button>
                        <button type="button" onClick={() => openBlock(block)}>Edit Request Window</button>
                      </>
                    )}
                    {block.can_manage_build_workspace && !block.published_at && (
                      <button type="button" onClick={() => deleteBlock(block)}>Delete</button>
                    )}
                    {block.can_manage_build_workspace && block.build_status === 'PREVIEW' && (
                      <button type="button" onClick={() => moveBackToBuild(block)}>Move Back to Build</button>
                    )}
                    {block.can_unpublish_schedule && block.build_status === 'ARCHIVE' && block.published_at && (
                      <button type="button" onClick={() => unpublishBlock(block)}>Unpublish / Return to Build</button>
                    )}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {!sortedBlocks.length && <div className="empty-state">No Schedule Blocks found</div>}

      {isModalOpen && (
        <div className="shift-modal-overlay" onClick={closeModal}>
          <div
            className={`shift-modal shift-modal-wide schedule-block-modal ${activeModalTab === 'requests' ? 'request-builder-modal' : ''}`}
            onClick={(event) => event.stopPropagation()}
          >
            <div className="shift-modal-header">
              <h2>
                {editingBlockId === null
                  ? 'Create New Schedule Block'
                  : activeModalTab === 'requests'
                    ? 'Schedule Block Requests'
                    : isReadOnlyOpen
                      ? 'Schedule Block (View Only)'
                      : 'Edit Schedule Block'}
              </h2>
            </div>
            {editingBlockId !== null && (
              <div className="schedule-block-modal-tabs">
                <button
                  type="button"
                  className={activeModalTab === 'details' ? 'active' : ''}
                  onClick={() => {
                    setActiveModalTab('details')
                    if (requestBlockId !== null) {
                      onCloseRequests?.()
                    }
                  }}
                >
                  Details
                </button>
                <button
                  type="button"
                  className={activeModalTab === 'requests' ? 'active' : ''}
                  onClick={() => {
                    if (editingBlockId !== null && onOpenRequests) {
                      onOpenRequests(editingBlockId)
                    } else {
                      setActiveModalTab('requests')
                    }
                  }}
                >
                  Requests
                </button>
              </div>
            )}
            <div className="shift-modal-body">
              {activeModalTab === 'details' && (
                <>
                  <div className="schedule-block-modal-scope">
                    <div><span>Region</span><strong>{modalRegionName}</strong></div>
                    <div><span>Domain</span><strong>{modalDomainName}</strong></div>
                  </div>
                  <label className="facility-field">
                    <span>Schedule Block Name</span>
                    <input
                      type="text"
                      value={buildGeneratedName(formState.start_date, formState.end_date)}
                      readOnly
                    />
                  </label>
                  <div className="shift-filters-grid">
                    <label className="facility-field">
                      <span>Schedule Start Date</span>
                      <input
                        type="date"
                        value={formState.start_date}
                        onChange={(event) =>
                          setFormState((current) => {
                            const nextStartDate = event.target.value
                            const nextEndDate = current.end_date && current.end_date < nextStartDate
                              ? nextStartDate
                              : current.end_date
                            return {
                              ...current,
                              start_date: nextStartDate,
                              end_date: nextEndDate,
                            }
                          })
                        }
                        disabled={isReadOnlyOpen}
                      />
                    </label>
                    <label className="facility-field">
                      <span>Schedule End Date</span>
                      <input
                        type="date"
                        value={formState.end_date}
                        min={formState.start_date || undefined}
                        onChange={(event) =>
                          setFormState((current) => ({ ...current, end_date: event.target.value }))
                        }
                        disabled={isReadOnlyOpen}
                      />
                    </label>
                  </div>
                  <div className="shift-filters-grid">
                    <label className="facility-field">
                      <span>Request Open Date/Time</span>
                      <input
                        type="datetime-local"
                        value={formState.request_open_datetime}
                        onChange={(event) =>
                          setFormState((current) => {
                            const nextRequestOpen = event.target.value
                            const nextRequestClose =
                              current.request_close_datetime && current.request_close_datetime < nextRequestOpen
                                ? nextRequestOpen
                                : current.request_close_datetime

                            return {
                              ...current,
                              request_open_datetime: nextRequestOpen,
                              request_close_datetime: nextRequestClose,
                            }
                          })
                        }
                        disabled={isReadOnlyOpen}
                      />
                    </label>
                    <label className="facility-field">
                      <span>Request Close Date/Time</span>
                      <input
                        type="datetime-local"
                        value={formState.request_close_datetime}
                        min={formState.request_open_datetime || undefined}
                        onChange={(event) =>
                          setFormState((current) => ({ ...current, request_close_datetime: event.target.value }))
                        }
                        disabled={isReadOnlyOpen}
                      />
                    </label>
                  </div>
                </>
              )}

              {activeModalTab === 'requests' && openedBlock && (
                <RequestBuilderView
                  block={{
                    id: openedBlock.id,
                    start_date: openedBlock.start_date,
                    end_date: openedBlock.end_date,
                    build_status: openedBlock.build_status,
                  }}
                />
              )}
            </div>
            <div className="shift-modal-actions">
              <button className="secondary" type="button" onClick={closeModal}>
                {activeModalTab === 'requests' ? 'Back to Schedule Blocks' : 'Close'}
              </button>
              {activeModalTab === 'details' && !isReadOnlyOpen && (
                <button type="button" onClick={saveBlock} disabled={isSaving}>
                  {isSaving ? 'Saving...' : 'Save'}
                </button>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
