import React, { useEffect, useMemo, useState } from 'react'
import RequestBuilderView from './RequestBuilderView'

type BuildStatus = 'PRE_BUILD' | 'BUILD' | 'PREVIEW' | 'ARCHIVE'

type ScheduleBlock = {
  id: number
  name: string
  start_date: string
  end_date: string
  request_open_datetime: string
  request_close_datetime: string
  build_status: BuildStatus
  created_at: string
  updated_at: string
  published_at: string | null
}

type ScheduleBlockFormState = {
  start_date: string
  end_date: string
  request_open_datetime: string
  request_close_datetime: string
}

const API_BASE = 'http://localhost:8000/api'

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

export default function ScheduleBlocksView() {
  const [blocks, setBlocks] = useState<ScheduleBlock[]>([])
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

      const response = await fetch(`${API_BASE}/schedule-blocks/`, {
        credentials: 'include',
      })

      if (!response.ok) {
        const parsed = await parseApiResponseError(response)
        throw new Error(parsed.message ?? 'Unable to load Schedule Blocks.')
      }

      const data = await response.json()
      setBlocks(data)
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

  useEffect(() => {
    if (editingBlockId === null) {
      return
    }

    const updatedBlock = blocks.find((block) => block.id === editingBlockId) ?? null
    setOpenedBlock(updatedBlock)
  }, [blocks, editingBlockId])

  const sortedBlocks = useMemo(
    () => [...blocks].sort((a, b) => b.created_at.localeCompare(a.created_at)),
    [blocks],
  )

  const openCreateModal = () => {
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
    setIsModalOpen(false)
    setEditingBlockId(null)
    setIsReadOnlyOpen(false)
    setOpenedBlock(null)
    setActiveModalTab('details')
    setFormState(defaultFormState)
  }

  const saveBlock = async () => {
    if (!formState.start_date || !formState.end_date || !formState.request_open_datetime || !formState.request_close_datetime) {
      setError('All Schedule Block fields are required.')
      return
    }

    const payload = {
      start_date: formState.start_date,
      end_date: formState.end_date,
      request_open_datetime: toIsoFromDatetimeLocal(formState.request_open_datetime),
      request_close_datetime: toIsoFromDatetimeLocal(formState.request_close_datetime),
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
    if (block.build_status !== 'PRE_BUILD') {
      return
    }

    const confirmed = window.confirm(`Delete Schedule Block ${block.name}?`)
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

  const enterPreview = async (block: ScheduleBlock) => {
    try {
      setError(null)
      const response = await fetch(`${API_BASE}/schedule-blocks/${block.id}/enter-preview/`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        credentials: 'include',
        body: JSON.stringify({}),
      })

      if (!response.ok) {
        const parsed = await parseApiResponseError(response)
        throw new Error(parsed.message ?? 'Unable to enter preview.')
      }

      await fetchBlocks()
    } catch (previewError) {
      console.error(previewError)
      setError(previewError instanceof Error ? previewError.message : 'Unable to enter preview.')
    }
  }

  const publishBlock = async (block: ScheduleBlock) => {
    try {
      setError(null)
      let response = await fetch(`${API_BASE}/schedule-blocks/${block.id}/publish/`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        credentials: 'include',
        body: JSON.stringify({}),
      })

      let parsedError: { message: string | null; requiresAcknowledgement: boolean } | null = null
      if (!response.ok) {
        parsedError = await parseApiResponseError(response)

        if (parsedError.requiresAcknowledgement && parsedError.message) {
          const acknowledged = window.confirm(parsedError.message)
          if (acknowledged) {
            response = await fetch(`${API_BASE}/schedule-blocks/${block.id}/publish/`, {
              method: 'POST',
              headers: {
                'Content-Type': 'application/json',
              },
              credentials: 'include',
              body: JSON.stringify({ acknowledge_overlap: true }),
            })
            parsedError = null
          }
        }
      }

      if (!response.ok) {
        if (!parsedError) {
          parsedError = await parseApiResponseError(response)
        }
        throw new Error(parsedError.message ?? 'Unable to publish Schedule Block.')
      }

      await fetchBlocks()
    } catch (publishError) {
      console.error(publishError)
      setError(publishError instanceof Error ? publishError.message : 'Unable to publish Schedule Block.')
    }
  }

  if (isLoading) {
    return <div className="scheduler-loading">Loading Schedule Blocks...</div>
  }

  return (
    <div className="facilities-view-card">
      <div className="facilities-header">
        <h2>Schedule Blocks</h2>
        <button type="button" className="primary-action" onClick={openCreateModal}>
          Create New
        </button>
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
                <td>{formatDateTime(block.published_at)}</td>
                <td>
                  <div className="facility-actions">
                    <button type="button" onClick={() => openBlock(block, block.build_status === 'ARCHIVE')}>
                      Open
                    </button>
                    <button type="button" onClick={() => openBlock(block, block.build_status === 'ARCHIVE', 'requests')}>
                      Requests
                    </button>
                    {block.build_status !== 'ARCHIVE' && (
                      <>
                        <button type="button" onClick={() => openBlock(block)}>Edit Dates</button>
                        <button type="button" onClick={() => openBlock(block)}>Edit Request Window</button>
                      </>
                    )}
                    {block.build_status === 'PRE_BUILD' && (
                      <button type="button" onClick={() => deleteBlock(block)}>Delete</button>
                    )}
                    {(block.build_status === 'PRE_BUILD' || block.build_status === 'BUILD') && (
                      <button type="button" onClick={() => enterPreview(block)}>Enter Preview</button>
                    )}
                    {block.build_status === 'PREVIEW' && (
                      <button type="button" onClick={() => publishBlock(block)}>Publish</button>
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
          <div className="shift-modal shift-modal-wide schedule-block-modal" onClick={(event) => event.stopPropagation()}>
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
                  onClick={() => setActiveModalTab('details')}
                >
                  Details
                </button>
                <button
                  type="button"
                  className={activeModalTab === 'requests' ? 'active' : ''}
                  onClick={() => setActiveModalTab('requests')}
                >
                  Requests
                </button>
              </div>
            )}
            <div className="shift-modal-body">
              {activeModalTab === 'details' && (
                <>
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
                Close
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
