import React, { useState, useEffect, useRef } from 'react'
import { useAuth } from '../contexts/AuthContext'
import { API_BASE } from '../api'

type APIShift = {
  id: number | null
  shift_instance_id: number
  facility: number
  facility_name: string
  facility_short_name: string
  facility_sort_order: number
  physician: number | null
  physician_name: string
  role: string
  role_display: string
  date: string
  start_time: string
  end_time: string
  status: string
  status_display: string
  posting_mode: 'PICKUP' | 'TRADE_ONLY' | null
  split_group_id: number | null
  split_group_start_time: string | null
  is_split: boolean
  domain: number
  domain_name: string
  region: number
  region_name: string
}

type DomainOption = {
  id: number
  name: string
  region: number
  region_name: string
  active: boolean
}

type Shift = {
  assignmentId: number | null
  instanceId: number
  physicianId: number | null
  facility: string
  facilityOrder: number
  shift: string
  role: string
  physician_name: string
  date: string
  status: string
  postingMode: 'PICKUP' | 'TRADE_ONLY' | null
  startTime: string
  endTime: string
  splitGroupId: number | null
  splitGroupStartTime: string | null
  isSplit: boolean
  domainId: number
  domainName: string
}

const DOMAIN_RIBBON_COLORS = [
  '#8b5cf6',
  '#14b8a6',
  '#f59e0b',
  '#38bdf8',
  '#f43f5e',
  '#84cc16',
  '#e879f9',
  '#fb7185',
]

function domainPriority(domain: Pick<DomainOption, 'name'>) {
  const normalized = domain.name.trim().toLowerCase()
  if (normalized === 'physician') return 0
  if (normalized === 'app') return 1
  return 2
}

function domainRibbonColor(domain: Pick<DomainOption, 'name'>, fallbackIndex: number) {
  const normalized = domain.name.trim().toLowerCase()
  if (normalized === 'physician') return DOMAIN_RIBBON_COLORS[0]
  if (normalized === 'app') return DOMAIN_RIBBON_COLORS[1]
  return DOMAIN_RIBBON_COLORS[(fallbackIndex + 2) % DOMAIN_RIBBON_COLORS.length]
}

type Trade = {
  id: number
  trade_type: 'PICKUP' | 'TRADE'
  status_display: string
  status: 'PENDING_RECIPIENT' | 'PENDING_SCHEDULER' | 'DECLINED' | 'APPROVED' | 'CANCELLED'
  offered_assignment: { id: number; physician_name: string; date: string; facility: string; start_time: string; end_time: string }
  requested_assignment: { id: number; physician_name: string; date: string; facility: string; start_time: string; end_time: string } | null
  can_accept: boolean
  can_cancel: boolean
  can_review: boolean
}

type PhysicianOption = {
  id: number
  first_name: string
  last_name: string
  display_name: string
  active: boolean
}

type TradeOption = {
  id: number
  physician_id: number
  physician_name: string
  date: string
  facility: string
  start_time: string
  end_time: string
}

type ScheduleDateComment = {
  id: number | string
  source: 'ONE_TIME' | 'RECURRING'
  series_id: number | null
  date: string
  title: string
  details: string
  schedule_block: number | null
  domain?: number | null
  domain_name?: string | null
  updated_at: string
  recurrence_type?: 'WEEKLY' | 'MONTHLY'
  interval?: number
  monthly_ordinal?: number | null
  end_type?: 'NEVER' | 'ON_DATE' | 'AFTER_COUNT'
  end_date?: string | null
  occurrence_count?: number | null
}

const SHIFT_TONE_CLASS: Record<string, string> = {
  '7a-7p': 'shift-tone-day',
  '7p-7a': 'shift-tone-night',
  '9a-9p': 'shift-tone-long-day',
  '1p-1a': 'shift-tone-swing',
  'fast-track': 'shift-tone-fast-track',
  midday: 'shift-tone-midday',
}

function getShiftTone(role: string) {
  const normalized = role.toLowerCase().replace(/[^a-z0-9]+/g, '-')
  return SHIFT_TONE_CLASS[normalized] ?? 'shift-tone-default'
}

function formatDisplayTime(date: Date) {
  const hour = date.getHours()
  const suffix = hour < 12 ? 'a' : 'p'
  const displayHour = hour % 12 || 12
  const minutes = date.getMinutes()
  return `${displayHour}${minutes ? `:${String(minutes).padStart(2, '0')}` : ''}${suffix}`
}

function parseDateTime(dateValue: string, timeValue: string) {
  return new Date(`${dateValue}T${timeValue}`)
}

function formatClockValue(timeValue: string) {
  return formatDisplayTime(parseDateTime('2000-01-01', timeValue))
}

type CalendarProps = {
  shiftsRefreshToken: number
  forceUserView?: boolean
}

export default function Calendar({ shiftsRefreshToken, forceUserView = false }: CalendarProps){
  const { user } = useAuth()
  const today = new Date()
  const physicianFilterRef = useRef<HTMLDetailsElement>(null)
  const domainFilterRef = useRef<HTMLDetailsElement>(null)

  // viewDate represents the first day of the currently displayed month
  const [viewDate, setViewDate] = useState<Date>(new Date(today.getFullYear(), today.getMonth(), 1))
  const [selectedShift, setSelectedShift] = useState<Shift | null>(null)
  const [allShifts, setAllShifts] = useState<APIShift[]>([])
  const [domains, setDomains] = useState<DomainOption[]>([])
  const [selectedRegionId, setSelectedRegionId] = useState<number | null>(() => {
    const stored = Number(window.sessionStorage.getItem('atlas.schedule.region'))
    return Number.isInteger(stored) && stored > 0 ? stored : null
  })
  const [selectedDomainIds, setSelectedDomainIds] = useState<number[]>(() => {
    try {
      const stored = JSON.parse(window.sessionStorage.getItem('atlas.schedule.domains') ?? '[]')
      return Array.isArray(stored) ? stored.filter((value) => Number.isInteger(value)) : []
    } catch {
      return []
    }
  })
  const [physicians, setPhysicians] = useState<PhysicianOption[]>([])
  const [selectedPhysicianIds, setSelectedPhysicianIds] = useState<number[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [trades, setTrades] = useState<Trade[]>([])
  const [tradePolicy, setTradePolicy] = useState({ require_scheduler_approval: true, can_manage: false })
  const [showTrades, setShowTrades] = useState(false)
  const [splitTime, setSplitTime] = useState('12:00')
  const [tradeOnlyPosting, setTradeOnlyPosting] = useState(false)
  const [tradeOptions, setTradeOptions] = useState<TradeOption[]>([])
  const [tradePartnerId, setTradePartnerId] = useState<number | ''>('')
  const [tradeTargetId, setTradeTargetId] = useState<number | ''>('')
  const [tradeNote, setTradeNote] = useState('')
  const [offeredAssignmentId, setOfferedAssignmentId] = useState<number | ''>('')
  const [reassignPhysicianId, setReassignPhysicianId] = useState<number | ''>('')
  const [unsplitPhysicianId, setUnsplitPhysicianId] = useState<number | ''>('')
  const [adminSwapAssignmentId, setAdminSwapAssignmentId] = useState<number | ''>('')
  const [openShiftPhysicianId, setOpenShiftPhysicianId] = useState<number | ''>('')
  const [dateComments, setDateComments] = useState<ScheduleDateComment[]>([])
  const [commentDate, setCommentDate] = useState<string | null>(null)
  const [commentTitle, setCommentTitle] = useState('')
  const [commentDetails, setCommentDetails] = useState('')
  const [commentRepeat, setCommentRepeat] = useState('NONE')
  const [commentEndType, setCommentEndType] = useState<'NEVER' | 'ON_DATE' | 'AFTER_COUNT'>('NEVER')
  const [commentEndDate, setCommentEndDate] = useState('')
  const [commentOccurrenceCount, setCommentOccurrenceCount] = useState(10)
  const [commentEditScope, setCommentEditScope] = useState<'THIS' | 'FUTURE' | 'ALL'>('THIS')
  const [isSavingComment, setIsSavingComment] = useState(false)
  const [actualStartTime, setActualStartTime] = useState('')
  const [actualEndTime, setActualEndTime] = useState('')
  const [isMutating, setIsMutating] = useState(false)
  const [localRefreshToken, setLocalRefreshToken] = useState(0)

  // Fetch shifts from API
  useEffect(() => {
    const fetchShifts = async () => {
      try {
        setLoadError(null)
        const [shiftsResponse, physiciansResponse, tradesResponse, policyResponse, commentsResponse, domainsResponse] = await Promise.all([
          fetch(`${API_BASE}/published-schedule/`, { credentials: 'include' }),
          fetch(`${API_BASE}/physicians/`, { credentials: 'include' }),
          fetch(`${API_BASE}/shift-trades/`, { credentials: 'include' }),
          fetch(`${API_BASE}/shift-trade-policy/`, { credentials: 'include' }),
          fetch(`${API_BASE}/published-schedule-comments/`, { credentials: 'include' }),
          fetch(`${API_BASE}/domains/?active=true&accessible=true`, { credentials: 'include' }),
        ])
        if (!shiftsResponse.ok || !physiciansResponse.ok || !domainsResponse.ok) {
          throw new Error('Unable to load the schedule filters')
        }
        const [shiftsData, physiciansData, tradesData, policyData, commentsData, domainsData] = await Promise.all([
          shiftsResponse.json(),
          physiciansResponse.json(),
          tradesResponse.ok ? tradesResponse.json() : [],
          policyResponse.ok ? policyResponse.json() : tradePolicy,
          commentsResponse.ok ? commentsResponse.json() : [],
          domainsResponse.json(),
        ])
        setAllShifts(shiftsData)
        setPhysicians(physiciansData)
        setTrades(tradesData)
        setTradePolicy(policyData)
        setDateComments(commentsData)
        setDomains(domainsData)
      } catch (error) {
        console.error('Error fetching shifts:', error)
        setLoadError(error instanceof Error ? error.message : 'Unable to load the schedule')
      } finally {
        setIsLoading(false)
      }
    }

    fetchShifts()
  }, [shiftsRefreshToken, localRefreshToken])

  useEffect(() => {
    const closePhysicianFilter = (event: MouseEvent) => {
      for (const menu of [physicianFilterRef.current, domainFilterRef.current]) {
        if (menu?.open && event.target instanceof Node && !menu.contains(event.target)) {
          menu.removeAttribute('open')
        }
      }
    }

    document.addEventListener('mousedown', closePhysicianFilter)
    return () => document.removeEventListener('mousedown', closePhysicianFilter)
  }, [])

  const regions = Array.from(new Map(
    domains.map((domain) => [domain.region, { id: domain.region, name: domain.region_name }]),
  ).values())
  const domainsForRegion = domains.filter((domain) => domain.region === selectedRegionId)

  useEffect(() => {
    if (!domains.length) return
    const nextRegionId = regions.some((region) => region.id === selectedRegionId)
      ? selectedRegionId
      : regions[0]?.id ?? null
    const regionDomainIds = domains
      .filter((domain) => domain.region === nextRegionId)
      .map((domain) => domain.id)
    const validSelectedIds = selectedDomainIds.filter((id) => regionDomainIds.includes(id))
    const nextDomainIds = validSelectedIds.length ? validSelectedIds : regionDomainIds
    if (nextRegionId !== selectedRegionId) setSelectedRegionId(nextRegionId)
    if (nextDomainIds.join(',') !== selectedDomainIds.join(',')) setSelectedDomainIds(nextDomainIds)
  }, [domains, regions, selectedDomainIds, selectedRegionId])

  useEffect(() => {
    if (selectedRegionId) window.sessionStorage.setItem('atlas.schedule.region', String(selectedRegionId))
  }, [selectedRegionId])

  useEffect(() => {
    if (selectedDomainIds.length) {
      window.sessionStorage.setItem('atlas.schedule.domains', JSON.stringify(selectedDomainIds))
    }
  }, [selectedDomainIds])

  const selectedDomainSet = new Set(selectedDomainIds)
  const domainVisibleShifts = allShifts.filter((shift) => selectedDomainSet.has(shift.domain))
  const visiblePhysicianIds = new Set(
    domainVisibleShifts.flatMap((shift) => shift.physician == null ? [] : [shift.physician]),
  )
  const domainsInDisplayOrder = [...domainsForRegion]
    .sort((left, right) => (
      domainPriority(left) - domainPriority(right)
      || left.name.localeCompare(right.name)
      || left.id - right.id
    ))
  const selectedDomainsInDisplayOrder = domainsInDisplayOrder
    .filter((domain) => selectedDomainSet.has(domain.id))
  const showDomainGroups = selectedDomainsInDisplayOrder.length > 1
  const domainColorById = new Map(domainsInDisplayOrder.map((domain, index) => [
    domain.id,
    domainRibbonColor(domain, index),
  ]))

  const sortedPhysicians = physicians.filter((physician) => visiblePhysicianIds.has(physician.id)).sort((left, right) => {
    const leftName = left.display_name || `${left.first_name} ${left.last_name}`
    const rightName = right.display_name || `${right.first_name} ${right.last_name}`
    return leftName.localeCompare(rightName)
  })

  useEffect(() => {
    setSelectedPhysicianIds((current) => current.filter((id) => visiblePhysicianIds.has(id)))
  }, [selectedDomainIds.join(','), allShifts])
  const normalizedUserName = `${user?.first_name ?? ''} ${user?.last_name ?? ''}`.trim().toLowerCase()
  const userLastName = (user?.last_name ?? '').trim().toLowerCase()
  const exactNamePhysician = normalizedUserName
    ? physicians.find((physician) => (
        `${physician.first_name} ${physician.last_name}`.trim().toLowerCase() === normalizedUserName
        || physician.display_name.trim().toLowerCase() === normalizedUserName
      ))
    : undefined
  const lastNameMatches = userLastName
    ? physicians.filter((physician) => (
        physician.last_name.trim().toLowerCase() === userLastName
        || physician.display_name.trim().toLowerCase() === userLastName
      ))
    : []
  const myPhysicianId = user?.physician_id
    ?? exactNamePhysician?.id
    ?? (lastNameMatches.length === 1 ? lastNameMatches[0].id : null)
  const canManage = !forceUserView && Boolean(user?.is_staff || user?.is_superuser || user?.groups.some((group) => ['admin', 'scheduler'].includes(group.toLowerCase())))

  useEffect(() => {
    setTradeOptions([])
    setTradePartnerId('')
    setTradeTargetId('')
    setAdminSwapAssignmentId('')
    setOpenShiftPhysicianId('')
    setUnsplitPhysicianId('')
    setTradeNote('')
    if (!selectedShift?.assignmentId || (selectedShift.physicianId !== myPhysicianId && !canManage)) return
    fetch(`${API_BASE}/schedule-assignments/${selectedShift.assignmentId}/trade-options/`, { credentials: 'include' })
      .then(async (response) => {
        const data = await response.json()
        if (!response.ok) throw new Error(data.detail ?? 'Unable to load trade options.')
        setTradeOptions(data)
      })
      .catch((error) => setLoadError(error instanceof Error ? error.message : 'Unable to load trade options.'))
  }, [selectedShift?.assignmentId, selectedShift?.physicianId, myPhysicianId, canManage])

  const selectedPhysicianSet = new Set(selectedPhysicianIds)
  const isGroupSchedule = selectedPhysicianIds.length === 0
  const isMySchedule = myPhysicianId != null
    && selectedPhysicianIds.length === 1
    && selectedPhysicianIds[0] === myPhysicianId

  const toggleMySchedule = () => {
    if (myPhysicianId == null) {
      return
    }
    setSelectedPhysicianIds(isMySchedule ? [] : [myPhysicianId])
  }

  const togglePhysician = (physicianId: number) => {
    setSelectedPhysicianIds((current) => (
      current.includes(physicianId)
        ? current.filter((id) => id !== physicianId)
        : [...current, physicianId]
    ))
  }

  const toggleDomain = (domainId: number) => {
    setSelectedDomainIds((current) => {
      if (current.includes(domainId)) {
        return current.length === 1 ? current : current.filter((id) => id !== domainId)
      }
      return [...current, domainId]
    })
  }

  const year = viewDate.getFullYear()
  const month = viewDate.getMonth() // 0 = January

  // compute month layout dynamically
  const startingDayOfWeek = new Date(year, month, 1).getDay()
  const daysInMonth = new Date(year, month + 1, 0).getDate()
  const totalCells = startingDayOfWeek + daysInMonth
  const rows = Math.ceil(totalCells / 7)
  const cells = rows * 7
  const calendarStart = new Date(year, month, 1 - startingDayOfWeek)
  const dateKey = (date: Date) => (
    `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`
  )
  const days = Array.from({ length: cells }, (_, index) => {
    const date = new Date(calendarStart.getFullYear(), calendarStart.getMonth(), calendarStart.getDate() + index)
    return {
      date,
      key: dateKey(date),
      isCurrentMonth: date.getFullYear() === year && date.getMonth() === month,
    }
  })
  const visibleDateKeys = new Set(days.map((day) => day.key))
  const commentsByDate = new Map(dateComments
    .filter((comment) => comment.domain == null || selectedDomainSet.has(comment.domain))
    .map((comment) => [comment.date, comment]))

  // Convert API shifts to calendar format
  const shifts: Record<string, Shift[]> = {}
  domainVisibleShifts.forEach((apiShift) => {
    if (!isGroupSchedule && !selectedPhysicianSet.has(apiShift.physician)) {
      return
    }
    const startDate = parseDateTime(apiShift.date, apiShift.start_time)
    const endDate = parseDateTime(apiShift.date, apiShift.end_time)
    
    // Include the adjacent-month dates visible in the first and last weeks.
    if (visibleDateKeys.has(apiShift.date)) {
      // Format time range (e.g., "7a–7p")
      const shift = `${formatDisplayTime(startDate)}-${formatDisplayTime(endDate)}`
      
      // Format date string
      const dateStr = startDate.toLocaleDateString('en-US', { year: 'numeric', month: 'long', day: 'numeric' })
      
      // Capitalize status
      const statusCapitalized = apiShift.status_display
      
      if (!shifts[apiShift.date]) {
        shifts[apiShift.date] = []
      }
      
      shifts[apiShift.date].push({
        assignmentId: apiShift.id,
        instanceId: apiShift.shift_instance_id,
        physicianId: apiShift.physician,
        facility: apiShift.facility_short_name || apiShift.facility_name,
        facilityOrder: apiShift.facility_sort_order,
        shift,
        role: apiShift.role_display,
        physician_name: apiShift.physician_name,
        date: dateStr,
        status: statusCapitalized,
        postingMode: apiShift.posting_mode,
        startTime: apiShift.start_time,
        endTime: apiShift.end_time,
        splitGroupId: apiShift.split_group_id,
        splitGroupStartTime: apiShift.split_group_start_time,
        isSplit: apiShift.is_split,
        domainId: apiShift.domain,
        domainName: apiShift.domain_name,
      })
    }
  })

  Object.values(shifts).forEach((dayShifts) => {
    dayShifts.sort((left, right) => {
      const facilityComparison = left.facilityOrder - right.facilityOrder
        || left.facility.localeCompare(right.facility)
      if (facilityComparison) return facilityComparison
      const leftAnchor = left.splitGroupStartTime || left.startTime
      const rightAnchor = right.splitGroupStartTime || right.startTime
      const anchorComparison = leftAnchor.localeCompare(rightAnchor)
      if (anchorComparison) return anchorComparison
      if (left.splitGroupId != null && left.splitGroupId === right.splitGroupId) {
        return left.startTime.localeCompare(right.startTime)
      }
      return left.startTime.localeCompare(right.startTime)
        || left.endTime.localeCompare(right.endTime)
        || left.role.localeCompare(right.role)
        || left.physician_name.localeCompare(right.physician_name)
    })
  })

  const hasShifts = Object.keys(shifts).length > 0

  const goPrev = () => setViewDate(d => new Date(d.getFullYear(), d.getMonth() - 1, 1))
  const goNext = () => setViewDate(d => new Date(d.getFullYear(), d.getMonth() + 1, 1))
  const goToday = () => setViewDate(new Date(today.getFullYear(), today.getMonth(), 1))
  const todayStart = new Date(today.getFullYear(), today.getMonth(), today.getDate())
  const dayCellClassName = (day: typeof days[number]) => {
    const classes = ['day-cell']
    if (!day.isCurrentMonth) classes.push('day-cell-adjacent')
    if (day.date.getTime() === todayStart.getTime()) classes.push('day-cell-today')
    else if (day.date < todayStart) classes.push('day-cell-past')
    return classes.join(' ')
  }
  const myAssignments = domainVisibleShifts.filter((shift) => shift.physician === myPhysicianId)
  const pendingTradeCount = trades.filter((trade) => trade.can_accept || trade.can_review).length
  const pendingTrades = trades.filter((trade) => ['PENDING_RECIPIENT', 'PENDING_SCHEDULER'].includes(trade.status))
  const pendingAssignmentIds = new Set(pendingTrades.flatMap((trade) => [
    trade.offered_assignment.id,
    ...(trade.requested_assignment ? [trade.requested_assignment.id] : []),
  ]))
  const statusClassForShift = (shift: Shift) => {
    if (shift.status.toLowerCase() === 'open') return 'shift-status-open'
    const isOwn = shift.physicianId === myPhysicianId
    if (isOwn && shift.assignmentId != null && pendingAssignmentIds.has(shift.assignmentId)) return 'shift-status-own-pending'
    if (isOwn && shift.postingMode) return 'shift-status-own-posted'
    if (!isOwn && shift.postingMode) return 'shift-status-posted-other'
    if (isOwn) return 'shift-status-own'
    return getShiftTone(shift.role)
  }
  const tradePartners = Array.from(new Map(
    tradeOptions.map((option) => [option.physician_id, option.physician_name]),
  ).entries()).sort((left, right) => left[1].localeCompare(right[1]))
  const selectedPartnerShifts = tradePartnerId === ''
    ? []
    : tradeOptions.filter((option) => option.physician_id === tradePartnerId)

  const openCommentEditor = (date: string) => {
    if (!canManage) return
    if (selectedDomainIds.length !== 1) {
      setLoadError('Select one Domain before adding or editing a calendar comment.')
      return
    }
    const comment = commentsByDate.get(date)
    setCommentDate(date)
    setCommentTitle(comment?.title ?? '')
    setCommentDetails(comment?.details ?? '')
    setCommentRepeat(
      comment?.recurrence_type === 'MONTHLY'
        ? 'MONTHLY'
        : comment?.recurrence_type === 'WEEKLY'
          ? `WEEKLY_${comment.interval ?? 1}`
          : 'NONE',
    )
    setCommentEndType(comment?.end_type ?? 'NEVER')
    setCommentEndDate(comment?.end_date ?? '')
    setCommentOccurrenceCount(comment?.occurrence_count ?? 10)
    setCommentEditScope('THIS')
    setLoadError(null)
  }

  const commentRecurrencePayload = () => {
    if (commentRepeat === 'NONE' || !commentDate) return {}
    const selectedDate = parseDateTime(commentDate, '12:00')
    const occurrenceNumber = Math.ceil(selectedDate.getDate() / 7)
    const monthlyOrdinal = occurrenceNumber <= 4 ? occurrenceNumber : -1
    return {
      recurrence_type: commentRepeat === 'MONTHLY' ? 'MONTHLY' : 'WEEKLY',
      interval: commentRepeat.startsWith('WEEKLY_') ? Number(commentRepeat.split('_')[1]) : 1,
      monthly_ordinal: commentRepeat === 'MONTHLY' ? monthlyOrdinal : null,
      end_type: commentEndType,
      end_date: commentEndType === 'ON_DATE' ? commentEndDate : null,
      occurrence_count: commentEndType === 'AFTER_COUNT' ? commentOccurrenceCount : null,
    }
  }

  const saveDateComment = async () => {
    if (!commentDate) return
    try {
      setIsSavingComment(true)
      setLoadError(null)
      const existingComment = commentsByDate.get(commentDate)
      const isRecurring = existingComment?.source === 'RECURRING' && existingComment.series_id != null
      const url = isRecurring
        ? `${API_BASE}/published-schedule-comment-series/${existingComment.series_id}/occurrences/${commentDate}/`
        : `${API_BASE}/published-schedule-comments/`
      const response = await fetch(url, {
        method: isRecurring ? 'PATCH' : 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          date: commentDate,
          domain: selectedDomainIds[0],
          title: commentTitle,
          details: commentDetails,
          scope: commentEditScope,
          ...commentRecurrencePayload(),
        }),
      })
      const data = await response.json().catch(() => ({}))
      if (!response.ok) throw new Error(data.detail ?? 'Unable to save the calendar comment.')
      setCommentDate(null)
      setLocalRefreshToken((current) => current + 1)
    } catch (error) {
      setLoadError(error instanceof Error ? error.message : 'Unable to save the calendar comment.')
    } finally {
      setIsSavingComment(false)
    }
  }

  const deleteDateComment = async () => {
    if (!commentDate) return
    try {
      setIsSavingComment(true)
      setLoadError(null)
      const existingComment = commentsByDate.get(commentDate)
      const isRecurring = existingComment?.source === 'RECURRING' && existingComment.series_id != null
      const url = isRecurring
        ? `${API_BASE}/published-schedule-comment-series/${existingComment.series_id}/occurrences/${commentDate}/`
        : `${API_BASE}/published-schedule-comments/${commentDate}/?domain=${selectedDomainIds[0]}`
      const response = await fetch(url, {
        method: 'DELETE',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: isRecurring ? JSON.stringify({ scope: commentEditScope }) : undefined,
      })
      const data = await response.json().catch(() => ({}))
      if (!response.ok) throw new Error(data.detail ?? 'Unable to delete the calendar comment.')
      setCommentDate(null)
      setLocalRefreshToken((current) => current + 1)
    } catch (error) {
      setLoadError(error instanceof Error ? error.message : 'Unable to delete the calendar comment.')
    } finally {
      setIsSavingComment(false)
    }
  }

  const editingDateComment = commentDate ? commentsByDate.get(commentDate) : undefined
  const editingRecurringComment = editingDateComment?.source === 'RECURRING'
  const commentDateValue = commentDate ? parseDateTime(commentDate, '12:00') : null
  const commentWeekday = commentDateValue?.toLocaleDateString('en-US', { weekday: 'long' }) ?? ''
  const commentOrdinalNumber = commentDateValue ? Math.ceil(commentDateValue.getDate() / 7) : 1
  const commentOrdinal = commentOrdinalNumber === 1
    ? 'first'
    : commentOrdinalNumber === 2
      ? 'second'
      : commentOrdinalNumber === 3
        ? 'third'
        : commentOrdinalNumber === 4
          ? 'fourth'
          : 'last'
  const showRecurrenceSettings = !editingRecurringComment || commentEditScope === 'ALL'

  const mutate = async (url: string, body: Record<string, unknown>, method = 'POST') => {
    try {
      setIsMutating(true)
      setLoadError(null)
      const send = async (payload: Record<string, unknown>) => {
        const response = await fetch(`${API_BASE}/${url}`, {
          method, credentials: 'include', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
        })
        const data = await response.json().catch(() => ({}))
        return { response, data }
      }
      let { response, data } = await send(body)
      if (
        response.status === 409
        && data.requires_confirmation
        && !data.requires_physician_selection
        && window.confirm(data.detail ?? 'This action creates a schedule conflict. Proceed anyway?')
      ) {
        ({ response, data } = await send({ ...body, force: true }))
      }
      if (!response.ok) throw new Error(data.detail ?? 'Unable to complete that action.')
      setSelectedShift(null)
      setLocalRefreshToken((current) => current + 1)
    } catch (error) {
      setLoadError(error instanceof Error ? error.message : 'Unable to complete that action.')
    } finally {
      setIsMutating(false)
    }
  }

  const allRegionDomainsSelected = domainsForRegion.length > 0
    && domainsForRegion.every((domain) => selectedDomainSet.has(domain.id))
  const selectedDomainSummary = allRegionDomainsSelected
    ? 'All domains'
    : `${selectedDomainIds.length} domain${selectedDomainIds.length === 1 ? '' : 's'}`

  return (
    <div className="calendar-card">
      <div className="schedule-scope-toolbar">
        <div className="schedule-scope-field">
          <span>Region</span>
          {regions.length > 1 ? (
            <select
              value={selectedRegionId ?? ''}
              onChange={(event) => {
                const nextRegionId = Number(event.target.value)
                setSelectedRegionId(nextRegionId)
                setSelectedDomainIds(domains.filter((domain) => domain.region === nextRegionId).map((domain) => domain.id))
              }}
            >
              {regions.map((region) => <option key={region.id} value={region.id}>{region.name}</option>)}
            </select>
          ) : (
            <strong>{regions[0]?.name ?? 'No accessible Region'}</strong>
          )}
        </div>
        <div className="schedule-scope-field">
          <span>Domain</span>
          {domainsForRegion.length > 1 ? (
            <details ref={domainFilterRef} className="physician-filter-menu schedule-domain-filter-menu">
              <summary>{selectedDomainSummary}</summary>
              <div className="physician-filter-popover schedule-domain-filter-popover">
                <div className="physician-filter-heading">
                  <strong>Show Domains</strong>
                  <button
                    type="button"
                    onClick={() => setSelectedDomainIds(domainsForRegion.map((domain) => domain.id))}
                    disabled={allRegionDomainsSelected}
                  >
                    Show all
                  </button>
                </div>
                <div className="physician-filter-list">
                  {domainsForRegion.map((domain) => (
                    <label key={domain.id}>
                      <input
                        type="checkbox"
                        checked={selectedDomainSet.has(domain.id)}
                        onChange={() => toggleDomain(domain.id)}
                      />
                      <span>{domain.name}</span>
                    </label>
                  ))}
                </div>
              </div>
            </details>
          ) : (
            <strong>{domainsForRegion[0]?.name ?? 'No accessible Domain'}</strong>
          )}
        </div>
      </div>
      <div className="calendar-header">
        <div className="calendar-heading-row">
          <div className="calendar-month-navigation">
            <button onClick={goPrev} aria-label="Previous month">◀</button>
            <div className="month-label">{viewDate.toLocaleString(undefined, { month: 'long', year: 'numeric' })}</div>
            <button onClick={goNext} aria-label="Next month">▶</button>
          </div>
          <div className="schedule-status-legend" aria-label="Schedule highlight legend">
            <span className="shift-status-own">My shifts</span>
            <span className="shift-status-posted-other">Posted by another user</span>
            <span className="shift-status-own-posted">Your posted shift</span>
            <span className="shift-status-own-pending">Your pending trade</span>
            <span className="shift-status-open">Open shift</span>
          </div>
        </div>
        <div className="schedule-toolbar">
          <button type="button" className="trade-center-button" onClick={() => setShowTrades(true)}>
            Trade requests{pendingTradeCount ? ` (${pendingTradeCount})` : ''}
          </button>
          <label className={`my-schedule-filter ${isMySchedule ? 'selected' : ''} ${myPhysicianId == null ? 'disabled' : ''}`}>
            <input
              type="checkbox"
              checked={isMySchedule}
              disabled={myPhysicianId == null}
              onChange={toggleMySchedule}
            />
            My Schedule
          </label>
          <details ref={physicianFilterRef} className="physician-filter-menu">
            <summary>
              {isGroupSchedule
                ? 'All Users'
                : `${selectedPhysicianIds.length} physician${selectedPhysicianIds.length === 1 ? '' : 's'}`}
            </summary>
            <div className="physician-filter-popover">
              <div className="physician-filter-heading">
                <strong>Show schedules</strong>
                <button type="button" onClick={() => setSelectedPhysicianIds([])} disabled={isGroupSchedule}>Clear</button>
              </div>
              <div className="physician-filter-list">
                {sortedPhysicians.map((physician) => {
                  const name = physician.display_name || `${physician.first_name} ${physician.last_name}`.trim()
                  return (
                    <label key={physician.id} className={!physician.active ? 'inactive' : ''}>
                      <input
                        type="checkbox"
                        checked={selectedPhysicianSet.has(physician.id)}
                        onChange={() => togglePhysician(physician.id)}
                      />
                      <span>{name}</span>
                      {!physician.active && <small>Inactive</small>}
                    </label>
                  )
                })}
              </div>
            </div>
          </details>
          <div className="controls">
          <button onClick={goToday}>Today</button>
          <button className="primary">Month</button>
          </div>
        </div>
      </div>

      {loadError && <div className="schedule-filter-error">{loadError}</div>}
      {!isGroupSchedule && (
        <div className="schedule-filter-status">
          Showing {selectedPhysicianIds.length} selected physician{selectedPhysicianIds.length === 1 ? '' : 's'}.
          <button type="button" onClick={() => setSelectedPhysicianIds([])}>Return to group schedule</button>
        </div>
      )}

      <div className="calendar-weekday-banner" aria-hidden="true">
        {['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'].map((weekday) => (
          <span key={weekday}>{weekday}</span>
        ))}
      </div>
      <div className="grid">
        {days.map((day) => (
          <div key={day.key} className={dayCellClassName(day)}>
            {canManage ? (
              <button type="button" className="day-number day-number-button" onClick={() => openCommentEditor(day.key)}>
                {day.isCurrentMonth
                  ? day.date.getDate()
                  : day.date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })}
              </button>
            ) : (
              <div className="day-number">
                {day.isCurrentMonth
                  ? day.date.getDate()
                  : day.date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })}
              </div>
            )}
            {shifts[day.key] && (
              <div className="shifts-container">
                {selectedDomainsInDisplayOrder.map((domain) => {
                  const domainShifts = shifts[day.key].filter((shift) => shift.domainId === domain.id)
                  if (!domainShifts.length) return null
                  const ribbonColor = domainColorById.get(domain.id) ?? DOMAIN_RIBBON_COLORS[0]
                  return (
                    <section
                      key={domain.id}
                      className="calendar-domain-group"
                      style={{ '--domain-ribbon': ribbonColor } as React.CSSProperties}
                    >
                      {showDomainGroups && <div className="calendar-domain-heading">{domain.name}</div>}
                      {domainShifts.map((shift) => (
                      <div
                        key={`${shift.instanceId}-${shift.assignmentId ?? 'open'}`}
                        className={`shift-item shift-item-compact clickable ${statusClassForShift(shift)}`}
                        style={{ borderLeftColor: ribbonColor }}
                        onClick={() => {
                          setSelectedShift(shift)
                          setTradeOnlyPosting(shift.postingMode === 'TRADE_ONLY')
                          setActualStartTime(shift.startTime.slice(0, 5))
                          setActualEndTime(shift.endTime.slice(0, 5))
                        }}
                      >
                        <span>
                          {shift.facility} {shift.shift} {shift.physician_name}
                        </span>
                      </div>
                      ))}
                    </section>
                  )
                })}
              </div>
            )}
            {commentsByDate.get(day.key) && (
              <div className="calendar-date-comment">
                <span>{commentsByDate.get(day.key)!.title}</span>
                <div className="calendar-date-comment-hover" role="tooltip">
                  <strong>{commentsByDate.get(day.key)!.title}</strong>
                  {commentsByDate.get(day.key)!.details && <p>{commentsByDate.get(day.key)!.details}</p>}
                </div>
              </div>
            )}
          </div>
        ))}
      </div>

      {!isLoading && !hasShifts && (
        <div style={{marginTop:16}}>
          <div className="empty-state">{isGroupSchedule ? 'No shifts scheduled' : 'No scheduled shifts for the selected physicians'}</div>
        </div>
      )}

      {commentDate && (
        <div className="shift-modal-overlay" onClick={() => setCommentDate(null)}>
          <div className="shift-modal calendar-comment-modal" onClick={(event) => event.stopPropagation()}>
            <div className="shift-modal-header">
              <h2>{commentsByDate.has(commentDate) ? 'Edit date comment' : 'Add date comment'}</h2>
              <small>{parseDateTime(commentDate, '12:00').toLocaleDateString('en-US', { month: 'long', day: 'numeric', year: 'numeric' })}</small>
            </div>
            <div className="shift-modal-body">
              <label>Title<input maxLength={100} value={commentTitle} onChange={(event) => setCommentTitle(event.target.value)} autoFocus /></label>
              <label>Details<textarea value={commentDetails} onChange={(event) => setCommentDetails(event.target.value)} /></label>
              {editingRecurringComment && (
                <label>
                  Apply changes to
                  <select value={commentEditScope} onChange={(event) => setCommentEditScope(event.target.value as 'THIS' | 'FUTURE' | 'ALL')}>
                    <option value="THIS">This date only</option>
                    <option value="FUTURE">This and future dates</option>
                    <option value="ALL">Entire series</option>
                  </select>
                </label>
              )}
              {showRecurrenceSettings && (
                <label>
                  Repeat
                  <select value={commentRepeat} onChange={(event) => setCommentRepeat(event.target.value)}>
                    {!editingRecurringComment && <option value="NONE">Does not repeat</option>}
                    <option value="WEEKLY_1">Every week on {commentWeekday}</option>
                    <option value="WEEKLY_2">Every 2 weeks on {commentWeekday}</option>
                    <option value="WEEKLY_3">Every 3 weeks on {commentWeekday}</option>
                    <option value="WEEKLY_4">Every 4 weeks on {commentWeekday}</option>
                    <option value="MONTHLY">Monthly on the {commentOrdinal} {commentWeekday}</option>
                  </select>
                </label>
              )}
              {showRecurrenceSettings && commentRepeat !== 'NONE' && (
                <div className="calendar-comment-end-row">
                  <label>
                    Ends
                    <select value={commentEndType} onChange={(event) => setCommentEndType(event.target.value as 'NEVER' | 'ON_DATE' | 'AFTER_COUNT')}>
                      <option value="NEVER">Does not end</option>
                      <option value="ON_DATE">On date</option>
                      <option value="AFTER_COUNT">After occurrences</option>
                    </select>
                  </label>
                  {commentEndType === 'ON_DATE' && <label>End date<input type="date" min={commentDate ?? undefined} value={commentEndDate} onChange={(event) => setCommentEndDate(event.target.value)} /></label>}
                  {commentEndType === 'AFTER_COUNT' && <label>Occurrences<input type="number" min={1} step={1} value={commentOccurrenceCount} onChange={(event) => setCommentOccurrenceCount(Number(event.target.value))} /></label>}
                </div>
              )}
            </div>
            <div className="shift-modal-actions calendar-comment-actions">
              {commentsByDate.has(commentDate) && <button type="button" className="danger" disabled={isSavingComment} onClick={deleteDateComment}>Delete</button>}
              <button type="button" className="secondary" disabled={isSavingComment} onClick={() => setCommentDate(null)}>Cancel</button>
              <button type="button" disabled={isSavingComment || !commentTitle.trim()} onClick={saveDateComment}>{isSavingComment ? 'Saving…' : 'Save'}</button>
            </div>
          </div>
        </div>
      )}

      {selectedShift && (
        <div className="shift-modal-overlay" onClick={() => setSelectedShift(null)}>
          <div className="shift-modal schedule-shift-modal" onClick={(e) => e.stopPropagation()}>
            <div className="shift-modal-header">
              <h2>Shift details</h2>
            </div>
            <div className="shift-modal-body">
              <div className="detail-row"><span>Facility</span><span>{selectedShift.facility}</span></div>
              <div className="detail-row"><span>Physician</span><span>{selectedShift.physician_name}</span></div>
              <div className="detail-row"><span>Role</span><span>{selectedShift.role}</span></div>
              <div className="detail-row"><span>Date</span><span>{selectedShift.date}</span></div>
              <div className="detail-row"><span>Time</span><span>{selectedShift.shift}</span></div>
              {selectedShift.postingMode && <div className="detail-row"><span>Posted</span><span>{selectedShift.postingMode === 'PICKUP' ? 'Available for pickup' : 'Trade only'}</span></div>}
              {selectedShift.assignmentId != null && (selectedShift.physicianId === myPhysicianId || canManage) && (
                <div className="schedule-shift-actions">
                  <div className="shift-post-controls">
                    <strong>Post this shift</strong>
                    <label><input type="radio" checked={tradeOnlyPosting} onClick={() => setTradeOnlyPosting((current) => !current)} readOnly /> Trade only</label>
                    <button disabled={isMutating} onClick={() => mutate(`schedule-assignments/${selectedShift.assignmentId}/posting/`, { mode: tradeOnlyPosting ? 'TRADE_ONLY' : 'PICKUP' })}>Post</button>
                    {selectedShift.postingMode && <button disabled={isMutating} onClick={() => mutate(`schedule-assignments/${selectedShift.assignmentId}/posting/`, { mode: 'CLOSE' })}>Remove posting</button>}
                  </div>
                  <strong>Split shift</strong>
                  <div><input type="time" value={splitTime} onChange={(event) => setSplitTime(event.target.value)} /><button disabled={isMutating} onClick={() => mutate(`schedule-assignments/${selectedShift.assignmentId}/split/`, { split_time: splitTime })}>Split</button></div>
                  {selectedShift.isSplit && canManage && (
                    <label>
                      Recombined shift user if portions differ
                      <select value={unsplitPhysicianId} onChange={(event) => setUnsplitPhysicianId(Number(event.target.value) || '')}>
                        <option value="">Use the current user when all portions match</option>
                        {sortedPhysicians.filter((physician) => physician.active).map((physician) => <option key={physician.id} value={physician.id}>{physician.display_name}</option>)}
                      </select>
                    </label>
                  )}
                  {selectedShift.isSplit && <button disabled={isMutating} onClick={() => mutate(`schedule-assignments/${selectedShift.assignmentId}/unsplit/`, { physician_id: unsplitPhysicianId || null })}>Unsplit shift</button>}
                  {selectedShift.physicianId === myPhysicianId && (
                    <div className="propose-trade-controls">
                      <strong>Propose a trade</strong>
                      <label>
                        Trade with
                        <select value={tradePartnerId} onChange={(event) => { setTradePartnerId(Number(event.target.value) || ''); setTradeTargetId('') }}>
                          <option value="">Choose physician</option>
                          {tradePartners.map(([physicianId, physicianName]) => <option key={physicianId} value={physicianId}>{physicianName}</option>)}
                        </select>
                      </label>
                      <label>
                        Shift requested
                        <select value={tradeTargetId} disabled={tradePartnerId === ''} onChange={(event) => setTradeTargetId(Number(event.target.value) || '')}>
                          <option value="">Choose shift</option>
                          {selectedPartnerShifts.map((option) => (
                            <option key={option.id} value={option.id}>
                              {option.date} · {option.facility} · {formatClockValue(option.start_time)}-{formatClockValue(option.end_time)}
                            </option>
                          ))}
                        </select>
                      </label>
                      <label>
                        Comments (optional)
                        <textarea value={tradeNote} onChange={(event) => setTradeNote(event.target.value)} />
                      </label>
                      <button disabled={isMutating || !tradeTargetId} onClick={() => mutate('shift-trades/', { offered_assignment_id: selectedShift.assignmentId, target_assignment_id: tradeTargetId, note: tradeNote })}>Send trade proposal</button>
                      {!tradeOptions.length && <small>No conflict-free trade options are currently available.</small>}
                    </div>
                  )}
                </div>
              )}
              {selectedShift.assignmentId != null && selectedShift.physicianId !== myPhysicianId && selectedShift.postingMode && myPhysicianId != null && (
                <div className="schedule-shift-actions">
                  <strong>{selectedShift.postingMode === 'PICKUP' ? 'Request pickup' : 'Offer a trade'}</strong>
                  {selectedShift.postingMode === 'TRADE_ONLY' && (
                    <select value={offeredAssignmentId} onChange={(event) => setOfferedAssignmentId(Number(event.target.value) || '')}>
                      <option value="">Choose one of your shifts</option>
                      {myAssignments.map((shift) => <option key={shift.id} value={shift.id}>{shift.date} · {shift.facility_short_name} · {shift.start_time}-{shift.end_time}</option>)}
                    </select>
                  )}
                  <button disabled={isMutating || (selectedShift.postingMode === 'TRADE_ONLY' && !offeredAssignmentId)} onClick={() => mutate('shift-trades/', { target_assignment_id: selectedShift.assignmentId, offered_assignment_id: offeredAssignmentId || null })}>Send request</button>
                </div>
              )}
              {selectedShift.assignmentId != null && canManage && (
                <div className="schedule-shift-actions">
                  <strong>Actual shift times</strong>
                  <div className="actual-shift-time-controls">
                    <label>Start<input type="time" value={actualStartTime} onChange={(event) => setActualStartTime(event.target.value)} /></label>
                    <label>End<input type="time" value={actualEndTime} onChange={(event) => setActualEndTime(event.target.value)} /></label>
                    <button disabled={isMutating || !actualStartTime || !actualEndTime} onClick={() => mutate(`shift-instances/${selectedShift.instanceId}/times/`, { start_time: actualStartTime, end_time: actualEndTime }, 'PATCH')}>Update times</button>
                  </div>
                  <strong>Change scheduled user</strong>
                  <select value={reassignPhysicianId} onChange={(event) => setReassignPhysicianId(Number(event.target.value) || '')}>
                    <option value="">Choose physician</option>
                    {sortedPhysicians.filter((physician) => physician.active).map((physician) => <option key={physician.id} value={physician.id}>{physician.display_name}</option>)}
                  </select>
                  <button disabled={isMutating || !reassignPhysicianId} onClick={() => mutate(`schedule-assignments/${selectedShift.assignmentId}/reassign/`, { physician_id: reassignPhysicianId })}>Change user</button>
                  <strong>Swap scheduled users</strong>
                  <select value={adminSwapAssignmentId} onChange={(event) => setAdminSwapAssignmentId(Number(event.target.value) || '')}>
                    <option value="">Choose another user&apos;s shift</option>
                    {tradeOptions.map((option) => (
                      <option key={option.id} value={option.id}>
                        {option.physician_name} · {option.date} · {option.facility} · {formatClockValue(option.start_time)}-{formatClockValue(option.end_time)}
                      </option>
                    ))}
                  </select>
                  <button disabled={isMutating || !adminSwapAssignmentId} onClick={() => mutate(`schedule-assignments/${selectedShift.assignmentId}/swap/`, { target_assignment_id: adminSwapAssignmentId })}>Swap users</button>
                  <strong>Open shift</strong>
                  <button disabled={isMutating} onClick={() => mutate(`schedule-assignments/${selectedShift.assignmentId}/open/`, {})}>Open shift</button>
                </div>
              )}
              {selectedShift.assignmentId == null && selectedShift.status.toLowerCase() === 'open' && canManage && (
                <div className="schedule-shift-actions">
                  <strong>Fill open shift</strong>
                  <select value={openShiftPhysicianId} onChange={(event) => setOpenShiftPhysicianId(Number(event.target.value) || '')}>
                    <option value="">Choose physician</option>
                    {sortedPhysicians.filter((physician) => physician.active).map((physician) => <option key={physician.id} value={physician.id}>{physician.display_name}</option>)}
                  </select>
                  <button disabled={isMutating || !openShiftPhysicianId} onClick={() => mutate(`shift-instances/${selectedShift.instanceId}/assign/`, { physician_id: openShiftPhysicianId })}>Assign user</button>
                </div>
              )}
              {selectedShift.assignmentId != null && pendingTrades.filter((trade) => (
                trade.can_cancel && (
                  trade.offered_assignment.id === selectedShift.assignmentId
                  || trade.requested_assignment?.id === selectedShift.assignmentId
                )
              )).map((trade) => (
                <div className="schedule-shift-actions" key={trade.id}>
                  <strong>Pending trade offer</strong>
                  <button disabled={isMutating} onClick={() => mutate(`shift-trades/${trade.id}/cancel/`, {})}>Cancel trade offer</button>
                </div>
              ))}
            </div>
            <div className="shift-modal-actions">
              <button className="secondary" onClick={() => setSelectedShift(null)}>Close</button>
            </div>
          </div>
        </div>
      )}

      {showTrades && (
        <div className="shift-modal-overlay" onClick={() => setShowTrades(false)}>
          <div className="shift-modal shift-trade-modal" onClick={(event) => event.stopPropagation()}>
            <div className="shift-modal-header"><h2>Shift trade requests</h2></div>
            {tradePolicy.can_manage && <label className="trade-policy"><input type="checkbox" checked={tradePolicy.require_scheduler_approval} onChange={(event) => mutate('shift-trade-policy/', { require_scheduler_approval: event.target.checked }, 'PATCH')} /> Require scheduler approval</label>}
            <div className="trade-request-list">
              {trades.map((trade) => (
                <div className="trade-request-card" key={trade.id}>
                  <strong>{trade.trade_type === 'PICKUP' ? 'Pickup' : 'Trade'} · {trade.status_display}</strong>
                  <span>{trade.offered_assignment.date} · {trade.offered_assignment.facility} · {trade.offered_assignment.physician_name}</span>
                  {trade.requested_assignment && <span>For {trade.requested_assignment.date} · {trade.requested_assignment.facility} · {trade.requested_assignment.physician_name}</span>}
                  <div>{trade.can_accept && <><button onClick={() => mutate(`shift-trades/${trade.id}/accept/`, {})}>Accept</button><button onClick={() => mutate(`shift-trades/${trade.id}/decline/`, {})}>Decline</button></>}{trade.can_review && <><button onClick={() => mutate(`shift-trades/${trade.id}/approve/`, {})}>Approve</button><button onClick={() => mutate(`shift-trades/${trade.id}/reject/`, {})}>Reject</button></>}{trade.can_cancel && <button onClick={() => mutate(`shift-trades/${trade.id}/cancel/`, {})}>Cancel</button>}</div>
                </div>
              ))}
              {!trades.length && <div className="empty-state">No trade requests</div>}
            </div>
            <div className="shift-modal-actions"><button onClick={() => setShowTrades(false)}>Close</button></div>
          </div>
        </div>
      )}
    </div>
  )
}
