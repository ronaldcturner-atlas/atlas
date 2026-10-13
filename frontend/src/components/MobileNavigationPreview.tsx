import React from 'react'
import { useAuth } from '../contexts/AuthContext'
import { API_BASE } from '../api'
import { shiftHasStarted } from '../utils/regressionRules'
import './MobileNavigationPreview.css'

type PreviewPage = 'schedule' | 'stats' | 'users' | 'more'
type ScheduleDisplay = 'list' | 'month'
type ListScheduleScope = 'group' | 'mine'
type ShiftTone = 'standard' | 'own' | 'available' | 'posted' | 'trade-sent' | 'trade-received' | 'open'
type ShiftWorkflow = 'post' | 'trade' | 'split' | 'unsplit' | 'reassign' | 'assign' | 'times' | 'swap' | 'pickup' | 'offer'

const primaryItems: Array<{ id: PreviewPage; label: string; icon: string }> = [
  { id: 'schedule', label: 'Schedule', icon: '▦' },
  { id: 'stats', label: 'Stats', icon: '▥' },
  { id: 'users', label: 'Users', icon: '●' },
  { id: 'more', label: 'More', icon: '•••' },
]

type PreviewShift = {
  facility: string
  time: string
  physician: string
  domain: string
  tone?: ShiftTone
  isMine?: boolean
  postingMode?: 'pickup' | 'trade'
  assignmentId?: number | null
  instanceId?: number
  physicianId?: number | null
  startDateTime?: string
  startTime?: string
  endTime?: string
  isSplit?: boolean
  canPickUp?: boolean
  pickupIneligibilityReason?: string
  postingComment?: string
  domainId?: number
}
type SelectedPreviewShift = PreviewShift & { date: Date }

type APIShift = {
  id: number | null
  shift_instance_id: number
  facility_name: string
  facility_short_name: string
  physician: number | null
  physician_name: string
  date: string
  start_datetime: string
  start_time: string
  end_time: string
  status: string
  posting_mode: 'PICKUP' | 'TRADE_ONLY' | null
  posting_comment: string
  can_pick_up: boolean
  pickup_ineligibility_reason: string
  domain: number
  domain_name: string
  region: number
  region_name: string
  is_split: boolean
  is_night: boolean
}

type DomainOption = { id: number; name: string; region: number; region_name: string; active: boolean }
type TradeOfferOption = { id: number; date: string; facility: string; time: string }
type TradeOption = TradeOfferOption & { physicianId: number; physicianName: string }
type MobileTradeAssignment = { id: number; physician_name: string; date: string; facility: string; start_time: string; end_time: string }
type MobileTradeRequest = {
  id: number
  trade_type: 'PICKUP' | 'TRADE'
  status: string
  status_display: string
  note: string
  offered_assignment: MobileTradeAssignment
  requested_assignment: MobileTradeAssignment | null
  can_accept: boolean
  can_cancel: boolean
  can_review: boolean
  is_unseen: boolean
}
type UnsplitUserOption = {
  id: number
  display_name: string
  first_name: string
  last_name: string
  active: boolean
  domain_memberships: Array<{ domain_id: number; active: boolean; clinically_active: boolean }>
}

const demoDomains: DomainOption[] = [
  { id: -1, name: 'Physician', region: -1, region_name: 'Lowcountry Emergency Physicians', active: true },
  { id: -2, name: 'APP', region: -1, region_name: 'Lowcountry Emergency Physicians', active: true },
]

const sampleShiftsByDay: Record<number, PreviewShift[]> = {
  11: [
    { facility: 'Berkeley', time: '8a–5p', physician: 'Santos', domain: 'APP' },
  ],
  12: [
    { facility: 'Roper', time: '6a–3p', physician: 'Mullin', domain: 'Physician', tone: 'own', isMine: true },
    { facility: 'Berkeley', time: '7a–4p', physician: 'Moe', domain: 'Physician' },
    { facility: 'St Francis', time: '2p–11p', physician: 'Feingold', domain: 'APP' },
  ],
  13: [
    { facility: 'Roper', time: '6a–3p', physician: 'Taylor', domain: 'Physician', tone: 'available', postingMode: 'pickup' },
    { facility: 'Roper', time: '3p–12a', physician: 'Santos', domain: 'APP' },
  ],
  14: [
    { facility: 'Berkeley', time: '7a–4p', physician: 'Moe', domain: 'Physician' },
    { facility: 'St Francis', time: '9a–6p', physician: 'Turner', domain: 'APP', tone: 'posted', isMine: true },
    { facility: 'Roper', time: '3p–12a', physician: 'Mullin', domain: 'Physician' },
  ],
  15: [
    { facility: 'St Francis', time: '6a–3p', physician: 'Feingold', domain: 'APP' },
    { facility: 'Berkeley', time: '4p–1a', physician: 'Taylor', domain: 'Physician', tone: 'trade-sent', isMine: true },
  ],
  16: [
    { facility: 'Roper', time: '7a–4p', physician: 'Open shift', domain: 'Physician', tone: 'open' },
    { facility: 'St Francis', time: '3p–12a', physician: 'Moe', domain: 'Physician' },
  ],
  17: [
    { facility: 'Berkeley', time: '7a–4p', physician: 'Feingold', domain: 'APP', tone: 'available', postingMode: 'trade' },
  ],
}

const fallbackShifts: PreviewShift[] = [
  { facility: 'Roper', time: '6a–3p', physician: 'Mullin', domain: 'Physician' },
  { facility: 'Berkeley', time: '7a–4p', physician: 'Moe', domain: 'Physician' },
  { facility: 'St Francis', time: '2p–11p', physician: 'Feingold', domain: 'APP' },
]

const MONTH_NAMES = [
  'January', 'February', 'March', 'April', 'May', 'June',
  'July', 'August', 'September', 'October', 'November', 'December',
]

function dateKey(value: Date) {
  return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, '0')}-${String(value.getDate()).padStart(2, '0')}`
}

function weekOffset(date: Date, start: Date) {
  const dateUtc = Date.UTC(date.getFullYear(), date.getMonth(), date.getDate())
  const startUtc = Date.UTC(start.getFullYear(), start.getMonth(), start.getDate())
  return Math.floor((dateUtc - startUtc) / 604800000)
}

function formatClockValue(value: string) {
  const [hoursValue, minutesValue = '0'] = value.split(':')
  const hours = Number(hoursValue)
  const minutes = Number(minutesValue)
  const suffix = hours < 12 ? 'a' : 'p'
  const displayHour = hours % 12 || 12
  return `${displayHour}${minutes ? `:${String(minutes).padStart(2, '0')}` : ''}${suffix}`
}

function shiftHours(startTime: string, endTime: string) {
  const clockMinutes = (value: string) => {
    const [hours, minutes] = value.split(':').map(Number)
    return hours * 60 + minutes
  }
  const start = clockMinutes(startTime)
  let end = clockMinutes(endTime)
  if (end <= start) end += 24 * 60
  return (end - start) / 60
}

function displayHours(value: number) {
  return Number.isInteger(value) ? String(value) : value.toFixed(1)
}

function PreviewShiftList({ date, shifts = fallbackShifts, label = 'Selected date', onSelect, currentUserName, prioritizeCalendarShifts = false }: { date: Date; shifts?: PreviewShift[]; label?: string; onSelect?: (shift: SelectedPreviewShift) => void; currentUserName?: string; prioritizeCalendarShifts?: boolean }) {
  const displayedShifts = prioritizeCalendarShifts
    ? [...shifts].sort((left, right) => {
      const priority = (shift: PreviewShift) => shift.isMine ? 0 : shift.tone && shift.tone !== 'standard' ? 1 : 2
      return priority(left) - priority(right)
    })
    : shifts
  const availableIndexes = displayedShifts.flatMap((shift, index) => (
    prioritizeCalendarShifts && !shift.isMine && shift.tone && shift.tone !== 'standard' ? [index] : []
  ))
  const firstAvailableIndex = availableIndexes[0]
  const lastAvailableIndex = availableIndexes[availableIndexes.length - 1]

  return (
    <section className="mobile-day-schedule" aria-label={`Schedule for ${date.toLocaleDateString('en-US')}`}>
      <header><span>{label}</span><strong>{date.toLocaleDateString('en-US', { weekday: 'long', month: 'long', day: 'numeric' })}</strong></header>
      {displayedShifts.length === 0 && <div className="mobile-no-shifts">No shifts scheduled</div>}
      {displayedShifts.map((shift, index) => {
        const { facility, time, physician, domain, tone = 'standard', isMine, postingMode } = shift
        const displayedPhysician = isMine && currentUserName ? currentUserName : physician
        const mobileTone = isMine ? 'own' : tone === 'standard' ? 'standard' : 'other-status'
        const statusLabel = postingMode === 'pickup' ? 'Pickup' : postingMode === 'trade' ? 'Trade' : tone === 'open' ? 'Open' : ''
        const domainLabel = domain === 'Physician' ? 'MD' : domain
        const calendarHighlight = prioritizeCalendarShifts
          ? isMine
            ? ' calendar-own-highlight'
            : index >= firstAvailableIndex && index <= lastAvailableIndex
              ? ` calendar-available-highlight${index === firstAvailableIndex ? ' available-first' : ''}${index === lastAvailableIndex ? ' available-last' : ''}`
              : ''
          : ''
        return <button type="button" className={`shift-tone-${mobileTone}${calendarHighlight}`} key={`${shift.instanceId ?? facility}-${time}`} onClick={() => onSelect?.({ ...shift, physician: displayedPhysician, date })}><i /><span><strong>{facility}</strong><small>{time}</small></span><b>{displayedPhysician}<small>{domainLabel}{statusLabel ? ` · ${statusLabel}` : ''}</small></b><em>›</em></button>
      })}
    </section>
  )
}

export default function MobileNavigationPreview() {
  const { user, logout } = useAuth()
  const [activePage, setActivePage] = React.useState<PreviewPage>(() => (
    (window.localStorage.getItem('atlas.mobile-preview.page') as PreviewPage) || 'schedule'
  ))
  const [display, setDisplay] = React.useState<ScheduleDisplay>(() => (
    (window.localStorage.getItem('atlas.mobile-preview.display') as ScheduleDisplay) || 'month'
  ))
  const [listScheduleScope, setListScheduleScope] = React.useState<ListScheduleScope>(() => (
    (window.localStorage.getItem('atlas.mobile-preview.list-scope') as ListScheduleScope) || 'group'
  ))
  const [showMore, setShowMore] = React.useState(false)
  const [showScope, setShowScope] = React.useState(false)
  const [showTradeRequests, setShowTradeRequests] = React.useState(false)
  const [selectedShift, setSelectedShift] = React.useState<SelectedPreviewShift | null>(null)
  const [shiftWorkflow, setShiftWorkflow] = React.useState<ShiftWorkflow | null>(null)
  const [postingMode, setPostingMode] = React.useState<'pickup' | 'trade'>('pickup')
  const [postComment, setPostComment] = React.useState('')
  const [tradePartner, setTradePartner] = React.useState<number | ''>('')
  const [tradeTarget, setTradeTarget] = React.useState<number | ''>('')
  const [tradeNote, setTradeNote] = React.useState('')
  const [tradeOptions, setTradeOptions] = React.useState<TradeOption[]>([])
  const [isLoadingTradeOptions, setIsLoadingTradeOptions] = React.useState(false)
  const [tradeFeedback, setTradeFeedback] = React.useState('')
  const [tradeRequiresForce, setTradeRequiresForce] = React.useState(false)
  const [isTradeSaving, setIsTradeSaving] = React.useState(false)
  const [tradeOfferAssignmentId, setTradeOfferAssignmentId] = React.useState<number | ''>('')
  const [tradeOfferNote, setTradeOfferNote] = React.useState('')
  const [eligibleTradeOffers, setEligibleTradeOffers] = React.useState<TradeOfferOption[]>([])
  const [isLoadingTradeOffers, setIsLoadingTradeOffers] = React.useState(false)
  const [tradeOfferFeedback, setTradeOfferFeedback] = React.useState('')
  const [tradeOfferRequiresForce, setTradeOfferRequiresForce] = React.useState(false)
  const [isTradeOfferSaving, setIsTradeOfferSaving] = React.useState(false)
  const [splitTime, setSplitTime] = React.useState('12:00')
  const [splitFeedback, setSplitFeedback] = React.useState('')
  const [isSplitSaving, setIsSplitSaving] = React.useState(false)
  const [unsplitFeedback, setUnsplitFeedback] = React.useState('')
  const [unsplitRequiresForce, setUnsplitRequiresForce] = React.useState(false)
  const [unsplitRequiresUser, setUnsplitRequiresUser] = React.useState(false)
  const [unsplitUserId, setUnsplitUserId] = React.useState<number | ''>('')
  const [unsplitUsers, setUnsplitUsers] = React.useState<UnsplitUserOption[]>([])
  const [isUnsplitSaving, setIsUnsplitSaving] = React.useState(false)
  const [reassignUserId, setReassignUserId] = React.useState<number | ''>('')
  const [reassignUsers, setReassignUsers] = React.useState<UnsplitUserOption[]>([])
  const [reassignFeedback, setReassignFeedback] = React.useState('')
  const [reassignRequiresForce, setReassignRequiresForce] = React.useState(false)
  const [isLoadingReassignUsers, setIsLoadingReassignUsers] = React.useState(false)
  const [isReassignSaving, setIsReassignSaving] = React.useState(false)
  const [assignUserId, setAssignUserId] = React.useState<number | ''>('')
  const [assignUsers, setAssignUsers] = React.useState<UnsplitUserOption[]>([])
  const [assignFeedback, setAssignFeedback] = React.useState('')
  const [assignRequiresForce, setAssignRequiresForce] = React.useState(false)
  const [isLoadingAssignUsers, setIsLoadingAssignUsers] = React.useState(false)
  const [isAssignSaving, setIsAssignSaving] = React.useState(false)
  const [actualStartTime, setActualStartTime] = React.useState('')
  const [actualEndTime, setActualEndTime] = React.useState('')
  const [timesFeedback, setTimesFeedback] = React.useState('')
  const [timesRequireForce, setTimesRequireForce] = React.useState(false)
  const [isTimesSaving, setIsTimesSaving] = React.useState(false)
  const [swapTargetId, setSwapTargetId] = React.useState<number | ''>('')
  const [swapOptions, setSwapOptions] = React.useState<TradeOption[]>([])
  const [swapFeedback, setSwapFeedback] = React.useState('')
  const [swapRequiresForce, setSwapRequiresForce] = React.useState(false)
  const [isLoadingSwapOptions, setIsLoadingSwapOptions] = React.useState(false)
  const [isSwapSaving, setIsSwapSaving] = React.useState(false)
  const [openShiftFeedback, setOpenShiftFeedback] = React.useState('')
  const [isOpenShiftSaving, setIsOpenShiftSaving] = React.useState(false)
  const [workflowMessage, setWorkflowMessage] = React.useState('')
  const [pickupFeedback, setPickupFeedback] = React.useState('')
  const [pickupRequiresForce, setPickupRequiresForce] = React.useState(false)
  const [isPickupSaving, setIsPickupSaving] = React.useState(false)
  const [postingFeedback, setPostingFeedback] = React.useState('')
  const [isPostingSaving, setIsPostingSaving] = React.useState(false)
  const [scheduleActionMessage, setScheduleActionMessage] = React.useState('')
  const [testUserView, setTestUserView] = React.useState(false)
  const [scheduleDataState, setScheduleDataState] = React.useState<'loading' | 'live' | 'demo'>('loading')
  const [scheduleDataMessage, setScheduleDataMessage] = React.useState('')
  const [apiShifts, setApiShifts] = React.useState<APIShift[]>([])
  const [availableDomains, setAvailableDomains] = React.useState<DomainOption[]>([])
  const [tradeRequests, setTradeRequests] = React.useState<MobileTradeRequest[]>([])
  const [isLoadingTradeRequests, setIsLoadingTradeRequests] = React.useState(false)
  const [tradeCenterFeedback, setTradeCenterFeedback] = React.useState('')
  const [tradeActionSaving, setTradeActionSaving] = React.useState('')
  const [tradeActionConfirmation, setTradeActionConfirmation] = React.useState<{ tradeId: number; action: string } | null>(null)
  const [isSigningOut, setIsSigningOut] = React.useState(false)
  const [signOutFeedback, setSignOutFeedback] = React.useState('')
  const statsToday = new Date()
  const [statsMonth, setStatsMonth] = React.useState(statsToday.getMonth())
  const [statsYear, setStatsYear] = React.useState(statsToday.getFullYear())
  const [viewDate, setViewDate] = React.useState(() => {
    const stored = window.localStorage.getItem('atlas.mobile-preview.month')
    const parsed = stored ? new Date(`${stored}-01T12:00:00`) : new Date()
    return Number.isNaN(parsed.getTime()) ? new Date(new Date().getFullYear(), new Date().getMonth(), 1) : new Date(parsed.getFullYear(), parsed.getMonth(), 1)
  })
  const [selectedDay, setSelectedDay] = React.useState(() => {
    const stored = Number(window.localStorage.getItem('atlas.mobile-preview.day'))
    return Number.isInteger(stored) && stored >= 1 && stored <= 31 ? stored : new Date().getDate()
  })
  const [activeListDate, setActiveListDate] = React.useState(() => dateKey(new Date(viewDate.getFullYear(), viewDate.getMonth(), selectedDay)))
  const [weekIndex, setWeekIndex] = React.useState(() => {
    const monthStart = new Date(viewDate.getFullYear(), viewDate.getMonth(), 1)
    const monthGridStart = new Date(monthStart.getFullYear(), monthStart.getMonth(), 1 - monthStart.getDay())
    const gridStart = new Date(monthGridStart.getFullYear(), monthGridStart.getMonth(), monthGridStart.getDate() - 28)
    const selectedDate = new Date(viewDate.getFullYear(), viewDate.getMonth(), selectedDay)
    return Math.max(0, Math.min(15, weekOffset(selectedDate, gridStart)))
  })
  const contentRef = React.useRef<HTMLElement>(null)
  const programmaticScrollRef = React.useRef(false)
  const pendingWeekJumpRef = React.useRef(false)
  const [selectedRegionId, setSelectedRegionId] = React.useState<number | null>(() => {
    const stored = Number(window.localStorage.getItem('atlas.mobile-preview.region-id'))
    return Number.isFinite(stored) && stored !== 0 ? stored : null
  })
  const [selectedDomainIds, setSelectedDomainIds] = React.useState<number[]>(() => {
    try {
      const value = JSON.parse(window.localStorage.getItem('atlas.mobile-preview.domain-ids') || '[]')
      return Array.isArray(value) ? value.filter((id): id is number => typeof id === 'number') : []
    } catch {
      return []
    }
  })

  React.useEffect(() => window.localStorage.setItem('atlas.mobile-preview.page', activePage), [activePage])
  React.useEffect(() => window.localStorage.setItem('atlas.mobile-preview.display', display), [display])
  React.useEffect(() => window.localStorage.setItem('atlas.mobile-preview.list-scope', listScheduleScope), [listScheduleScope])
  React.useEffect(() => window.localStorage.setItem('atlas.mobile-preview.month', `${viewDate.getFullYear()}-${String(viewDate.getMonth() + 1).padStart(2, '0')}`), [viewDate])
  React.useEffect(() => {
    if (selectedRegionId != null) window.localStorage.setItem('atlas.mobile-preview.region-id', String(selectedRegionId))
  }, [selectedRegionId])
  React.useEffect(() => window.localStorage.setItem('atlas.mobile-preview.domain-ids', JSON.stringify(selectedDomainIds)), [selectedDomainIds])
  React.useEffect(() => window.localStorage.setItem('atlas.mobile-preview.day', String(selectedDay)), [selectedDay])

  const loadTradeRequests = React.useCallback(async () => {
    setIsLoadingTradeRequests(true)
    try {
      const response = await fetch(`${API_BASE}/shift-trades/`, { credentials: 'include', cache: 'no-store' })
      const data = await response.json().catch(() => [])
      if (!response.ok || !Array.isArray(data)) throw new Error('Trade requests could not be loaded.')
      setTradeRequests(data)
    } catch (error) {
      setTradeCenterFeedback(error instanceof Error ? error.message : 'Trade requests could not be loaded.')
    } finally {
      setIsLoadingTradeRequests(false)
    }
  }, [])

  React.useEffect(() => { void loadTradeRequests() }, [loadTradeRequests])

  React.useEffect(() => {
    let cancelled = false
    const loadPublishedSchedule = async () => {
      setScheduleDataState('loading')
      try {
        const [scheduleResponse, domainsResponse] = await Promise.all([
          fetch(`${API_BASE}/published-schedule/`, { credentials: 'include', cache: 'no-store' }),
          fetch(`${API_BASE}/domains/?active=true&accessible=true`, { credentials: 'include', cache: 'no-store' }),
        ])
        if (!scheduleResponse.ok || !domainsResponse.ok) throw new Error('Published schedule could not be loaded.')
        const [schedule, domains] = await Promise.all([scheduleResponse.json(), domainsResponse.json()])
        if (cancelled) return
        setApiShifts(Array.isArray(schedule) ? schedule : [])
        setAvailableDomains(Array.isArray(domains) ? domains : [])
        setScheduleDataState('live')
        setScheduleDataMessage('')
      } catch (error) {
        if (cancelled) return
        setApiShifts([])
        setAvailableDomains([])
        setScheduleDataState('demo')
        setScheduleDataMessage(error instanceof Error ? error.message : 'Published schedule could not be loaded.')
      }
    }
    void loadPublishedSchedule()
    return () => { cancelled = true }
  }, [])

  const selectPrimary = (page: PreviewPage) => {
    if (page === 'more') {
      setShowMore(true)
      setShowScope(false)
      return
    }
    setActivePage(page)
    setShowMore(false)
    setShowScope(false)
  }

  const openTradeRequests = () => {
    setShowMore(false)
    setShowTradeRequests(true)
    setTradeCenterFeedback('')
    void loadTradeRequests()
    void fetch(`${API_BASE}/shift-trades/mark-seen/`, {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: '{}',
    }).then((response) => {
      if (response.ok) setTradeRequests((current) => current.map((trade) => ({ ...trade, is_unseen: false })))
    })
  }

  const signOut = async () => {
    if (isSigningOut) return
    setIsSigningOut(true)
    setSignOutFeedback('')
    setShowMore(false)
    setShowScope(false)
    setShowTradeRequests(false)
    setSelectedShift(null)
    setShiftWorkflow(null)
    try {
      await logout()
      setApiShifts([])
      setTradeRequests([])
      setAvailableDomains([])
    } catch (error) {
      setSignOutFeedback(error instanceof Error ? error.message : 'Unable to sign out. Please try again.')
      setShowMore(true)
    } finally {
      setIsSigningOut(false)
    }
  }

  const effectiveDomains = scheduleDataState === 'live' ? availableDomains : demoDomains
  const regions = Array.from(new Map(effectiveDomains.map((domain) => [domain.region, { id: domain.region, name: domain.region_name }])).values())
  const domainsForRegion = effectiveDomains.filter((domain) => domain.region === selectedRegionId)

  React.useEffect(() => {
    if (!effectiveDomains.length) return
    const validRegion = regions.some((region) => region.id === selectedRegionId)
    const nextRegionId = validRegion && selectedRegionId != null ? selectedRegionId : regions[0].id
    if (nextRegionId !== selectedRegionId) setSelectedRegionId(nextRegionId)
    const regionDomainIds = effectiveDomains.filter((domain) => domain.region === nextRegionId).map((domain) => domain.id)
    const validSelectedIds = selectedDomainIds.filter((id) => regionDomainIds.includes(id))
    if (!validSelectedIds.length || validSelectedIds.length !== selectedDomainIds.length) {
      setSelectedDomainIds(validSelectedIds.length ? validSelectedIds : regionDomainIds)
    }
  }, [scheduleDataState, availableDomains, selectedRegionId])

  const changeRegion = (regionId: number) => {
    setSelectedRegionId(regionId)
    setSelectedDomainIds(effectiveDomains.filter((domain) => domain.region === regionId).map((domain) => domain.id))
  }
  const toggleDomain = (domainId: number) => {
    setSelectedDomainIds((current) => current.includes(domainId)
      ? current.length === 1 ? current : current.filter((value) => value !== domainId)
      : [...current, domainId])
  }
  const moveMonth = (direction: -1 | 1) => {
    const nextMonth = new Date(viewDate.getFullYear(), viewDate.getMonth() + direction, 1)
    const today = new Date()
    const nextDay = nextMonth.getFullYear() === today.getFullYear() && nextMonth.getMonth() === today.getMonth() ? today.getDate() : 1
    const nextGridStart = new Date(nextMonth.getFullYear(), nextMonth.getMonth(), 1 - nextMonth.getDay() - 28)
    const nextSelectedDate = new Date(nextMonth.getFullYear(), nextMonth.getMonth(), nextDay)
    setViewDate(nextMonth)
    setSelectedDay(nextDay)
    setActiveListDate(dateKey(new Date(nextMonth.getFullYear(), nextMonth.getMonth(), nextDay)))
    setWeekIndex(Math.max(0, Math.min(15, weekOffset(nextSelectedDate, nextGridStart))))
  }

  const currentMonth = viewDate.toLocaleDateString('en-US', { month: 'long', year: 'numeric' })
  const currentUserName = [user?.first_name, user?.last_name].filter(Boolean).join(' ') || 'My account'
  const statsYears = React.useMemo(() => {
    const publishedYears = apiShifts.map((shift) => Number(shift.date.slice(0, 4))).filter(Number.isFinite)
    const minimum = Math.min(statsToday.getFullYear() - 3, ...publishedYears)
    const maximum = Math.max(statsToday.getFullYear() + 3, ...publishedYears)
    return Array.from({ length: maximum - minimum + 1 }, (_, index) => minimum + index)
  }, [apiShifts])
  const myStatsShifts = apiShifts
    .filter((shift) => (
      shift.physician === user?.physician_id
      && shift.status?.toUpperCase() !== 'OPEN'
      && Number(shift.date.slice(0, 4)) === statsYear
      && Number(shift.date.slice(5, 7)) === statsMonth + 1
    ))
    .sort((left, right) => left.date.localeCompare(right.date) || left.start_time.localeCompare(right.start_time))
  const myStatsTotalHours = myStatsShifts.reduce((total, shift) => total + shiftHours(shift.start_time, shift.end_time), 0)
  const myStatsNightHours = myStatsShifts.reduce((total, shift) => total + (shift.is_night ? shiftHours(shift.start_time, shift.end_time) : 0), 0)
  const monthStart = new Date(viewDate.getFullYear(), viewDate.getMonth(), 1)
  const monthGridStart = new Date(monthStart.getFullYear(), monthStart.getMonth(), 1 - monthStart.getDay())
  const daysInMonth = new Date(viewDate.getFullYear(), viewDate.getMonth() + 1, 0).getDate()
  const monthCellCount = Math.ceil((monthStart.getDay() + daysInMonth) / 7) * 7
  const monthDays = Array.from({ length: monthCellCount }, (_, index) => {
    const day = index - monthStart.getDay() + 1
    return day >= 1 && day <= daysInMonth ? day : null
  })
  const listStart = new Date(monthGridStart.getFullYear(), monthGridStart.getMonth(), monthGridStart.getDate() - 28)
  const listDates = Array.from({ length: 112 }, (_, index) => new Date(listStart.getFullYear(), listStart.getMonth(), listStart.getDate() + index))
  const weekDays = listDates.slice(weekIndex * 7, (weekIndex * 7) + 7)
  const listMonthLabel = (weekDays[3] || monthStart).toLocaleDateString('en-US', { month: 'long', year: 'numeric' })

  React.useEffect(() => {
    if (selectedDay > daysInMonth) {
      setSelectedDay(daysInMonth)
      setActiveListDate(dateKey(new Date(viewDate.getFullYear(), viewDate.getMonth(), daysInMonth)))
    }
  }, [daysInMonth, selectedDay])
  const liveShiftsByDate = apiShifts.reduce<Record<string, PreviewShift[]>>((result, shift) => {
    if (selectedDomainIds.length && !selectedDomainIds.includes(shift.domain)) return result
    const isMine = shift.physician != null && shift.physician === user?.physician_id
    const status = shift.status?.toUpperCase()
    const postingMode = shift.posting_mode === 'PICKUP' ? 'pickup' : shift.posting_mode === 'TRADE_ONLY' ? 'trade' : undefined
    const previewShift: PreviewShift = {
      facility: shift.facility_short_name || shift.facility_name,
      time: `${formatClockValue(shift.start_time)}–${formatClockValue(shift.end_time)}`,
      physician: shift.physician_name || 'Open shift',
      domain: shift.domain_name,
      tone: isMine ? 'own' : status === 'OPEN' ? 'open' : postingMode ? 'available' : 'standard',
      isMine,
      postingMode,
      assignmentId: shift.id,
      instanceId: shift.shift_instance_id,
      physicianId: shift.physician,
      startDateTime: shift.start_datetime,
      startTime: shift.start_time,
      endTime: shift.end_time,
      isSplit: shift.is_split,
      canPickUp: shift.can_pick_up,
      pickupIneligibilityReason: shift.pickup_ineligibility_reason,
      postingComment: shift.posting_comment,
      domainId: shift.domain,
    }
    result[shift.date] = [...(result[shift.date] || []), previewShift]
    return result
  }, {})
  const shiftsForDate = (date: Date) => {
    if (scheduleDataState === 'live') return liveShiftsByDate[dateKey(date)] || []
    const today = new Date()
    return date.getFullYear() === today.getFullYear() && date.getMonth() === today.getMonth()
      ? (sampleShiftsByDay[date.getDate()] || []).filter((shift) => {
        const demoDomain = demoDomains.find((domain) => domain.name === shift.domain)
        return !selectedDomainIds.length || (demoDomain && selectedDomainIds.includes(demoDomain.id))
      })
      : []
  }
  const datesWithMyShifts = listDates.filter((date) => shiftsForDate(date).some((shift) => shift.isMine))
  const visibleListDates = listScheduleScope === 'mine' ? datesWithMyShifts : listDates
  const todayStart = new Date(new Date().getFullYear(), new Date().getMonth(), new Date().getDate())
  const nextOwnShiftDate = datesWithMyShifts.find((date) => date.getTime() >= todayStart.getTime()) || datesWithMyShifts[datesWithMyShifts.length - 1]
  const markerStateForDate = (date: Date) => {
    const shifts = shiftsForDate(date)
    return {
      hasOwnShift: shifts.some((shift) => shift.isMine),
      hasOtherStatus: listScheduleScope === 'group' && shifts.some((shift) => !shift.isMine && shift.tone && shift.tone !== 'standard'),
    }
  }
  const jumpToListDate = (date: Date) => {
    const key = dateKey(date)
    programmaticScrollRef.current = true
    const content = contentRef.current
    const target = document.getElementById(`mobile-list-day-${key}`)
    if (content && target) content.scrollTo({ top: Math.max(0, target.offsetTop - 128), behavior: 'auto' })
    setActiveListDate(key)
    const nextWeekIndex = Math.max(0, Math.min(15, weekOffset(date, listStart)))
    setWeekIndex(nextWeekIndex)
  }
  const moveListWeek = (direction: -1 | 1) => {
    pendingWeekJumpRef.current = true
    setWeekIndex((current) => Math.max(0, Math.min(15, current + direction)))
  }

  React.useEffect(() => {
    if (display !== 'list' || !pendingWeekJumpRef.current) return
    pendingWeekJumpRef.current = false
    const date = listDates[weekIndex * 7]
    const key = dateKey(date)
    programmaticScrollRef.current = true
    setActiveListDate(key)
    window.requestAnimationFrame(() => {
      const content = contentRef.current
      const target = document.getElementById(`mobile-list-day-${key}`)
      if (content && target) content.scrollTo({ top: Math.max(0, target.offsetTop - 128), behavior: 'auto' })
    })
  }, [weekIndex])

  React.useEffect(() => {
    if (display !== 'list') return
    const content = contentRef.current
    if (!content) return
    const syncWeekToScroll = () => {
      if (programmaticScrollRef.current) return
      const threshold = content.getBoundingClientRect().top + 132
      const anchors = Array.from(content.querySelectorAll<HTMLElement>('.mobile-list-day-anchor'))
      let active = anchors[0]
      for (const anchor of anchors) {
        if (anchor.getBoundingClientRect().top <= threshold) active = anchor
        else break
      }
      const key = active?.dataset.date
      if (!key) return
      setActiveListDate((current) => current === key ? current : key)
      const date = new Date(`${key}T12:00:00`)
      const nextWeekIndex = Math.max(0, Math.min(15, weekOffset(date, listStart)))
      setWeekIndex((current) => current === nextWeekIndex ? current : nextWeekIndex)
    }
    const resumeManualTracking = () => { programmaticScrollRef.current = false }
    content.addEventListener('scroll', syncWeekToScroll, { passive: true })
    content.addEventListener('wheel', resumeManualTracking, { passive: true })
    content.addEventListener('touchstart', resumeManualTracking, { passive: true })
    window.requestAnimationFrame(() => {
      const target = document.getElementById(`mobile-list-day-${activeListDate}`)
      if (target) {
        programmaticScrollRef.current = true
        content.scrollTo({ top: Math.max(0, target.offsetTop - 128), behavior: 'auto' })
      }
    })
    return () => {
      content.removeEventListener('scroll', syncWeekToScroll)
      content.removeEventListener('wheel', resumeManualTracking)
      content.removeEventListener('touchstart', resumeManualTracking)
    }
  }, [display])

  React.useLayoutEffect(() => {
    if (display !== 'list') return
    programmaticScrollRef.current = true
    const content = contentRef.current
    const targetDate = listScheduleScope === 'mine' && nextOwnShiftDate ? nextOwnShiftDate : new Date(`${activeListDate}T12:00:00`)
    const targetKey = dateKey(targetDate)
    if (targetKey !== activeListDate) setActiveListDate(targetKey)
    if (listScheduleScope === 'mine') {
      const nextWeekIndex = Math.max(0, Math.min(15, weekOffset(targetDate, listStart)))
      setWeekIndex((current) => current === nextWeekIndex ? current : nextWeekIndex)
    }
    const target = document.getElementById(`mobile-list-day-${targetKey}`)
    if (content && target) content.scrollTop = Math.max(0, target.offsetTop - 128)
  }, [display, listScheduleScope])
  const openShift = (shift: SelectedPreviewShift) => {
    setSelectedShift(shift)
    setShiftWorkflow(null)
    setWorkflowMessage('')
    setPickupFeedback('')
    setPickupRequiresForce(false)
    setPostingFeedback('')
    setTradeOfferFeedback('')
    setTradeOfferRequiresForce(false)
    setTradeFeedback('')
    setTradeRequiresForce(false)
    setTradeTarget('')
    setSplitFeedback('')
    setUnsplitFeedback('')
    setUnsplitRequiresForce(false)
    setUnsplitRequiresUser(false)
    setUnsplitUserId('')
    setReassignUserId('')
    setReassignFeedback('')
    setReassignRequiresForce(false)
    setAssignUserId('')
    setAssignFeedback('')
    setAssignRequiresForce(false)
    setTimesFeedback('')
    setTimesRequireForce(false)
    setSwapTargetId('')
    setSwapFeedback('')
    setSwapRequiresForce(false)
    setOpenShiftFeedback('')
  }
  const closeShift = () => {
    setSelectedShift(null)
    setShiftWorkflow(null)
    setWorkflowMessage('')
    setPickupFeedback('')
    setPickupRequiresForce(false)
    setPostingFeedback('')
    setTradeOfferFeedback('')
    setTradeOfferRequiresForce(false)
    setTradeFeedback('')
    setTradeRequiresForce(false)
    setSplitFeedback('')
    setUnsplitFeedback('')
    setUnsplitRequiresForce(false)
    setUnsplitRequiresUser(false)
    setUnsplitUserId('')
    setReassignUserId('')
    setReassignFeedback('')
    setReassignRequiresForce(false)
    setAssignUserId('')
    setAssignFeedback('')
    setAssignRequiresForce(false)
    setTimesFeedback('')
    setTimesRequireForce(false)
    setSwapTargetId('')
    setSwapFeedback('')
    setSwapRequiresForce(false)
    setOpenShiftFeedback('')
  }
  const showWorkflow = (workflow: ShiftWorkflow) => {
    setShiftWorkflow(workflow)
    setWorkflowMessage('')
    setPickupFeedback('')
    setPickupRequiresForce(false)
    setPostingFeedback('')
    setTradeOfferFeedback('')
    setTradeOfferRequiresForce(false)
    setTradeFeedback('')
    setTradeRequiresForce(false)
    setSplitFeedback('')
    setUnsplitFeedback('')
    setUnsplitRequiresForce(false)
    setReassignFeedback('')
    setReassignRequiresForce(false)
    setAssignFeedback('')
    setAssignRequiresForce(false)
    setTimesFeedback('')
    setTimesRequireForce(false)
    setSwapFeedback('')
    setSwapRequiresForce(false)
    if (workflow === 'post' && selectedShift) {
      setPostingMode(selectedShift.postingMode || 'pickup')
      setPostComment(selectedShift.postingComment || '')
    }
    if (workflow === 'offer') {
      setTradeOfferAssignmentId('')
      setTradeOfferNote('')
      setEligibleTradeOffers([])
      setIsLoadingTradeOffers(true)
    }
    if (workflow === 'trade') {
      setTradePartner('')
      setTradeTarget('')
      setTradeNote('')
      setTradeOptions([])
      setIsLoadingTradeOptions(true)
    }
    if (workflow === 'split') setSplitTime('12:00')
    if (workflow === 'reassign') {
      setReassignUserId('')
      setReassignUsers([])
    }
    if (workflow === 'assign') {
      setAssignUserId('')
      setAssignUsers([])
    }
    if (workflow === 'times' && selectedShift) {
      setActualStartTime(selectedShift.startTime?.slice(0, 5) || '')
      setActualEndTime(selectedShift.endTime?.slice(0, 5) || '')
    }
    if (workflow === 'swap') {
      setSwapTargetId('')
      setSwapOptions([])
      setIsLoadingSwapOptions(true)
    }
  }

  React.useEffect(() => {
    if (shiftWorkflow !== 'reassign' || !selectedShift?.domainId) return
    if (scheduleDataState !== 'live') {
      setIsLoadingReassignUsers(false)
      return
    }
    let cancelled = false
    const loadUsers = async () => {
      setIsLoadingReassignUsers(true)
      setReassignFeedback('')
      try {
        const response = await fetch(`${API_BASE}/physicians/`, { credentials: 'include', cache: 'no-store' })
        const data = await response.json().catch(() => [])
        if (!response.ok || !Array.isArray(data)) throw new Error('Unable to load users for this shift.')
        if (cancelled) return
        setReassignUsers(data.filter((candidate: UnsplitUserOption) => (
          candidate.active
          && candidate.domain_memberships.some((membership) => (
            membership.domain_id === selectedShift.domainId
            && membership.active
            && membership.clinically_active
          ))
        )))
      } catch (error) {
        if (!cancelled) setReassignFeedback(error instanceof Error ? error.message : 'Unable to load users for this shift.')
      } finally {
        if (!cancelled) setIsLoadingReassignUsers(false)
      }
    }
    void loadUsers()
    return () => { cancelled = true }
  }, [shiftWorkflow, selectedShift?.domainId, scheduleDataState])

  React.useEffect(() => {
    if (shiftWorkflow !== 'assign' || !selectedShift?.domainId) return
    if (scheduleDataState !== 'live') {
      setIsLoadingAssignUsers(false)
      return
    }
    let cancelled = false
    const loadUsers = async () => {
      setIsLoadingAssignUsers(true)
      setAssignFeedback('')
      try {
        const response = await fetch(`${API_BASE}/physicians/`, { credentials: 'include', cache: 'no-store' })
        const data = await response.json().catch(() => [])
        if (!response.ok || !Array.isArray(data)) throw new Error('Unable to load users for this shift.')
        if (cancelled) return
        setAssignUsers(data.filter((candidate: UnsplitUserOption) => (
          candidate.active
          && candidate.domain_memberships.some((membership) => (
            membership.domain_id === selectedShift.domainId
            && membership.active
            && membership.clinically_active
          ))
        )))
      } catch (error) {
        if (!cancelled) setAssignFeedback(error instanceof Error ? error.message : 'Unable to load users for this shift.')
      } finally {
        if (!cancelled) setIsLoadingAssignUsers(false)
      }
    }
    void loadUsers()
    return () => { cancelled = true }
  }, [shiftWorkflow, selectedShift?.domainId, scheduleDataState])

  React.useEffect(() => {
    if (shiftWorkflow !== 'swap' || !selectedShift) return
    if (scheduleDataState !== 'live') {
      setSwapOptions([
        { id: -301, physicianId: -31, physicianName: 'Taylor', date: '2026-10-24', facility: 'Roper', time: '7a–4p' },
      ])
      setIsLoadingSwapOptions(false)
      return
    }
    if (selectedShift.assignmentId == null) {
      setSwapFeedback('Only an assigned shift can be swapped.')
      setIsLoadingSwapOptions(false)
      return
    }
    let cancelled = false
    const loadSwapOptions = async () => {
      setIsLoadingSwapOptions(true)
      setSwapFeedback('')
      try {
        const response = await fetch(`${API_BASE}/schedule-assignments/${selectedShift.assignmentId}/trade-options/`, { credentials: 'include', cache: 'no-store' })
        const data = await response.json().catch(() => [])
        if (!response.ok) throw new Error(data.detail || 'Unable to load swap options.')
        if (cancelled) return
        const options: TradeOption[] = (Array.isArray(data) ? data : []).map((option) => ({
          id: option.id,
          physicianId: option.physician_id,
          physicianName: option.physician_name,
          date: option.date,
          facility: option.facility,
          time: `${formatClockValue(option.start_time)}–${formatClockValue(option.end_time)}`,
        }))
        setSwapOptions(options)
        if (!options.length) setSwapFeedback('No swap options are currently available.')
      } catch (error) {
        if (!cancelled) setSwapFeedback(error instanceof Error ? error.message : 'Unable to load swap options.')
      } finally {
        if (!cancelled) setIsLoadingSwapOptions(false)
      }
    }
    void loadSwapOptions()
    return () => { cancelled = true }
  }, [shiftWorkflow, selectedShift?.assignmentId, scheduleDataState])

  React.useEffect(() => {
    if (shiftWorkflow !== 'trade' || !selectedShift) return
    if (scheduleDataState !== 'live') {
      setTradeOptions([
        { id: -201, physicianId: -21, physicianName: 'Moe', date: '2026-10-13', facility: 'Roper', time: '6a–3p' },
        { id: -202, physicianId: -22, physicianName: 'Taylor', date: '2026-10-15', facility: 'Berkeley', time: '4p–1a' },
      ])
      setIsLoadingTradeOptions(false)
      return
    }
    if (selectedShift.assignmentId == null || !selectedShift.isMine || Boolean(selectedShift.startDateTime && shiftHasStarted(selectedShift.startDateTime))) {
      setTradeOptions([])
      setTradeFeedback('This shift is not eligible to start a trade proposal.')
      setIsLoadingTradeOptions(false)
      return
    }

    let cancelled = false
    const loadTradeOptions = async () => {
      setIsLoadingTradeOptions(true)
      setTradeFeedback('')
      try {
        const response = await fetch(`${API_BASE}/schedule-assignments/${selectedShift.assignmentId}/trade-options/`, { credentials: 'include', cache: 'no-store' })
        const data = await response.json().catch(() => [])
        if (!response.ok) throw new Error(data.detail || 'Unable to load eligible trade options.')
        if (cancelled) return
        const options: TradeOption[] = (Array.isArray(data) ? data : []).map((option) => ({
          id: option.id,
          physicianId: option.physician_id,
          physicianName: option.physician_name,
          date: option.date,
          facility: option.facility,
          time: `${formatClockValue(option.start_time)}–${formatClockValue(option.end_time)}`,
        }))
        setTradeOptions(options)
        if (!options.length) setTradeFeedback('No eligible trade options are currently available.')
      } catch (error) {
        if (!cancelled) setTradeFeedback(error instanceof Error ? error.message : 'Unable to load eligible trade options.')
      } finally {
        if (!cancelled) setIsLoadingTradeOptions(false)
      }
    }
    void loadTradeOptions()
    return () => { cancelled = true }
  }, [shiftWorkflow, selectedShift?.assignmentId, selectedShift?.isMine, selectedShift?.startDateTime, scheduleDataState])

  React.useEffect(() => {
    if (shiftWorkflow !== 'offer' || !selectedShift) return
    if (scheduleDataState !== 'live') {
      setEligibleTradeOffers([
        { id: -101, date: '2026-10-14', facility: 'St Francis', time: '9a–6p' },
        { id: -102, date: '2026-10-15', facility: 'Berkeley', time: '4p–1a' },
      ])
      setIsLoadingTradeOffers(false)
      return
    }
    if (selectedShift.assignmentId == null) {
      setEligibleTradeOffers([])
      setTradeOfferFeedback('This shift is not available for a trade request.')
      setIsLoadingTradeOffers(false)
      return
    }

    let cancelled = false
    const loadEligibleOffers = async () => {
      setIsLoadingTradeOffers(true)
      setTradeOfferFeedback('')
      setEligibleTradeOffers([])
      const candidates = apiShifts.filter((shift) => (
        shift.id != null
        && shift.physician === user?.physician_id
        && shift.domain === selectedShift.domainId
        && !shiftHasStarted(shift.start_datetime)
      ))
      try {
        const checkedCandidates = await Promise.all(candidates.map(async (candidate) => {
          const response = await fetch(`${API_BASE}/schedule-assignments/${candidate.id}/trade-options/`, { credentials: 'include', cache: 'no-store' })
          const options = await response.json().catch(() => [])
          if (!response.ok || !Array.isArray(options)) return null
          return options.some((option: { id?: number }) => option.id === selectedShift.assignmentId) ? candidate : null
        }))
        if (cancelled) return
        const options = checkedCandidates
          .filter((candidate): candidate is APIShift => candidate != null && candidate.id != null)
          .sort((left, right) => left.start_datetime.localeCompare(right.start_datetime))
          .map((candidate) => ({
            id: candidate.id as number,
            date: candidate.date,
            facility: candidate.facility_short_name || candidate.facility_name,
            time: `${formatClockValue(candidate.start_time)}–${formatClockValue(candidate.end_time)}`,
          }))
        setEligibleTradeOffers(options)
        if (!options.length) setTradeOfferFeedback('No eligible shifts are currently available to offer.')
      } catch (error) {
        if (!cancelled) setTradeOfferFeedback(error instanceof Error ? error.message : 'Unable to load eligible trade offers.')
      } finally {
        if (!cancelled) setIsLoadingTradeOffers(false)
      }
    }
    void loadEligibleOffers()
    return () => { cancelled = true }
  }, [shiftWorkflow, selectedShift?.assignmentId, selectedShift?.domainId, scheduleDataState, apiShifts, user?.physician_id])

  const confirmPickup = async () => {
    if (!selectedShift || isPickupSaving) return
    if (scheduleDataState !== 'live') {
      setWorkflowMessage('Preview complete — this shift would now be assigned to you.')
      return
    }
    if (selectedShift.assignmentId == null && selectedShift.canPickUp === false) {
      setPickupFeedback(selectedShift.pickupIneligibilityReason || 'This shift is not currently eligible for pickup.')
      return
    }
    if (selectedShift.instanceId == null || (selectedShift.assignmentId != null && selectedShift.assignmentId <= 0)) {
      setPickupFeedback('This shift cannot be identified for pickup. Refresh the schedule and try again.')
      return
    }

    setIsPickupSaving(true)
    setPickupFeedback('')
    try {
      const isOpenShift = selectedShift.assignmentId == null
      const endpoint = isOpenShift
        ? `${API_BASE}/shift-instances/${selectedShift.instanceId}/pickup/`
        : `${API_BASE}/shift-trades/`
      const body: Record<string, unknown> = isOpenShift
        ? {}
        : { target_assignment_id: selectedShift.assignmentId, offered_assignment_id: null }
      if (pickupRequiresForce) body.force = true
      const response = await fetch(endpoint, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      const data = await response.json().catch(() => ({}))
      if (response.status === 409 && data.requires_confirmation && !data.requires_physician_selection) {
        setPickupRequiresForce(true)
        setPickupFeedback(data.detail || 'This pickup creates a scheduling conflict. Confirm again to continue.')
        return
      }
      if (!response.ok) throw new Error(data.detail || 'Unable to pick up this shift.')

      const scheduleResponse = await fetch(`${API_BASE}/published-schedule/`, { credentials: 'include', cache: 'no-store' })
      if (!scheduleResponse.ok) throw new Error('The pickup was recorded, but the updated schedule could not be loaded.')
      const refreshedSchedule = await scheduleResponse.json()
      const nextShifts: APIShift[] = Array.isArray(refreshedSchedule) ? refreshedSchedule : []
      setApiShifts(nextShifts)
      setScheduleDataState('live')
      const pickedUpShift = nextShifts.find((shift) => (
        shift.shift_instance_id === selectedShift.instanceId && shift.physician === user?.physician_id
      ))
      setScheduleActionMessage(pickedUpShift ? 'Shift picked up and added to My schedule.' : 'Pickup request submitted successfully.')
      closeShift()
    } catch (error) {
      setPickupFeedback(error instanceof Error ? error.message : 'Unable to pick up this shift.')
    } finally {
      setIsPickupSaving(false)
    }
  }

  const savePosting = async (mode: 'PICKUP' | 'TRADE_ONLY' | 'CLOSE') => {
    if (!selectedShift || isPostingSaving) return
    if (scheduleDataState !== 'live') {
      setWorkflowMessage(mode === 'CLOSE' ? 'Preview complete — this would remove the posting.' : 'Preview complete — this would post the shift.')
      return
    }
    if (selectedShift.assignmentId == null) {
      setPostingFeedback('Only an assigned shift can be posted.')
      return
    }
    if (selectedShift.startDateTime && shiftHasStarted(selectedShift.startDateTime)) {
      setPostingFeedback('This shift has already started and can no longer be posted.')
      return
    }

    setIsPostingSaving(true)
    setPostingFeedback('')
    try {
      const response = await fetch(`${API_BASE}/schedule-assignments/${selectedShift.assignmentId}/posting/`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode, ...(mode === 'CLOSE' ? {} : { comment: postComment }) }),
      })
      const data = await response.json().catch(() => ({}))
      if (!response.ok) throw new Error(data.detail || 'Unable to update this shift posting.')

      const scheduleResponse = await fetch(`${API_BASE}/published-schedule/`, { credentials: 'include', cache: 'no-store' })
      if (!scheduleResponse.ok) throw new Error('The posting was updated, but the schedule could not be reloaded.')
      const refreshedSchedule = await scheduleResponse.json()
      setApiShifts(Array.isArray(refreshedSchedule) ? refreshedSchedule : [])
      setScheduleDataState('live')
      setScheduleActionMessage(mode === 'CLOSE' ? 'Shift posting removed.' : mode === 'PICKUP' ? 'Shift posted for pickup.' : 'Shift posted for trade only.')
      closeShift()
    } catch (error) {
      setPostingFeedback(error instanceof Error ? error.message : 'Unable to update this shift posting.')
    } finally {
      setIsPostingSaving(false)
    }
  }

  const splitShift = async () => {
    if (!selectedShift || !splitTime || isSplitSaving) return
    if (scheduleDataState !== 'live') {
      setWorkflowMessage('Preview complete — this would split the shift.')
      return
    }
    if (selectedShift.assignmentId == null) {
      setSplitFeedback('Only an assigned shift can be split.')
      return
    }

    setIsSplitSaving(true)
    setSplitFeedback('')
    try {
      const response = await fetch(`${API_BASE}/schedule-assignments/${selectedShift.assignmentId}/split/`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ split_time: splitTime }),
      })
      const data = await response.json().catch(() => ({}))
      if (!response.ok) throw new Error(data.detail || 'Unable to split this shift.')

      const scheduleResponse = await fetch(`${API_BASE}/published-schedule/`, { credentials: 'include', cache: 'no-store' })
      if (!scheduleResponse.ok) throw new Error('The shift was split, but the updated schedule could not be loaded.')
      const refreshedSchedule = await scheduleResponse.json()
      setApiShifts(Array.isArray(refreshedSchedule) ? refreshedSchedule : [])
      setScheduleDataState('live')
      setScheduleActionMessage(data.detail || 'Shift split successfully.')
      closeShift()
    } catch (error) {
      setSplitFeedback(error instanceof Error ? error.message : 'Unable to split this shift.')
    } finally {
      setIsSplitSaving(false)
    }
  }

  const loadUnsplitUsers = async () => {
    if (!selectedShift?.domainId) return
    try {
      const response = await fetch(`${API_BASE}/physicians/`, { credentials: 'include', cache: 'no-store' })
      const data = await response.json().catch(() => [])
      if (!response.ok || !Array.isArray(data)) throw new Error('Unable to load users for this shift.')
      setUnsplitUsers(data.filter((candidate: UnsplitUserOption) => (
        candidate.active
        && candidate.domain_memberships.some((membership) => (
          membership.domain_id === selectedShift.domainId
          && membership.active
          && membership.clinically_active
        ))
      )))
    } catch (error) {
      setUnsplitFeedback(error instanceof Error ? error.message : 'Unable to load users for this shift.')
    }
  }

  const unsplitShift = async () => {
    if (!selectedShift || isUnsplitSaving) return
    if (scheduleDataState !== 'live') {
      setWorkflowMessage('Preview complete — this would recombine the split shift.')
      return
    }
    if (selectedShift.assignmentId == null) {
      setShiftWorkflow('unsplit')
      setUnsplitFeedback('Only an assigned split shift can be recombined.')
      return
    }

    setIsUnsplitSaving(true)
    setUnsplitFeedback('')
    try {
      const body: Record<string, unknown> = {}
      if (unsplitUserId !== '') body.physician_id = unsplitUserId
      if (unsplitRequiresForce) body.force = true
      const response = await fetch(`${API_BASE}/schedule-assignments/${selectedShift.assignmentId}/unsplit/`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      const data = await response.json().catch(() => ({}))
      if (response.status === 409 && data.requires_physician_selection) {
        setShiftWorkflow('unsplit')
        setUnsplitRequiresUser(true)
        setUnsplitRequiresForce(false)
        setUnsplitFeedback(data.detail || 'Choose the user who should receive the recombined shift.')
        await loadUnsplitUsers()
        return
      }
      if (response.status === 409 && data.requires_confirmation) {
        setShiftWorkflow('unsplit')
        setUnsplitRequiresForce(true)
        setUnsplitFeedback(data.detail || 'Confirm that you want to recombine this shift.')
        return
      }
      if (!response.ok) {
        setShiftWorkflow('unsplit')
        throw new Error(data.detail || 'Unable to recombine this shift.')
      }

      const scheduleResponse = await fetch(`${API_BASE}/published-schedule/`, { credentials: 'include', cache: 'no-store' })
      if (!scheduleResponse.ok) throw new Error('The shift was recombined, but the updated schedule could not be loaded.')
      const refreshedSchedule = await scheduleResponse.json()
      setApiShifts(Array.isArray(refreshedSchedule) ? refreshedSchedule : [])
      setScheduleDataState('live')
      setScheduleActionMessage(data.detail || 'Shift recombined successfully.')
      closeShift()
    } catch (error) {
      setUnsplitFeedback(error instanceof Error ? error.message : 'Unable to recombine this shift.')
    } finally {
      setIsUnsplitSaving(false)
    }
  }

  const reassignShift = async () => {
    if (!selectedShift || reassignUserId === '' || isReassignSaving) return
    if (scheduleDataState !== 'live') {
      setWorkflowMessage('Preview complete — this would change the scheduled user.')
      return
    }
    if (selectedShift.assignmentId == null) {
      setReassignFeedback('Only an assigned shift can be reassigned.')
      return
    }

    setIsReassignSaving(true)
    setReassignFeedback('')
    try {
      const body: Record<string, unknown> = { physician_id: reassignUserId }
      if (reassignRequiresForce) body.force = true
      const response = await fetch(`${API_BASE}/schedule-assignments/${selectedShift.assignmentId}/reassign/`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      const data = await response.json().catch(() => ({}))
      if (response.status === 409 && data.requires_confirmation && !data.requires_physician_selection) {
        setReassignRequiresForce(true)
        setReassignFeedback(data.detail || 'This reassignment creates a scheduling conflict. Confirm again to continue.')
        return
      }
      if (!response.ok) throw new Error(data.detail || 'Unable to change the scheduled user.')

      const scheduleResponse = await fetch(`${API_BASE}/published-schedule/`, { credentials: 'include', cache: 'no-store' })
      if (!scheduleResponse.ok) throw new Error('The scheduled user was changed, but the updated schedule could not be loaded.')
      const refreshedSchedule = await scheduleResponse.json()
      setApiShifts(Array.isArray(refreshedSchedule) ? refreshedSchedule : [])
      setScheduleDataState('live')
      setScheduleActionMessage(data.detail || 'Scheduled user changed.')
      closeShift()
    } catch (error) {
      setReassignFeedback(error instanceof Error ? error.message : 'Unable to change the scheduled user.')
    } finally {
      setIsReassignSaving(false)
    }
  }

  const assignOpenShift = async () => {
    if (!selectedShift || assignUserId === '' || isAssignSaving) return
    if (scheduleDataState !== 'live') {
      setWorkflowMessage('Preview complete — this would assign the open shift.')
      return
    }
    if (selectedShift.instanceId == null) {
      setAssignFeedback('This shift cannot be identified. Refresh the schedule and try again.')
      return
    }

    setIsAssignSaving(true)
    setAssignFeedback('')
    try {
      const body: Record<string, unknown> = { physician_id: assignUserId }
      if (assignRequiresForce) body.force = true
      const response = await fetch(`${API_BASE}/shift-instances/${selectedShift.instanceId}/assign/`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      const data = await response.json().catch(() => ({}))
      if (response.status === 409 && data.requires_confirmation && !data.requires_physician_selection) {
        setAssignRequiresForce(true)
        setAssignFeedback(data.detail || 'This assignment creates a scheduling conflict. Confirm again to continue.')
        return
      }
      if (!response.ok) throw new Error(data.detail || 'Unable to assign this open shift.')

      const scheduleResponse = await fetch(`${API_BASE}/published-schedule/`, { credentials: 'include', cache: 'no-store' })
      if (!scheduleResponse.ok) throw new Error('The shift was assigned, but the schedule could not be reloaded.')
      const refreshedSchedule = await scheduleResponse.json()
      setApiShifts(Array.isArray(refreshedSchedule) ? refreshedSchedule : [])
      setScheduleDataState('live')
      setScheduleActionMessage(data.detail || 'Open shift assigned.')
      closeShift()
    } catch (error) {
      setAssignFeedback(error instanceof Error ? error.message : 'Unable to assign this open shift.')
    } finally {
      setIsAssignSaving(false)
    }
  }

  const updateShiftTimes = async () => {
    if (!selectedShift || !actualStartTime || !actualEndTime || isTimesSaving) return
    if (scheduleDataState !== 'live') {
      setWorkflowMessage('Preview complete — this would update the shift times.')
      return
    }
    if (selectedShift.instanceId == null) {
      setTimesFeedback('This shift cannot be identified. Refresh the schedule and try again.')
      return
    }

    setIsTimesSaving(true)
    setTimesFeedback('')
    try {
      const body: Record<string, unknown> = { start_time: actualStartTime, end_time: actualEndTime }
      if (timesRequireForce) body.force = true
      const response = await fetch(`${API_BASE}/shift-instances/${selectedShift.instanceId}/times/`, {
        method: 'PATCH',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      const data = await response.json().catch(() => ({}))
      if (response.status === 409 && data.requires_confirmation && !data.requires_physician_selection) {
        setTimesRequireForce(true)
        setTimesFeedback(data.detail || 'These times create a scheduling conflict. Confirm again to continue.')
        return
      }
      if (!response.ok) throw new Error(data.detail || 'Unable to update the shift times.')

      const scheduleResponse = await fetch(`${API_BASE}/published-schedule/`, { credentials: 'include', cache: 'no-store' })
      if (!scheduleResponse.ok) throw new Error('The shift times were updated, but the schedule could not be reloaded.')
      const refreshedSchedule = await scheduleResponse.json()
      setApiShifts(Array.isArray(refreshedSchedule) ? refreshedSchedule : [])
      setScheduleDataState('live')
      setScheduleActionMessage(data.detail || 'Actual shift times updated.')
      closeShift()
    } catch (error) {
      setTimesFeedback(error instanceof Error ? error.message : 'Unable to update the shift times.')
    } finally {
      setIsTimesSaving(false)
    }
  }

  const swapScheduledUsers = async () => {
    if (!selectedShift || swapTargetId === '' || isSwapSaving) return
    if (scheduleDataState !== 'live') {
      setWorkflowMessage('Preview complete — this would swap the scheduled users.')
      return
    }
    if (selectedShift.assignmentId == null) {
      setSwapFeedback('Only an assigned shift can be swapped.')
      return
    }

    setIsSwapSaving(true)
    setSwapFeedback('')
    try {
      const body: Record<string, unknown> = { target_assignment_id: swapTargetId }
      if (swapRequiresForce) body.force = true
      const response = await fetch(`${API_BASE}/schedule-assignments/${selectedShift.assignmentId}/swap/`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      const data = await response.json().catch(() => ({}))
      if (response.status === 409 && data.requires_confirmation && !data.requires_physician_selection) {
        setSwapRequiresForce(true)
        setSwapFeedback(data.detail || 'This swap creates a scheduling conflict. Confirm again to continue.')
        return
      }
      if (!response.ok) throw new Error(data.detail || 'Unable to swap the scheduled users.')

      const scheduleResponse = await fetch(`${API_BASE}/published-schedule/`, { credentials: 'include', cache: 'no-store' })
      if (!scheduleResponse.ok) throw new Error('The users were swapped, but the schedule could not be reloaded.')
      const refreshedSchedule = await scheduleResponse.json()
      setApiShifts(Array.isArray(refreshedSchedule) ? refreshedSchedule : [])
      setScheduleDataState('live')
      setScheduleActionMessage(data.detail || 'Scheduled users swapped.')
      closeShift()
    } catch (error) {
      setSwapFeedback(error instanceof Error ? error.message : 'Unable to swap the scheduled users.')
    } finally {
      setIsSwapSaving(false)
    }
  }

  const openScheduledShift = async () => {
    if (!selectedShift || isOpenShiftSaving) return
    if (scheduleDataState !== 'live') {
      setWorkflowMessage('Preview complete — this shift would now be open.')
      return
    }
    if (selectedShift.assignmentId == null) {
      setOpenShiftFeedback('This shift is already open.')
      return
    }

    setIsOpenShiftSaving(true)
    setOpenShiftFeedback('')
    try {
      const response = await fetch(`${API_BASE}/schedule-assignments/${selectedShift.assignmentId}/open/`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: '{}',
      })
      const data = await response.json().catch(() => ({}))
      if (!response.ok) throw new Error(data.detail || 'Unable to open this shift.')

      const scheduleResponse = await fetch(`${API_BASE}/published-schedule/`, { credentials: 'include', cache: 'no-store' })
      if (!scheduleResponse.ok) throw new Error('The shift was opened, but the schedule could not be reloaded.')
      const refreshedSchedule = await scheduleResponse.json()
      setApiShifts(Array.isArray(refreshedSchedule) ? refreshedSchedule : [])
      setScheduleDataState('live')
      setScheduleActionMessage(data.detail || 'Shift opened.')
      closeShift()
    } catch (error) {
      setOpenShiftFeedback(error instanceof Error ? error.message : 'Unable to open this shift.')
    } finally {
      setIsOpenShiftSaving(false)
    }
  }

  const submitTradeOffer = async () => {
    if (!selectedShift || tradeOfferAssignmentId === '' || isTradeOfferSaving) return
    if (scheduleDataState !== 'live') {
      setWorkflowMessage('Preview complete — this would send the trade request.')
      return
    }
    if (selectedShift.assignmentId == null) {
      setTradeOfferFeedback('This shift is no longer available for trade.')
      return
    }

    setIsTradeOfferSaving(true)
    setTradeOfferFeedback('')
    try {
      const body: Record<string, unknown> = {
        target_assignment_id: selectedShift.assignmentId,
        offered_assignment_id: tradeOfferAssignmentId,
        note: tradeOfferNote,
      }
      if (tradeOfferRequiresForce) body.force = true
      const response = await fetch(`${API_BASE}/shift-trades/`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      const data = await response.json().catch(() => ({}))
      if (response.status === 409 && data.requires_confirmation && !data.requires_physician_selection) {
        setTradeOfferRequiresForce(true)
        setTradeOfferFeedback(data.detail || 'This trade creates a scheduling conflict. Confirm again to continue.')
        return
      }
      if (!response.ok) throw new Error(data.detail || 'Unable to send this trade request.')

      const scheduleResponse = await fetch(`${API_BASE}/published-schedule/`, { credentials: 'include', cache: 'no-store' })
      if (!scheduleResponse.ok) throw new Error('The trade request was sent, but the schedule could not be reloaded.')
      const refreshedSchedule = await scheduleResponse.json()
      setApiShifts(Array.isArray(refreshedSchedule) ? refreshedSchedule : [])
      setScheduleDataState('live')
      setScheduleActionMessage('Trade request sent successfully.')
      closeShift()
    } catch (error) {
      setTradeOfferFeedback(error instanceof Error ? error.message : 'Unable to send this trade request.')
    } finally {
      setIsTradeOfferSaving(false)
    }
  }

  const submitTradeProposal = async () => {
    if (!selectedShift || tradeTarget === '' || isTradeSaving) return
    if (scheduleDataState !== 'live') {
      setWorkflowMessage('Preview complete — this would send the trade proposal.')
      return
    }
    if (selectedShift.assignmentId == null) {
      setTradeFeedback('This shift is no longer eligible for a trade proposal.')
      return
    }

    setIsTradeSaving(true)
    setTradeFeedback('')
    try {
      const body: Record<string, unknown> = {
        offered_assignment_id: selectedShift.assignmentId,
        target_assignment_id: tradeTarget,
        note: tradeNote,
      }
      if (tradeRequiresForce) body.force = true
      const response = await fetch(`${API_BASE}/shift-trades/`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      const data = await response.json().catch(() => ({}))
      if (response.status === 409 && data.requires_confirmation && !data.requires_physician_selection) {
        setTradeRequiresForce(true)
        setTradeFeedback(data.detail || 'This trade creates a scheduling conflict. Confirm again to continue.')
        return
      }
      if (!response.ok) throw new Error(data.detail || 'Unable to send this trade proposal.')

      const scheduleResponse = await fetch(`${API_BASE}/published-schedule/`, { credentials: 'include', cache: 'no-store' })
      if (!scheduleResponse.ok) throw new Error('The proposal was sent, but the schedule could not be reloaded.')
      const refreshedSchedule = await scheduleResponse.json()
      setApiShifts(Array.isArray(refreshedSchedule) ? refreshedSchedule : [])
      setScheduleDataState('live')
      setScheduleActionMessage('Trade proposal sent successfully.')
      closeShift()
    } catch (error) {
      setTradeFeedback(error instanceof Error ? error.message : 'Unable to send this trade proposal.')
    } finally {
      setIsTradeSaving(false)
    }
  }

  const runTradeAction = async (tradeId: number, action: 'accept' | 'decline' | 'approve' | 'reject' | 'cancel') => {
    const actionKey = `${tradeId}:${action}`
    if (tradeActionSaving) return
    setTradeActionSaving(actionKey)
    setTradeCenterFeedback('')
    try {
      const force = tradeActionConfirmation?.tradeId === tradeId && tradeActionConfirmation.action === action
      const response = await fetch(`${API_BASE}/shift-trades/${tradeId}/${action}/`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(force ? { force: true } : {}),
      })
      const data = await response.json().catch(() => ({}))
      if (response.status === 409 && data.requires_confirmation && !data.requires_physician_selection) {
        setTradeActionConfirmation({ tradeId, action })
        setTradeCenterFeedback(data.detail || 'This action creates a scheduling conflict. Confirm again to continue.')
        return
      }
      if (!response.ok) throw new Error(data.detail || 'Unable to update this trade request.')

      const [scheduleResponse] = await Promise.all([
        fetch(`${API_BASE}/published-schedule/`, { credentials: 'include', cache: 'no-store' }),
        loadTradeRequests(),
      ])
      if (!scheduleResponse.ok) throw new Error('The request was updated, but the schedule could not be reloaded.')
      const refreshedSchedule = await scheduleResponse.json()
      setApiShifts(Array.isArray(refreshedSchedule) ? refreshedSchedule : [])
      setScheduleDataState('live')
      setTradeActionConfirmation(null)
      setScheduleActionMessage(data.detail || 'Trade request updated.')
    } catch (error) {
      setTradeCenterFeedback(error instanceof Error ? error.message : 'Unable to update this trade request.')
    } finally {
      setTradeActionSaving('')
    }
  }

  const selectedShiftHasStarted = Boolean(selectedShift?.startDateTime && shiftHasStarted(selectedShift.startDateTime))
  const tradePartners = Array.from(new Map(tradeOptions.map((option) => [option.physicianId, option.physicianName])).entries())
    .sort((left, right) => left[1].localeCompare(right[1]))
  const selectedPartnerTradeOptions = tradePartner === '' ? [] : tradeOptions.filter((option) => option.physicianId === tradePartner)
  const unseenTradeCount = tradeRequests.filter((trade) => trade.is_unseen).length
  const actionableTradeCount = tradeRequests.filter((trade) => trade.can_accept || trade.can_cancel || trade.can_review).length

  return (
    <div className="mobile-preview-workbench">
      <header className="mobile-preview-heading">
        <div><strong>Atlas mobile preview</strong><span>Local prototype · nothing has been published</span></div>
        <a href="/">Return to desktop Atlas</a>
      </header>

      <div className="mobile-preview-phone" aria-label="Atlas mobile schedule preview">
        <main ref={contentRef} className="mobile-preview-content">
          {activePage === 'schedule' ? (
            <>
              <header className="mobile-schedule-toolbar">
                <div className="mobile-display-switcher" role="group" aria-label="Schedule display">
                  <button type="button" className={display === 'list' ? 'active' : ''} onClick={() => setDisplay('list')}>List</button>
                  <button type="button" className={display === 'month' ? 'active' : ''} onClick={() => setDisplay('month')}>Month</button>
                </div>
                <div className="mobile-schedule-view-switcher" role="group" aria-label="Schedule selection">
                  <button type="button" className={listScheduleScope === 'group' ? 'active' : ''} onClick={() => setListScheduleScope('group')}>Group</button>
                  <button type="button" className={listScheduleScope === 'mine' ? 'active' : ''} onClick={() => setListScheduleScope('mine')}>My schedule</button>
                </div>
                <div className="mobile-month-heading">
                  <button type="button" aria-label="Previous month" onClick={() => moveMonth(-1)}>‹</button><strong>{display === 'list' ? listMonthLabel : currentMonth}</strong><button type="button" aria-label="Next month" onClick={() => moveMonth(1)}>›</button>
                </div>
              </header>

              {scheduleDataState === 'loading' && <div className="mobile-data-notice">Loading published schedule…</div>}
              {scheduleDataState === 'demo' && <div className="mobile-data-notice warning">Demo schedule shown · {scheduleDataMessage}</div>}
              {scheduleActionMessage && <div className="mobile-data-notice success">{scheduleActionMessage}<button type="button" onClick={() => setScheduleActionMessage('')} aria-label="Dismiss message">×</button></div>}

              {display === 'month' ? (
                <section className="mobile-month-preview" aria-label={`${currentMonth} calendar preview`}>
                  <div className="mobile-shift-legend" aria-label="Shift colors">
                    <span><i className="own" />My shifts</span>{listScheduleScope === 'group' && <span><i className="other-status" />Available or open</span>}
                  </div>
                  <div className="mobile-weekdays">{['S','M','T','W','T','F','S'].map((day, index) => <span key={`${day}-${index}`}>{day}</span>)}</div>
                  <div className="mobile-month-grid">
                    {monthDays.map((day, index) => {
                      const calendarDate = day ? new Date(viewDate.getFullYear(), viewDate.getMonth(), day) : null
                      const { hasOwnShift, hasOtherStatus } = calendarDate ? markerStateForDate(calendarDate) : { hasOwnShift: false, hasOtherStatus: false }
                      const today = new Date()
                      const isToday = calendarDate != null && dateKey(calendarDate) === dateKey(today)
                      return <button type="button" key={index} className={`${isToday ? 'today ' : ''}${day === selectedDay ? 'selected' : ''}`} disabled={day == null} onClick={() => day && setSelectedDay(day)}>
                        {day && <><span>{day}</span>{(hasOwnShift || hasOtherStatus) && <i className="mobile-date-shift-dots">{hasOwnShift && <b className="own" aria-label="My shift" />}{hasOtherStatus && <b className="other-status" aria-label="Available or open shift" />}</i>}</>}
                      </button>
                    })}
                  </div>
                  <PreviewShiftList date={new Date(viewDate.getFullYear(), viewDate.getMonth(), selectedDay)} shifts={shiftsForDate(new Date(viewDate.getFullYear(), viewDate.getMonth(), selectedDay)).filter((shift) => listScheduleScope === 'group' || shift.isMine)} onSelect={openShift} currentUserName={currentUserName} prioritizeCalendarShifts />
                </section>
              ) : (
                <section className="mobile-list-preview" aria-label="List view preview">
                  <div className="mobile-week-navigator" aria-label="Week navigation">
                    <button type="button" className="week-step" aria-label="Previous week" onClick={() => moveListWeek(-1)}>‹</button>
                    {weekDays.map((date) => {
                      const key = dateKey(date)
                      const { hasOwnShift, hasOtherStatus } = markerStateForDate(date)
                      const adjacentMonth = date.getMonth() !== weekDays[3].getMonth()
                      return <button type="button" key={key} data-week-date={key} className={`${key === activeListDate ? 'active ' : ''}${key === dateKey(new Date()) ? 'today ' : ''}${adjacentMonth ? 'adjacent' : ''}`} onClick={() => jumpToListDate(date)} aria-label={`Jump to ${date.toLocaleDateString('en-US', { weekday: 'long', month: 'long', day: 'numeric' })}`}>
                        <span>{date.toLocaleDateString('en-US', { weekday: 'narrow' })}</span><strong>{date.getDate()}</strong>{adjacentMonth && <small>{date.toLocaleDateString('en-US', { month: 'short' })}</small>}<i>{hasOwnShift && <b className="own" />}{hasOtherStatus && <b className="other-status" />}</i>
                      </button>
                    })}
                    <button type="button" className="week-step" aria-label="Next week" onClick={() => moveListWeek(1)}>›</button>
                  </div>
                  {visibleListDates.map((date) => {
                    const key = dateKey(date)
                    const currentMonthDate = date.getFullYear() === viewDate.getFullYear() && date.getMonth() === viewDate.getMonth()
                    const scheduledShifts = shiftsForDate(date)
                    const allShifts = scheduledShifts.length || scheduleDataState === 'live' ? scheduledShifts : fallbackShifts.slice(0, date.getDate() % 3 === 0 ? 3 : 2)
                    const shifts = listScheduleScope === 'mine' ? allShifts.filter((shift) => shift.isMine) : allShifts
                    return <div id={`mobile-list-day-${key}`} data-date={key} className={`mobile-list-day-anchor${currentMonthDate ? '' : ' adjacent-month'}`} key={key}><PreviewShiftList date={date} shifts={shifts} label={key === dateKey(new Date()) ? 'Today' : 'Schedule'} onSelect={openShift} currentUserName={currentUserName} /></div>
                  })}
                </section>
              )}
            </>
          ) : activePage === 'stats' ? (
            <section className="mobile-stats-page" aria-label="My Stats">
              <header className="mobile-stats-toolbar">
                <strong>My Stats</strong>
                <div>
                  <label><span>Month</span><select aria-label="Stats month" value={statsMonth} onChange={(event) => setStatsMonth(Number(event.target.value))}>{MONTH_NAMES.map((month, index) => <option value={index} key={month}>{month}</option>)}</select></label>
                  <label><span>Year</span><select aria-label="Stats year" value={statsYear} onChange={(event) => setStatsYear(Number(event.target.value))}>{statsYears.map((year) => <option value={year} key={year}>{year}</option>)}</select></label>
                </div>
              </header>
              {scheduleDataState === 'loading' ? <div className="mobile-stats-empty">Loading your statistics…</div> : scheduleDataState !== 'live' ? <div className="mobile-stats-empty">Your statistics could not be loaded.</div> : <>
                <div className="mobile-stats-summary">
                  <div><span>Total hours</span><strong>{displayHours(myStatsTotalHours)}</strong></div>
                  <div><span>Night hours</span><strong>{displayHours(myStatsNightHours)}</strong></div>
                  <div><span>Shifts</span><strong>{myStatsShifts.length}</strong></div>
                </div>
                <div className="mobile-stats-list">
                  {myStatsShifts.map((shift) => <article key={`${shift.shift_instance_id}-${shift.id ?? 'open'}`}>
                    <time dateTime={shift.date}><strong>{new Date(`${shift.date}T12:00:00`).toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' })}</strong><span>{shift.domain_name}</span></time>
                    <div><strong>{shift.facility_short_name || shift.facility_name}</strong><span>{formatClockValue(shift.start_time)}–{formatClockValue(shift.end_time)}</span></div>
                    <b>{displayHours(shiftHours(shift.start_time, shift.end_time))}<small>hours</small></b>
                  </article>)}
                  {!myStatsShifts.length && <div className="mobile-stats-empty">No assigned shifts in {MONTH_NAMES[statsMonth]} {statsYear}.</div>}
                </div>
              </>}
            </section>
          ) : (
            <section className="mobile-secondary-preview">
              <div>{primaryItems.find((item) => item.id === activePage)?.icon}</div>
              <strong>{primaryItems.find((item) => item.id === activePage)?.label}</strong>
              <p>This page is retained as a user-facing destination, without mobile administrative controls.</p>
            </section>
          )}
        </main>

        {selectedShift && (
          <div className="mobile-sheet-backdrop" onClick={closeShift}>
            <section className="mobile-sheet mobile-shift-sheet" onClick={(event) => event.stopPropagation()}>
              <div className="mobile-sheet-handle" />
              <header><div><span>{shiftWorkflow ? 'Shift workflow' : 'Published shift'}</span><strong>{shiftWorkflow === 'post' ? selectedShift.postingMode ? 'Update this posting' : 'Post this shift' : shiftWorkflow === 'trade' ? 'Propose a trade' : shiftWorkflow === 'split' ? 'Split shift' : shiftWorkflow === 'unsplit' ? 'Unsplit shift' : shiftWorkflow === 'reassign' ? 'Change scheduled user' : shiftWorkflow === 'assign' ? 'Assign user' : shiftWorkflow === 'times' ? 'Adjust actual times' : shiftWorkflow === 'swap' ? 'Swap scheduled users' : shiftWorkflow === 'pickup' ? 'Pickup shift' : shiftWorkflow === 'offer' ? 'Offer a trade' : selectedShift.facility}</strong></div><button type="button" onClick={closeShift} aria-label="Close shift details">×</button></header>
              {shiftWorkflow && <button type="button" className="mobile-workflow-back" onClick={() => { setShiftWorkflow(null); setWorkflowMessage('') }}>‹ Shift details</button>}
              <div className="mobile-shift-summary">
                <div><span>Date &amp; time</span><strong>{selectedShift.date.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' })} · {selectedShift.time}</strong></div>
                <div><span>Scheduled</span><strong>{selectedShift.physician} · {selectedShift.domain}</strong></div>
                {selectedShift.postingMode && <div><span>Availability</span><strong>{selectedShift.postingMode === 'pickup' ? 'Posted for pickup' : 'Trade only'}</strong></div>}
                {!selectedShift.isMine && selectedShift.tone === 'open' && <div><span>Status</span><strong>Open shift</strong></div>}
                {selectedShift.postingComment && <div><span>Comment</span><strong>{selectedShift.postingComment}</strong></div>}
              </div>
              {shiftWorkflow === 'post' ? (
                <div className="mobile-workflow-form">
                  <p>Choose how other clinicians may respond to this shift.</p>
                  <label><input type="radio" name="posting-mode" checked={postingMode === 'pickup'} onChange={() => setPostingMode('pickup')} /><span><strong>Available for pickup</strong><small>A clinician may request this shift without offering one back.</small></span></label>
                  <label><input type="radio" name="posting-mode" checked={postingMode === 'trade'} onChange={() => setPostingMode('trade')} /><span><strong>Trade only</strong><small>A clinician must offer one of their shifts in return.</small></span></label>
                  <label className="field">Comment (optional)<textarea value={postComment} maxLength={1000} onChange={(event) => setPostComment(event.target.value)} placeholder="Add a note for other clinicians" /></label>
                  {postingFeedback && <div className="mobile-workflow-message error">{postingFeedback}</div>}
                  <button type="button" className="mobile-workflow-primary" disabled={isPostingSaving} onClick={() => savePosting(postingMode === 'pickup' ? 'PICKUP' : 'TRADE_ONLY')}>{isPostingSaving ? 'Saving…' : selectedShift.postingMode ? 'Update posting' : 'Post shift'}</button>
                  {selectedShift.postingMode && <button type="button" className="mobile-workflow-secondary caution" disabled={isPostingSaving} onClick={() => savePosting('CLOSE')}>Remove posting</button>}
                  {workflowMessage && <div className="mobile-workflow-message">{workflowMessage}</div>}
                </div>
              ) : shiftWorkflow === 'trade' ? (
                <div className="mobile-workflow-form">
                  <label className="field">Trade with<select value={tradePartner} disabled={isLoadingTradeOptions || !tradePartners.length} onChange={(event) => { setTradePartner(Number(event.target.value) || ''); setTradeTarget(''); setTradeFeedback(''); setTradeRequiresForce(false) }}><option value="">{isLoadingTradeOptions ? 'Loading eligible clinicians…' : 'Choose clinician'}</option>{tradePartners.map(([id, name]) => <option value={id} key={id}>{name}</option>)}</select></label>
                  <label className="field">Shift requested<select value={tradeTarget} disabled={tradePartner === ''} onChange={(event) => { setTradeTarget(Number(event.target.value) || ''); setTradeFeedback(''); setTradeRequiresForce(false) }}><option value="">Choose shift</option>{selectedPartnerTradeOptions.map((option) => <option value={option.id} key={option.id}>{new Date(`${option.date}T12:00:00`).toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' })} · {option.facility} · {option.time}</option>)}</select></label>
                  <label className="field">Comment (optional)<textarea value={tradeNote} maxLength={1000} onChange={(event) => setTradeNote(event.target.value)} placeholder="Add a note for the other clinician" /></label>
                  {tradeFeedback && <div className="mobile-workflow-message error">{tradeFeedback}</div>}
                  <button type="button" className="mobile-workflow-primary" disabled={tradeTarget === '' || isLoadingTradeOptions || isTradeSaving} onClick={submitTradeProposal}>{isTradeSaving ? 'Sending…' : tradeRequiresForce ? 'Send trade proposal anyway' : 'Send trade proposal'}</button>
                  {workflowMessage && <div className="mobile-workflow-message">{workflowMessage}</div>}
                </div>
              ) : shiftWorkflow === 'split' ? (
                <div className="mobile-workflow-form">
                  <label className="field">Split at<input type="time" value={splitTime} onChange={(event) => setSplitTime(event.target.value)} /></label>
                  <div className="mobile-split-preview"><span>Resulting portions</span><strong>{selectedShift.startTime ? formatClockValue(selectedShift.startTime) : selectedShift.time.split('–')[0]}–{splitTime ? formatClockValue(splitTime) : '—'} · {splitTime ? formatClockValue(splitTime) : '—'}–{selectedShift.endTime ? formatClockValue(selectedShift.endTime) : selectedShift.time.split('–')[1]}</strong></div>
                  {splitFeedback && <div className="mobile-workflow-message error">{splitFeedback}</div>}
                  <button type="button" className="mobile-workflow-primary" disabled={!splitTime || isSplitSaving} onClick={splitShift}>{isSplitSaving ? 'Splitting…' : 'Split shift'}</button>
                  {workflowMessage && <div className="mobile-workflow-message">{workflowMessage}</div>}
                </div>
              ) : shiftWorkflow === 'unsplit' ? (
                <div className="mobile-workflow-form">
                  <p>Recombine the portions into the original complete shift.</p>
                  {unsplitRequiresUser && <label className="field">User for recombined shift<select value={unsplitUserId} onChange={(event) => { setUnsplitUserId(Number(event.target.value) || ''); setUnsplitRequiresForce(false); setUnsplitFeedback('') }}><option value="">Choose user</option>{unsplitUsers.map((candidate) => <option value={candidate.id} key={candidate.id}>{candidate.display_name || `${candidate.first_name} ${candidate.last_name}`}</option>)}</select></label>}
                  {unsplitFeedback && <div className="mobile-workflow-message error">{unsplitFeedback}</div>}
                  <button type="button" className="mobile-workflow-primary" disabled={isUnsplitSaving || (unsplitRequiresUser && unsplitUserId === '')} onClick={unsplitShift}>{isUnsplitSaving ? 'Recombining…' : unsplitRequiresForce ? 'Confirm unsplit shift' : 'Unsplit shift'}</button>
                  {workflowMessage && <div className="mobile-workflow-message">{workflowMessage}</div>}
                </div>
              ) : shiftWorkflow === 'reassign' ? (
                <div className="mobile-workflow-form">
                  <label className="field">Scheduled user<select value={reassignUserId} disabled={isLoadingReassignUsers} onChange={(event) => { setReassignUserId(Number(event.target.value) || ''); setReassignRequiresForce(false); setReassignFeedback('') }}><option value="">{isLoadingReassignUsers ? 'Loading users…' : 'Choose user'}</option>{reassignUsers.map((candidate) => <option value={candidate.id} key={candidate.id}>{candidate.display_name || `${candidate.first_name} ${candidate.last_name}`}</option>)}</select></label>
                  {reassignFeedback && <div className="mobile-workflow-message error">{reassignFeedback}</div>}
                  <button type="button" className="mobile-workflow-primary" disabled={reassignUserId === '' || isLoadingReassignUsers || isReassignSaving} onClick={reassignShift}>{isReassignSaving ? 'Changing…' : reassignRequiresForce ? 'Confirm change anyway' : 'Change user'}</button>
                  {workflowMessage && <div className="mobile-workflow-message">{workflowMessage}</div>}
                </div>
              ) : shiftWorkflow === 'assign' ? (
                <div className="mobile-workflow-form">
                  <label className="field">Assign to<select value={assignUserId} disabled={isLoadingAssignUsers} onChange={(event) => { setAssignUserId(Number(event.target.value) || ''); setAssignRequiresForce(false); setAssignFeedback('') }}><option value="">{isLoadingAssignUsers ? 'Loading users…' : 'Choose user'}</option>{assignUsers.map((candidate) => <option value={candidate.id} key={candidate.id}>{candidate.display_name || `${candidate.first_name} ${candidate.last_name}`}</option>)}</select></label>
                  {assignFeedback && <div className="mobile-workflow-message error">{assignFeedback}</div>}
                  <button type="button" className="mobile-workflow-primary" disabled={assignUserId === '' || isLoadingAssignUsers || isAssignSaving} onClick={assignOpenShift}>{isAssignSaving ? 'Assigning…' : assignRequiresForce ? 'Confirm assignment anyway' : 'Assign user'}</button>
                  {workflowMessage && <div className="mobile-workflow-message">{workflowMessage}</div>}
                </div>
              ) : shiftWorkflow === 'times' ? (
                <div className="mobile-workflow-form">
                  <label className="field">Actual start time<input type="time" value={actualStartTime} onChange={(event) => { setActualStartTime(event.target.value); setTimesRequireForce(false); setTimesFeedback('') }} /></label>
                  <label className="field">Actual end time<input type="time" value={actualEndTime} onChange={(event) => { setActualEndTime(event.target.value); setTimesRequireForce(false); setTimesFeedback('') }} /></label>
                  {timesFeedback && <div className="mobile-workflow-message error">{timesFeedback}</div>}
                  <button type="button" className="mobile-workflow-primary" disabled={!actualStartTime || !actualEndTime || isTimesSaving} onClick={updateShiftTimes}>{isTimesSaving ? 'Updating…' : timesRequireForce ? 'Confirm times anyway' : 'Update times'}</button>
                  {workflowMessage && <div className="mobile-workflow-message">{workflowMessage}</div>}
                </div>
              ) : shiftWorkflow === 'swap' ? (
                <div className="mobile-workflow-form">
                  <label className="field">Swap with<select value={swapTargetId} disabled={isLoadingSwapOptions} onChange={(event) => { setSwapTargetId(Number(event.target.value) || ''); setSwapRequiresForce(false); setSwapFeedback('') }}><option value="">{isLoadingSwapOptions ? 'Loading shifts…' : 'Choose another user’s shift'}</option>{swapOptions.map((option) => <option value={option.id} key={option.id}>{option.physicianName} · {new Date(`${option.date}T12:00:00`).toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' })} · {option.facility} · {option.time}</option>)}</select></label>
                  {swapFeedback && <div className="mobile-workflow-message error">{swapFeedback}</div>}
                  <button type="button" className="mobile-workflow-primary" disabled={swapTargetId === '' || isLoadingSwapOptions || isSwapSaving} onClick={swapScheduledUsers}>{isSwapSaving ? 'Swapping…' : swapRequiresForce ? 'Confirm swap anyway' : 'Swap users'}</button>
                  {workflowMessage && <div className="mobile-workflow-message">{workflowMessage}</div>}
                </div>
              ) : shiftWorkflow === 'pickup' ? (
                <div className="mobile-workflow-form">
                  <div className="mobile-workflow-warning"><strong>You will be responsible for this shift.</strong><span>Confirm only if you are able to work the complete scheduled time shown above.</span></div>
                  {pickupFeedback && <div className="mobile-workflow-message error">{pickupFeedback}</div>}
                  <button type="button" className="mobile-workflow-primary" disabled={isPickupSaving} onClick={confirmPickup}>{isPickupSaving ? 'Confirming…' : pickupRequiresForce ? 'Confirm pickup anyway' : 'Confirm pickup'}</button>
                  {workflowMessage && <div className="mobile-workflow-message">{workflowMessage}</div>}
                </div>
              ) : shiftWorkflow === 'offer' ? (
                <div className="mobile-workflow-form">
                  <p>This shift is available only in exchange for one of your eligible shifts.</p>
                  <label className="field">Your shift offered<select value={tradeOfferAssignmentId} disabled={isLoadingTradeOffers || !eligibleTradeOffers.length} onChange={(event) => { setTradeOfferAssignmentId(Number(event.target.value) || ''); setTradeOfferFeedback(''); setTradeOfferRequiresForce(false) }}><option value="">{isLoadingTradeOffers ? 'Checking eligible shifts…' : 'Choose one of your shifts'}</option>{eligibleTradeOffers.map((option) => <option value={option.id} key={option.id}>{new Date(`${option.date}T12:00:00`).toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' })} · {option.facility} · {option.time}</option>)}</select></label>
                  <label className="field">Comment (optional)<textarea value={tradeOfferNote} maxLength={1000} onChange={(event) => setTradeOfferNote(event.target.value)} placeholder="Add a note for the other clinician" /></label>
                  {tradeOfferFeedback && <div className="mobile-workflow-message error">{tradeOfferFeedback}</div>}
                  <button type="button" className="mobile-workflow-primary" disabled={tradeOfferAssignmentId === '' || isLoadingTradeOffers || isTradeOfferSaving} onClick={submitTradeOffer}>{isTradeOfferSaving ? 'Sending…' : tradeOfferRequiresForce ? 'Send trade request anyway' : 'Send trade request'}</button>
                  {workflowMessage && <div className="mobile-workflow-message">{workflowMessage}</div>}
                </div>
              ) : user?.can_manage_schedules && !testUserView && selectedShift.tone === 'open' ? (
                <div className="mobile-shift-actions"><strong>Manage open shift</strong><button type="button" onClick={() => showWorkflow('assign')}>Assign user <span>›</span></button></div>
              ) : user?.can_manage_schedules && !testUserView ? (
                <div className="mobile-shift-actions">
                  <strong>Manage this shift</strong>
                  <button type="button" onClick={() => showWorkflow('reassign')}>Change scheduled user <span>›</span></button>
                  <button type="button" onClick={() => showWorkflow('times')}>Adjust actual times <span>›</span></button>
                  <button type="button" onClick={() => showWorkflow('swap')}>Swap scheduled users <span>›</span></button>
                  <button type="button" onClick={() => showWorkflow('split')}>Split shift <span>›</span></button>
                  {selectedShift.isSplit && <button type="button" disabled={isUnsplitSaving} onClick={unsplitShift}>{isUnsplitSaving ? 'Recombining…' : 'Unsplit shift'} <span>›</span></button>}
                  <button type="button" className="caution" disabled={isOpenShiftSaving} onClick={openScheduledShift}>{isOpenShiftSaving ? 'Opening…' : 'Open shift'} <span>›</span></button>
                  {openShiftFeedback && <div className="mobile-workflow-message error">{openShiftFeedback}</div>}
                </div>
              ) : selectedShift.isMine ? (
                <div className="mobile-shift-actions">
                  <strong>Shift actions</strong>
                  {!selectedShiftHasStarted && <button type="button" onClick={() => showWorkflow('post')}>{selectedShift.postingMode ? 'Review posting' : 'Post this shift'} <span>›</span></button>}
                  {!selectedShiftHasStarted && <button type="button" onClick={() => showWorkflow('trade')}>Propose a trade <span>›</span></button>}
                  <button type="button" onClick={() => showWorkflow('split')}>Split shift <span>›</span></button>
                  {selectedShift.isSplit && <button type="button" disabled={isUnsplitSaving} onClick={unsplitShift}>{isUnsplitSaving ? 'Recombining…' : 'Unsplit shift'} <span>›</span></button>}
                </div>
              ) : selectedShift.tone === 'open' && selectedShift.canPickUp === false ? (
                <p className="mobile-shift-readonly">{selectedShift.pickupIneligibilityReason || 'This shift is not currently eligible for pickup.'}</p>
              ) : selectedShift.postingMode === 'pickup' || selectedShift.tone === 'open' ? (
                <div className="mobile-shift-actions"><strong>{selectedShift.tone === 'open' ? 'Open shift' : 'Available shift'}</strong><button type="button" onClick={() => showWorkflow('pickup')}>Pickup Shift <span>›</span></button></div>
              ) : selectedShift.postingMode === 'trade' ? (
                <div className="mobile-shift-actions"><strong>Trade-only shift</strong><button type="button" onClick={() => showWorkflow('offer')}>Offer one of my shifts <span>›</span></button></div>
              ) : <p className="mobile-shift-readonly">No actions are available for this shift.</p>}
            </section>
          </div>
        )}

        {showScope && (
          <div className="mobile-sheet-backdrop" onClick={() => setShowScope(false)}>
            <section className="mobile-sheet" onClick={(event) => event.stopPropagation()}>
              <div className="mobile-sheet-handle" />
              <header><strong>Schedule scope</strong><button type="button" onClick={() => setShowScope(false)} aria-label="Close schedule scope">×</button></header>
              <label>Region<select value={selectedRegionId ?? ''} onChange={(event) => changeRegion(Number(event.target.value))}>{regions.map((region) => <option value={region.id} key={region.id}>{region.name}</option>)}</select></label>
              <fieldset><legend>Domains</legend>{domainsForRegion.map((domain) => <label key={domain.id}><input type="checkbox" checked={selectedDomainIds.includes(domain.id)} onChange={() => toggleDomain(domain.id)} />{domain.name}</label>)}</fieldset>
              <button type="button" className="mobile-sheet-done" onClick={() => setShowScope(false)}>Done</button>
            </section>
          </div>
        )}

        {showTradeRequests && (
          <div className="mobile-sheet-backdrop" onClick={() => setShowTradeRequests(false)}>
            <section className="mobile-sheet mobile-trade-center" onClick={(event) => event.stopPropagation()}>
              <div className="mobile-sheet-handle" />
              <header><div><strong>Trade requests</strong><span>{actionableTradeCount ? `${actionableTradeCount} awaiting action` : 'No requests awaiting action'}</span></div><button type="button" onClick={() => setShowTradeRequests(false)} aria-label="Close trade requests">×</button></header>
              {tradeCenterFeedback && <div className="mobile-workflow-message error">{tradeCenterFeedback}</div>}
              {isLoadingTradeRequests && <div className="mobile-trade-empty">Loading trade requests…</div>}
              {!isLoadingTradeRequests && tradeRequests.map((trade) => {
                const confirmationAction = tradeActionConfirmation?.tradeId === trade.id ? tradeActionConfirmation.action : ''
                return (
                  <article className={`mobile-trade-card${trade.is_unseen ? ' unseen' : ''}`} key={trade.id}>
                    <header><strong>{trade.trade_type === 'PICKUP' ? 'Pickup' : 'Trade'}</strong><span>{trade.status_display}</span></header>
                    <div><span>{trade.offered_assignment.date} · {trade.offered_assignment.facility}</span><strong>{trade.offered_assignment.physician_name}</strong></div>
                    {trade.requested_assignment && <div><span>For {trade.requested_assignment.date} · {trade.requested_assignment.facility}</span><strong>{trade.requested_assignment.physician_name}</strong></div>}
                    {trade.note && <p>{trade.note}</p>}
                    {(trade.can_accept || trade.can_cancel || trade.can_review) && <footer>
                      {trade.can_accept && <><button type="button" disabled={Boolean(tradeActionSaving)} onClick={() => runTradeAction(trade.id, 'accept')}>{tradeActionSaving === `${trade.id}:accept` ? 'Accepting…' : 'Accept'}</button><button type="button" className="secondary" disabled={Boolean(tradeActionSaving)} onClick={() => runTradeAction(trade.id, 'decline')}>{tradeActionSaving === `${trade.id}:decline` ? 'Declining…' : 'Decline'}</button></>}
                      {trade.can_review && <><button type="button" disabled={Boolean(tradeActionSaving)} onClick={() => runTradeAction(trade.id, 'approve')}>{tradeActionSaving === `${trade.id}:approve` ? 'Approving…' : confirmationAction === 'approve' ? 'Confirm approval' : 'Approve'}</button><button type="button" className="secondary" disabled={Boolean(tradeActionSaving)} onClick={() => runTradeAction(trade.id, 'reject')}>{tradeActionSaving === `${trade.id}:reject` ? 'Rejecting…' : 'Reject'}</button></>}
                      {trade.can_cancel && <button type="button" className="secondary" disabled={Boolean(tradeActionSaving)} onClick={() => runTradeAction(trade.id, 'cancel')}>{tradeActionSaving === `${trade.id}:cancel` ? 'Cancelling…' : 'Cancel request'}</button>}
                    </footer>}
                  </article>
                )
              })}
              {!isLoadingTradeRequests && !tradeRequests.length && <div className="mobile-trade-empty">No trade requests</div>}
            </section>
          </div>
        )}

        {showMore && (
          <div className="mobile-sheet-backdrop" onClick={() => setShowMore(false)}>
            <section className="mobile-sheet mobile-more-sheet" onClick={(event) => event.stopPropagation()}>
              <div className="mobile-sheet-handle" />
              <header><strong>More</strong><button type="button" onClick={() => setShowMore(false)} aria-label="Close More menu">×</button></header>
              <div className="mobile-account-summary"><strong>{user?.first_name} {user?.last_name}</strong><span>Signed in on this device</span></div>
              {user?.can_manage_schedules && <div className="mobile-more-view-choice"><span>Interface view</span><div className="mobile-testing-switch" role="group" aria-label="Interface view"><button type="button" className={!testUserView ? 'active' : ''} onClick={() => setTestUserView(false)}>Scheduler</button><button type="button" className={testUserView ? 'active' : ''} onClick={() => setTestUserView(true)}>User</button></div></div>}
              <button type="button" className="mobile-more-scope-action" onClick={openTradeRequests}><span><strong>Trade requests{actionableTradeCount ? ` (${actionableTradeCount})` : ''}</strong><small>{unseenTradeCount ? `${unseenTradeCount} new` : 'Review pickup and trade activity'}</small></span><b>›</b></button>
              <button type="button" className="mobile-more-scope-action" onClick={() => { setShowMore(false); setShowScope(true) }}><span><strong>Schedule scope</strong><small>{regions.find((region) => region.id === selectedRegionId)?.name.replace(' Emergency Physicians', '') || 'Region'} · {selectedDomainIds.length === domainsForRegion.length ? 'All domains' : domainsForRegion.filter((domain) => selectedDomainIds.includes(domain.id)).map((domain) => domain.name).join(', ')}</small></span><b>›</b></button>
              {signOutFeedback && <div className="mobile-workflow-message error">{signOutFeedback}</div>}
              <button type="button" className="mobile-more-action" disabled={isSigningOut} onClick={() => void signOut()}>{isSigningOut ? 'Signing out…' : 'Sign out'} <span>›</span></button>
            </section>
          </div>
        )}

        <nav className="mobile-bottom-nav" aria-label="Primary mobile navigation">
          {primaryItems.map((item) => (
            <button type="button" key={item.id} className={`${(item.id === activePage && !showMore) || (item.id === 'more' && showMore) ? 'active' : ''}${item.id === 'more' && unseenTradeCount ? ' has-attention' : ''}`} aria-current={item.id === activePage && !showMore ? 'page' : undefined} onClick={() => selectPrimary(item.id)}><span aria-hidden="true">{item.icon}</span>{item.label}</button>
          ))}
        </nav>
      </div>

      <aside className="mobile-preview-notes"><strong>Updated direction</strong><ul><li>Light, schedule-first interface</li><li>No branding or identity bar</li><li>Compact List/Month and Group/My schedule controls</li><li>Region and domain scope lives in More</li><li>No Schedule Block or administrative navigation</li><li>Last page and view are remembered on this device</li></ul></aside>
    </div>
  )
}
