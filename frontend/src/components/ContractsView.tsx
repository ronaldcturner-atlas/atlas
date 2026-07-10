import React, { useEffect, useMemo, useState } from 'react'

type Domain = {
  id: number
  name: string
  active: boolean
}

type Facility = {
  id: number
  name: string
  active: boolean
}

type ShiftTemplate = {
  id: number
  facility: number
  name: string
  facility_name: string
  active: boolean
}

type Physician = {
  id: number
  first_name: string
  last_name: string
  display_name: string
  active: boolean
}

type ContractRecord = {
  id: number
  domain: number
  domain_name: string
  name: string
  active: boolean
  facility_ids: number[]
  workload_settings: WorkloadSettings
  shift_settings: ShiftSettings
  night_settings: NightSettings
  weekend_settings: WeekendSettings
  request_settings: RequestSettings
  assigned_users: Array<{ id: number; name: string }>
  assigned_users_count: number
}

type PeriodType = 'WEEK' | 'MONTH' | 'SCHEDULE_BLOCK'
type RuleUnits = 'HOURS' | 'SHIFTS'

const workloadPeriodLabels: Record<PeriodType, string> = {
  WEEK: 'Week',
  MONTH: 'Month',
  SCHEDULE_BLOCK: 'Schedule Block',
}

function workloadRuleSummary(rule: MinMaxRule) {
  const units = rule.units === 'HOURS' ? 'hours' : 'shifts'
  const period = rule.period_type === 'SCHEDULE_BLOCK' ? 'schedule block' : rule.period_type.toLowerCase()
  const behavior = rule.period_type === 'MONTH'
    ? 'Prorated when schedule block covers partial month'
    : rule.period_type === 'WEEK'
      ? 'Evaluated by calendar week overlap'
      : 'Not prorated'
  return { units, period, behavior }
}

type MinMaxRule = {
  id: string
  period_type: PeriodType
  units: RuleUnits
  min_value: string
  max_value: string
  min_penalty_weight: string
  max_penalty_weight: string
  spread_violations: boolean
}

type ShiftRule = {
  id: string
  label: string
  shift_template_ids: number[]
  period_rules: MinMaxRule[]
}

type NightPeriodRule = {
  id: string
  period_type: PeriodType
  min_shifts: string
  max_shifts: string
  min_penalty_weight: string
  max_penalty_weight: string
  spread_violations: boolean
}

type WeekendPeriodRule = {
  id: string
  period_type: 'MONTH' | 'SCHEDULE_BLOCK'
  min_volume: string
  max_volume: string
  min_penalty_weight: string
  max_penalty_weight: string
  spread_violations: boolean
}

type WorkloadSettings = {
  period_rules: MinMaxRule[]
  min_time_off_hours: string
  min_time_off_penalty_weight: string
  circadian_enabled: boolean
  circadian_penalty_weight: string
  min_days_in_row: string
  min_days_in_row_penalty_weight: string
  max_days_in_row: string
  max_days_in_row_penalty_weight: string
  min_same_shifts_in_row: string
  min_same_shifts_in_row_penalty_weight: string
  max_same_shifts_in_row: string
  max_same_shifts_in_row_penalty_weight: string
}

type ShiftSettings = {
  rules: ShiftRule[]
}

type NightSettings = {
  period_rules: NightPeriodRule[]
  min_consecutive_night_shifts: string
  min_consecutive_night_shifts_penalty_weight: string
  max_consecutive_night_shifts: string
  max_consecutive_night_shifts_penalty_weight: string
  days_off_after_night_block: string
  days_off_after_night_block_penalty_weight: string
  days_off_before_next_night_shift: string
  days_off_before_next_night_shift_penalty_weight: string
}

type WeekendSettings = {
  period_rules: WeekendPeriodRule[]
  min_consecutive_weekends: string
  min_consecutive_weekends_penalty_weight: string
  max_consecutive_weekends: string
  max_consecutive_weekends_penalty_weight: string
  min_consecutive_weekend_shifts: string
  min_consecutive_weekend_shifts_penalty_weight: string
  max_consecutive_weekend_shifts: string
  max_consecutive_weekend_shifts_penalty_weight: string
  block_friday_night_before_weekend_off: boolean
  block_friday_night_before_weekend_off_penalty_weight: string
}

type RequestSettings = {
  allow_day_off: boolean
  allow_shift_off: boolean
  allow_day_on: boolean
  allow_shift_on: boolean
  high_request_limit: string
  medium_request_limit: string
  low_request_limit: string
  low_request_unlimited: boolean
  weekend_request_limit: string
  weight_low: string
  weight_medium: string
  weight_high: string
  weight_fixed: string
  weight_weekend: string
}

type ContractFormState = {
  name: string
  domain: string
  active: boolean
  facility_ids: number[]
  assigned_user_ids: number[]
  workload_settings: WorkloadSettings
  shift_settings: ShiftSettings
  night_settings: NightSettings
  weekend_settings: WeekendSettings
  request_settings: RequestSettings
}

type ContractTab = 'summary' | 'workload' | 'facilities' | 'shifts' | 'nights' | 'weekends' | 'users'

const API_BASE = 'http://localhost:8000/api'

let idSeed = 0
function makeId(prefix: string) {
  idSeed += 1
  return `${prefix}-${idSeed}`
}

function makeMinMaxRule(defaultPeriod: PeriodType = 'WEEK'): MinMaxRule {
  return {
    id: makeId('rule'),
    period_type: defaultPeriod,
    units: 'HOURS',
    min_value: '',
    max_value: '',
    min_penalty_weight: '',
    max_penalty_weight: '',
    spread_violations: false,
  }
}

function makeNightRule(defaultPeriod: PeriodType = 'WEEK'): NightPeriodRule {
  return {
    id: makeId('night-rule'),
    period_type: defaultPeriod,
    min_shifts: '',
    max_shifts: '',
    min_penalty_weight: '',
    max_penalty_weight: '',
    spread_violations: false,
  }
}

function makeWeekendRule(defaultPeriod: 'MONTH' | 'SCHEDULE_BLOCK' = 'MONTH'): WeekendPeriodRule {
  return {
    id: makeId('weekend-rule'),
    period_type: defaultPeriod,
    min_volume: '',
    max_volume: '',
    min_penalty_weight: '',
    max_penalty_weight: '',
    spread_violations: false,
  }
}

function makeShiftRule(): ShiftRule {
  return {
    id: makeId('shift-group'),
    label: '',
    shift_template_ids: [],
    period_rules: [makeMinMaxRule('WEEK')],
  }
}

function emptyFormState(): ContractFormState {
  return {
    name: '',
    domain: '',
    active: true,
    facility_ids: [],
    assigned_user_ids: [],
    workload_settings: {
      period_rules: [makeMinMaxRule('WEEK')],
      min_time_off_hours: '',
      min_time_off_penalty_weight: '',
      circadian_enabled: false,
      circadian_penalty_weight: '',
      min_days_in_row: '',
      min_days_in_row_penalty_weight: '',
      max_days_in_row: '',
      max_days_in_row_penalty_weight: '',
      min_same_shifts_in_row: '',
      min_same_shifts_in_row_penalty_weight: '',
      max_same_shifts_in_row: '',
      max_same_shifts_in_row_penalty_weight: '',
    },
    shift_settings: {
      rules: [makeShiftRule()],
    },
    night_settings: {
      period_rules: [makeNightRule('WEEK')],
      min_consecutive_night_shifts: '',
      min_consecutive_night_shifts_penalty_weight: '',
      max_consecutive_night_shifts: '',
      max_consecutive_night_shifts_penalty_weight: '',
      days_off_after_night_block: '',
      days_off_after_night_block_penalty_weight: '',
      days_off_before_next_night_shift: '',
      days_off_before_next_night_shift_penalty_weight: '',
    },
    weekend_settings: {
      period_rules: [makeWeekendRule('MONTH')],
      min_consecutive_weekends: '',
      min_consecutive_weekends_penalty_weight: '',
      max_consecutive_weekends: '',
      max_consecutive_weekends_penalty_weight: '',
      min_consecutive_weekend_shifts: '',
      min_consecutive_weekend_shifts_penalty_weight: '',
      max_consecutive_weekend_shifts: '',
      max_consecutive_weekend_shifts_penalty_weight: '',
      block_friday_night_before_weekend_off: false,
      block_friday_night_before_weekend_off_penalty_weight: '',
    },
    request_settings: {
      allow_day_off: true,
      allow_shift_off: true,
      allow_day_on: true,
      allow_shift_on: true,
      high_request_limit: '',
      medium_request_limit: '',
      low_request_limit: '',
      low_request_unlimited: false,
      weekend_request_limit: '',
      weight_low: '',
      weight_medium: '',
      weight_high: '',
      weight_fixed: '',
      weight_weekend: '',
    },
  }
}

function cloneRule(rule: MinMaxRule): MinMaxRule {
  return { ...rule, id: makeId('rule') }
}

function normalizeMinMaxRule(rule: Partial<MinMaxRule> & Record<string, unknown>) {
  const legacyPenalty = typeof rule.penalty_weight === 'string' ? rule.penalty_weight : ''
  return {
    ...makeMinMaxRule(),
    ...rule,
    min_penalty_weight:
      typeof rule.min_penalty_weight === 'string' ? rule.min_penalty_weight : legacyPenalty,
    max_penalty_weight:
      typeof rule.max_penalty_weight === 'string' ? rule.max_penalty_weight : legacyPenalty,
    id: makeId('rule'),
  }
}

function normalizeNightRule(rule: Partial<NightPeriodRule> & Record<string, unknown>) {
  const legacyPenalty = typeof rule.penalty_weight === 'string' ? rule.penalty_weight : ''
  return {
    ...makeNightRule(),
    ...rule,
    min_penalty_weight:
      typeof rule.min_penalty_weight === 'string' ? rule.min_penalty_weight : legacyPenalty,
    max_penalty_weight:
      typeof rule.max_penalty_weight === 'string' ? rule.max_penalty_weight : legacyPenalty,
    id: makeId('night-rule'),
  }
}

function normalizeWeekendRule(rule: Partial<WeekendPeriodRule> & Record<string, unknown>) {
  const legacyPenalty = typeof rule.penalty_weight === 'string' ? rule.penalty_weight : ''
  return {
    ...makeWeekendRule(),
    ...rule,
    min_penalty_weight:
      typeof rule.min_penalty_weight === 'string' ? rule.min_penalty_weight : legacyPenalty,
    max_penalty_weight:
      typeof rule.max_penalty_weight === 'string' ? rule.max_penalty_weight : legacyPenalty,
    id: makeId('weekend-rule'),
  }
}

function normalizeContractToForm(contract: ContractRecord): ContractFormState {
  const fallback = emptyFormState()

  const workloadSettings = contract.workload_settings || fallback.workload_settings
  const shiftSettings = contract.shift_settings || fallback.shift_settings
  const nightSettings = contract.night_settings || fallback.night_settings
  const weekendSettings = contract.weekend_settings || fallback.weekend_settings
  const requestSettings = contract.request_settings || fallback.request_settings
  const legacyNightPenalty = (nightSettings as Record<string, unknown>).consecutive_penalty_weight
  const legacyWeekendPenalty = (weekendSettings as Record<string, unknown>).consecutive_penalty_weight

  const normalizedNightSettings: NightSettings = {
    ...fallback.night_settings,
    ...nightSettings,
    min_consecutive_night_shifts_penalty_weight:
      nightSettings.min_consecutive_night_shifts_penalty_weight || String(legacyNightPenalty || ''),
    max_consecutive_night_shifts_penalty_weight:
      nightSettings.max_consecutive_night_shifts_penalty_weight || String(legacyNightPenalty || ''),
    days_off_after_night_block_penalty_weight:
      nightSettings.days_off_after_night_block_penalty_weight || String(legacyNightPenalty || ''),
    days_off_before_next_night_shift_penalty_weight:
      nightSettings.days_off_before_next_night_shift_penalty_weight || String(legacyNightPenalty || ''),
  }

  const normalizedWeekendSettings: WeekendSettings = {
    ...fallback.weekend_settings,
    ...weekendSettings,
    min_consecutive_weekends_penalty_weight:
      weekendSettings.min_consecutive_weekends_penalty_weight || String(legacyWeekendPenalty || ''),
    max_consecutive_weekends_penalty_weight:
      weekendSettings.max_consecutive_weekends_penalty_weight || String(legacyWeekendPenalty || ''),
    min_consecutive_weekend_shifts_penalty_weight:
      weekendSettings.min_consecutive_weekend_shifts_penalty_weight || String(legacyWeekendPenalty || ''),
    max_consecutive_weekend_shifts_penalty_weight:
      weekendSettings.max_consecutive_weekend_shifts_penalty_weight || String(legacyWeekendPenalty || ''),
    block_friday_night_before_weekend_off_penalty_weight:
      weekendSettings.block_friday_night_before_weekend_off_penalty_weight || String(legacyWeekendPenalty || ''),
  }

  return {
    name: contract.name,
    domain: String(contract.domain),
    active: contract.active,
    facility_ids: Array.isArray(contract.facility_ids) ? contract.facility_ids : [],
    assigned_user_ids: contract.assigned_users.map((user) => user.id),
    workload_settings: {
      ...fallback.workload_settings,
      ...workloadSettings,
      period_rules: ((workloadSettings.period_rules && workloadSettings.period_rules.length)
        ? workloadSettings.period_rules
        : fallback.workload_settings.period_rules).map((rule) => normalizeMinMaxRule(rule as Partial<MinMaxRule> & Record<string, unknown>)),
    },
    shift_settings: {
      rules: ((shiftSettings.rules && shiftSettings.rules.length)
        ? shiftSettings.rules
        : fallback.shift_settings.rules).map((rule) => ({
        ...makeShiftRule(),
        ...rule,
        id: makeId('shift-group'),
        period_rules: ((rule.period_rules && rule.period_rules.length)
          ? rule.period_rules
          : [makeMinMaxRule('WEEK')]).map((periodRule) => normalizeMinMaxRule(periodRule as Partial<MinMaxRule> & Record<string, unknown>)),
      })),
    },
    night_settings: {
      ...normalizedNightSettings,
      period_rules: ((nightSettings.period_rules && nightSettings.period_rules.length)
        ? nightSettings.period_rules
        : fallback.night_settings.period_rules).map((rule) => normalizeNightRule(rule as Partial<NightPeriodRule> & Record<string, unknown>)),
    },
    weekend_settings: {
      ...normalizedWeekendSettings,
      period_rules: ((weekendSettings.period_rules && weekendSettings.period_rules.length)
        ? weekendSettings.period_rules
        : fallback.weekend_settings.period_rules).map((rule) => normalizeWeekendRule(rule as Partial<WeekendPeriodRule> & Record<string, unknown>)),
    },
    request_settings: {
      ...fallback.request_settings,
      ...requestSettings,
    },
  }
}

function sanitizeFormPayload(formState: ContractFormState) {
  return {
    name: formState.name.trim(),
    domain: Number(formState.domain),
    active: formState.active,
    facility_ids: formState.facility_ids,
    assigned_user_ids: formState.assigned_user_ids,
    workload_settings: {
      ...formState.workload_settings,
      period_rules: formState.workload_settings.period_rules.map(({ id, ...rule }) => rule),
    },
    shift_settings: {
      rules: formState.shift_settings.rules.map((rule) => ({
        label: rule.label,
        shift_template_ids: rule.shift_template_ids,
        period_rules: rule.period_rules.map(({ id, ...periodRule }) => periodRule),
      })),
    },
    night_settings: {
      ...formState.night_settings,
      period_rules: formState.night_settings.period_rules.map(({ id, ...rule }) => rule),
    },
    weekend_settings: {
      ...formState.weekend_settings,
      period_rules: formState.weekend_settings.period_rules.map(({ id, ...rule }) => rule),
    },
    request_settings: formState.request_settings,
  }
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

function physicianLabel(physician: Physician) {
  const fallback = `${physician.first_name} ${physician.last_name}`.trim()
  return physician.display_name || fallback || `User ${physician.id}`
}

function isActiveValue(value: unknown) {
  return value === true || value === 'true' || value === 1 || value === '1'
}

export default function ContractsView() {
  const [contracts, setContracts] = useState<ContractRecord[]>([])
  const [domains, setDomains] = useState<Domain[]>([])
  const [facilities, setFacilities] = useState<Facility[]>([])
  const [shiftTemplates, setShiftTemplates] = useState<ShiftTemplate[]>([])
  const [physicians, setPhysicians] = useState<Physician[]>([])

  const [isLoading, setIsLoading] = useState(true)
  const [isSaving, setIsSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saveNotice, setSaveNotice] = useState<string | null>(null)

  const [showInactive, setShowInactive] = useState(false)
  const [domainFilter, setDomainFilter] = useState('')

  const [editingContractId, setEditingContractId] = useState<number | null>(null)
  const [isModalOpen, setIsModalOpen] = useState(false)
  const [activeTab, setActiveTab] = useState<ContractTab>('summary')
  const [formState, setFormState] = useState<ContractFormState>(emptyFormState)

  const loadContracts = async (includeInactive: boolean, selectedDomain: string) => {
    const params = new URLSearchParams()
    if (includeInactive) {
      params.set('include_inactive', 'true')
    }
    if (selectedDomain) {
      params.set('domain', selectedDomain)
    }

    const response = await fetch(`${API_BASE}/contracts/?${params.toString()}`, { credentials: 'include' })
    if (!response.ok) {
      const message = await getApiErrorMessage(response)
      throw new Error(message ?? 'Unable to load contracts')
    }

    const data = await response.json()
    setContracts(data)
  }

  const loadReferenceData = async () => {
    const [domainsResponse, facilitiesResponse, templatesResponse, physiciansResponse] = await Promise.all([
      fetch(`${API_BASE}/domains/?active=true`, { credentials: 'include' }),
      fetch(`${API_BASE}/facilities/?active=true`, { credentials: 'include' }),
      fetch(`${API_BASE}/shift-templates/?active=true`, { credentials: 'include' }),
      fetch(`${API_BASE}/physicians/`, { credentials: 'include' }),
    ])

    if (!domainsResponse.ok) {
      const message = await getApiErrorMessage(domainsResponse)
      throw new Error(message ?? 'Unable to load domains')
    }
    if (!facilitiesResponse.ok) {
      const message = await getApiErrorMessage(facilitiesResponse)
      throw new Error(message ?? 'Unable to load facilities')
    }
    if (!templatesResponse.ok) {
      const message = await getApiErrorMessage(templatesResponse)
      throw new Error(message ?? 'Unable to load shift templates')
    }
    if (!physiciansResponse.ok) {
      const message = await getApiErrorMessage(physiciansResponse)
      throw new Error(message ?? 'Unable to load users')
    }

    const [domainsData, facilitiesData, templatesData, physiciansData] = await Promise.all([
      domainsResponse.json(),
      facilitiesResponse.json(),
      templatesResponse.json(),
      physiciansResponse.json(),
    ])

    const normalizedActiveFacilities: Facility[] = facilitiesData
      .map((facility: Facility) => ({
        ...facility,
        id: Number(facility.id),
        active: isActiveValue(facility.active),
      }))
      .filter((facility: Facility) => facility.active)

    const activeFacilityIds = new Set(normalizedActiveFacilities.map((facility) => facility.id))

    const normalizedTemplates: ShiftTemplate[] = templatesData
      .map((template: ShiftTemplate) => ({
        ...template,
        id: Number(template.id),
        facility: Number(template.facility),
        active: isActiveValue(template.active),
      }))
      .filter((template: ShiftTemplate) => template.active && activeFacilityIds.has(template.facility))

    setDomains(
      domainsData.filter((domain: Domain) => isActiveValue(domain.active)),
    )
    setFacilities(normalizedActiveFacilities)
    setShiftTemplates(normalizedTemplates)
    setPhysicians(physiciansData)
  }

  const fetchAllData = async (includeInactive: boolean, selectedDomain: string) => {
    try {
      setIsLoading(true)
      setError(null)
      await Promise.all([loadReferenceData(), loadContracts(includeInactive, selectedDomain)])
    } catch (loadError) {
      console.error(loadError)
      setError(loadError instanceof Error ? loadError.message : 'Unable to load contract data.')
    } finally {
      setIsLoading(false)
    }
  }

  useEffect(() => {
    fetchAllData(showInactive, domainFilter)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    loadContracts(showInactive, domainFilter).catch((loadError) => {
      console.error(loadError)
      setError(loadError instanceof Error ? loadError.message : 'Unable to refresh contracts.')
    })
  }, [showInactive, domainFilter])

  const visibleContracts = useMemo(
    () => [...contracts].sort((a, b) => a.name.localeCompare(b.name)),
    [contracts],
  )

  const activeFacilities = useMemo(
    () => facilities.filter((facility) => facility.active),
    [facilities],
  )

  const openCreateModal = () => {
    setEditingContractId(null)
    setActiveTab('summary')
    setFormState(emptyFormState())
    setError(null)
    setSaveNotice(null)
    setIsModalOpen(true)
  }

  const openEditModal = (contract: ContractRecord) => {
    setEditingContractId(contract.id)
    setActiveTab('summary')
    setFormState(normalizeContractToForm(contract))
    setError(null)
    setSaveNotice(null)
    setIsModalOpen(true)
  }

  const closeModal = () => {
    setIsModalOpen(false)
    setEditingContractId(null)
    setActiveTab('summary')
    setFormState(emptyFormState())
    setSaveNotice(null)
  }

  const saveContract = async () => {
    if (!formState.name.trim() || !formState.domain) {
      setError('Name and domain are required.')
      return
    }

    if (!formState.facility_ids.length) {
      setError('Select at least one facility.')
      return
    }

    try {
      setIsSaving(true)
      setError(null)
      setSaveNotice(null)

      const isEditing = editingContractId !== null
      const url = isEditing ? `${API_BASE}/contracts/${editingContractId}/` : `${API_BASE}/contracts/`
      const method = isEditing ? 'PATCH' : 'POST'

      const response = await fetch(url, {
        method,
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify(sanitizeFormPayload(formState)),
      })

      if (!response.ok) {
        const message = await getApiErrorMessage(response)
        throw new Error(message ?? 'Unable to save contract')
      }

      const savedContract = await response.json()
      if (savedContract?.id) {
        setEditingContractId(savedContract.id)
      }

      if (savedContract) {
        setFormState(normalizeContractToForm(savedContract as ContractRecord))
      }

      await loadContracts(showInactive, domainFilter)
      setSaveNotice('Saved successfully.')
    } catch (saveError) {
      console.error(saveError)
      setError(saveError instanceof Error ? saveError.message : 'Unable to save contract changes.')
      setSaveNotice(null)
    } finally {
      setIsSaving(false)
    }
  }

  const duplicateContract = async (contract: ContractRecord) => {
    try {
      setError(null)
      const response = await fetch(`${API_BASE}/contracts/${contract.id}/duplicate/`, {
        method: 'POST',
        credentials: 'include',
      })

      if (!response.ok) {
        const message = await getApiErrorMessage(response)
        throw new Error(message ?? 'Unable to duplicate contract')
      }

      await loadContracts(showInactive, domainFilter)
    } catch (duplicateError) {
      console.error(duplicateError)
      setError(duplicateError instanceof Error ? duplicateError.message : 'Unable to duplicate contract.')
    }
  }

  const toggleActive = async (contract: ContractRecord) => {
    try {
      setError(null)
      const action = contract.active ? 'deactivate' : 'reactivate'
      const response = await fetch(`${API_BASE}/contracts/${contract.id}/${action}/`, {
        method: 'POST',
        credentials: 'include',
      })

      if (!response.ok) {
        const message = await getApiErrorMessage(response)
        throw new Error(message ?? `Unable to ${action} contract`)
      }

      await loadContracts(showInactive, domainFilter)
    } catch (toggleError) {
      console.error(toggleError)
      setError(toggleError instanceof Error ? toggleError.message : 'Unable to update contract status.')
    }
  }

  const selectedDomainId = Number(formState.domain) || null

  const usersForSelectedDomain = useMemo(() => {
    // Domain-specific membership is not modeled yet, so show active physicians.
    return physicians.filter((physician) => physician.active)
  }, [physicians])

  const templatesForSelectedFacilities = useMemo(() => {
    if (!formState.facility_ids.length) {
      return []
    }

    const allowedFacilities = new Set(formState.facility_ids)
    return shiftTemplates.filter((template) => allowedFacilities.has(template.facility))
  }, [formState.facility_ids, shiftTemplates])

  const summaryLabel = useMemo(() => {
    const domainName = domains.find((domain) => domain.id === selectedDomainId)?.name || '-'
    return {
      domainName,
      facilityCount: formState.facility_ids.length,
      usersCount: formState.assigned_user_ids.length,
      workloadRuleCount: formState.workload_settings.period_rules.length,
      shiftRuleCount: formState.shift_settings.rules.length,
      nightRuleCount: formState.night_settings.period_rules.length,
      weekendRuleCount: formState.weekend_settings.period_rules.length,
    }
  }, [domains, formState, selectedDomainId])

  if (isLoading) {
    return <div className="scheduler-loading">Loading contracts...</div>
  }

  return (
    <div className="facilities-view-card">
      <div className="facilities-header contracts-header">
        <h2>Contracts</h2>
        <div className="contracts-toolbar">
          <label className="inline-checkbox-field contracts-inline-checkbox">
            <span>Show inactive</span>
            <input
              type="checkbox"
              checked={showInactive}
              onChange={(event) => setShowInactive(event.target.checked)}
            />
          </label>
          <label className="facility-field contracts-domain-filter">
            <span>Domain</span>
            <select value={domainFilter} onChange={(event) => setDomainFilter(event.target.value)}>
              <option value="">All domains</option>
              {domains.map((domain) => (
                <option key={domain.id} value={domain.id}>
                  {domain.name} {domain.active ? '' : '(Inactive)'}
                </option>
              ))}
            </select>
          </label>
          <button type="button" className="primary-action" onClick={openCreateModal}>
            Create Contract
          </button>
        </div>
      </div>

      {error && <div className="facilities-error">{error}</div>}

      <div className="scheduler-table-wrap">
        <table className="scheduler-table">
          <thead>
            <tr>
              <th>Name</th>
              <th>Domain</th>
              <th>Status</th>
              <th>Users Assigned</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {visibleContracts.map((contract) => (
              <tr key={contract.id}>
                <td>{contract.name}</td>
                <td>{contract.domain_name}</td>
                <td>{contract.active ? 'Active' : 'Inactive'}</td>
                <td>{contract.assigned_users_count}</td>
                <td>
                  <div className="facility-actions">
                    <button type="button" onClick={() => openEditModal(contract)}>
                      Open
                    </button>
                    <button type="button" onClick={() => openEditModal(contract)}>
                      Edit
                    </button>
                    <button type="button" onClick={() => duplicateContract(contract)}>
                      Duplicate
                    </button>
                    <button type="button" onClick={() => toggleActive(contract)}>
                      {contract.active ? 'Deactivate' : 'Reactivate'}
                    </button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {!visibleContracts.length && <div className="empty-state">No contracts found</div>}

      {isModalOpen && (
        <div className="shift-modal-overlay">
          <div className="shift-modal schedule-block-modal contract-modal" onClick={(event) => event.stopPropagation()}>
            <div className="shift-modal-header">
              <h2>{editingContractId ? 'Edit Contract' : 'Create Contract'}</h2>
            </div>

            {error && <div className="facilities-error">{error}</div>}
            {saveNotice && <div className="contract-saved-banner">{saveNotice}</div>}

            <div className="contract-header-grid">
              <label className="facility-field">
                <span>Name</span>
                <input
                  type="text"
                  value={formState.name}
                  onChange={(event) => setFormState((current) => ({ ...current, name: event.target.value }))}
                  placeholder="Full Time 120 Hours"
                />
              </label>

              <label className="facility-field">
                <span>Domain</span>
                <select
                  value={formState.domain}
                  onChange={(event) => setFormState((current) => ({ ...current, domain: event.target.value }))}
                >
                  <option value="">Select domain</option>
                  {domains.map((domain) => (
                    <option key={domain.id} value={domain.id}>
                      {domain.name}
                    </option>
                  ))}
                </select>
              </label>

              <label className="facility-field physician-active-field">
                <span>Active</span>
                <input
                  type="checkbox"
                  checked={formState.active}
                  onChange={(event) => setFormState((current) => ({ ...current, active: event.target.checked }))}
                />
              </label>
            </div>

            <div className="schedule-block-modal-tabs">
              <button type="button" className={activeTab === 'summary' ? 'active' : ''} onClick={() => setActiveTab('summary')}>Summary</button>
              <button type="button" className={activeTab === 'workload' ? 'active' : ''} onClick={() => setActiveTab('workload')}>Workload</button>
              <button type="button" className={activeTab === 'facilities' ? 'active' : ''} onClick={() => setActiveTab('facilities')}>Facilities</button>
              <button type="button" className={activeTab === 'shifts' ? 'active' : ''} onClick={() => setActiveTab('shifts')}>Shifts</button>
              <button type="button" className={activeTab === 'nights' ? 'active' : ''} onClick={() => setActiveTab('nights')}>Nights</button>
              <button type="button" className={activeTab === 'weekends' ? 'active' : ''} onClick={() => setActiveTab('weekends')}>Weekends</button>
              <button type="button" className={activeTab === 'users' ? 'active' : ''} onClick={() => setActiveTab('users')}>Users</button>
            </div>

            <div className="shift-modal-body">
              {activeTab === 'summary' && (
                <div className="contract-summary-grid">
                  <div className="request-existing-note">Domain: {summaryLabel.domainName}</div>
                  <div className="request-existing-note">Status: {formState.active ? 'Active' : 'Inactive'}</div>
                  <div className="request-existing-note">Facilities selected: {summaryLabel.facilityCount}</div>
                  <div className="request-existing-note">Users assigned: {summaryLabel.usersCount}</div>
                  <div className="request-existing-note">Workload period rules: {summaryLabel.workloadRuleCount}</div>
                  <div className="request-existing-note">Shift groups: {summaryLabel.shiftRuleCount}</div>
                  <div className="request-existing-note">Night period rules: {summaryLabel.nightRuleCount}</div>
                  <div className="request-existing-note">Weekend period rules: {summaryLabel.weekendRuleCount}</div>
                </div>
              )}

              {activeTab === 'workload' && (
                <div className="contract-section-stack">
                  <h3>Expected Workload Rules</h3>
                  <div className="request-existing-note">Schedule Block rules apply to the whole schedule block. Month rules are prorated when the schedule block covers only part of a month. Week rules are evaluated by week.</div>
                  <div className="contract-rule-grid">
                    {formState.workload_settings.period_rules.map((rule, index) => {
                      const summary = workloadRuleSummary(rule)
                      return <div key={rule.id} className="contract-rule-row">
                        <div className="request-existing-note">
                          <div><strong>Period:</strong> {workloadPeriodLabels[rule.period_type]}</div>
                          <div><strong>Units:</strong> {rule.units === 'HOURS' ? 'Hours' : 'Shifts'}</div>
                          <div><strong>Range:</strong> {rule.min_value || 'No min'}–{rule.max_value || 'No max'} {summary.units} per {summary.period}</div>
                          <div><strong>Penalty:</strong> min {rule.min_penalty_weight || '0'} / max {rule.max_penalty_weight || '0'}</div>
                          <div><strong>Effective behavior:</strong> {summary.behavior}</div>
                        </div>
                        <div className="contract-minmax-columns">
                          <div className="contract-minmax-column">
                            <div className="contract-minmax-title">Min</div>
                            <label className="facility-field"><span>Min value</span><input type="number" value={rule.min_value} onChange={(event) => setFormState((current) => ({ ...current, workload_settings: { ...current.workload_settings, period_rules: current.workload_settings.period_rules.map((item, itemIndex) => itemIndex === index ? { ...item, min_value: event.target.value } : item) } }))} /></label>
                            <label className="facility-field"><span>Min penalty</span><input type="number" value={rule.min_penalty_weight} onChange={(event) => setFormState((current) => ({ ...current, workload_settings: { ...current.workload_settings, period_rules: current.workload_settings.period_rules.map((item, itemIndex) => itemIndex === index ? { ...item, min_penalty_weight: event.target.value } : item) } }))} /></label>
                          </div>
                          <div className="contract-minmax-column">
                            <div className="contract-minmax-title">Max</div>
                            <label className="facility-field"><span>Max value</span><input type="number" value={rule.max_value} onChange={(event) => setFormState((current) => ({ ...current, workload_settings: { ...current.workload_settings, period_rules: current.workload_settings.period_rules.map((item, itemIndex) => itemIndex === index ? { ...item, max_value: event.target.value } : item) } }))} /></label>
                            <label className="facility-field"><span>Max penalty</span><input type="number" value={rule.max_penalty_weight} onChange={(event) => setFormState((current) => ({ ...current, workload_settings: { ...current.workload_settings, period_rules: current.workload_settings.period_rules.map((item, itemIndex) => itemIndex === index ? { ...item, max_penalty_weight: event.target.value } : item) } }))} /></label>
                          </div>
                        </div>
                        <div className="contract-rule-controls-grid">
                          <label className="facility-field"><span>Period</span><select value={rule.period_type} onChange={(event) => setFormState((current) => ({ ...current, workload_settings: { ...current.workload_settings, period_rules: current.workload_settings.period_rules.map((item, itemIndex) => itemIndex === index ? { ...item, period_type: event.target.value as PeriodType } : item) } }))}><option value="WEEK">Week</option><option value="MONTH">Month</option><option value="SCHEDULE_BLOCK">Schedule Block</option></select></label>
                          <label className="facility-field"><span>Units</span><select value={rule.units} onChange={(event) => setFormState((current) => ({ ...current, workload_settings: { ...current.workload_settings, period_rules: current.workload_settings.period_rules.map((item, itemIndex) => itemIndex === index ? { ...item, units: event.target.value as RuleUnits } : item) } }))}><option value="HOURS">Hours</option><option value="SHIFTS">Shifts</option></select></label>
                          <label className="inline-checkbox-field"><span>Spread violations</span><input type="checkbox" checked={rule.spread_violations} onChange={(event) => setFormState((current) => ({ ...current, workload_settings: { ...current.workload_settings, period_rules: current.workload_settings.period_rules.map((item, itemIndex) => itemIndex === index ? { ...item, spread_violations: event.target.checked } : item) } }))} /></label>
                        </div>
                        <button type="button" className="contract-remove-button" onClick={() => setFormState((current) => ({ ...current, workload_settings: { ...current.workload_settings, period_rules: current.workload_settings.period_rules.filter((_, itemIndex) => itemIndex !== index) } }))} disabled={formState.workload_settings.period_rules.length === 1}>Remove</button>
                      </div>
                    } )}
                  </div>
                  <button type="button" className="primary-action" onClick={() => setFormState((current) => ({ ...current, workload_settings: { ...current.workload_settings, period_rules: [...current.workload_settings.period_rules, makeMinMaxRule('MONTH')] } }))}>Add Workload Period Rule</button>

                  <h3>Additional Workload Rules</h3>
                  <div className="contract-input-grid">
                    <label className="facility-field"><span>Minimum time off between shifts (hours)</span><input type="number" value={formState.workload_settings.min_time_off_hours} onChange={(event) => setFormState((current) => ({ ...current, workload_settings: { ...current.workload_settings, min_time_off_hours: event.target.value } }))} /></label>
                    <label className="facility-field"><span>Time off penalty weight</span><input type="number" value={formState.workload_settings.min_time_off_penalty_weight} onChange={(event) => setFormState((current) => ({ ...current, workload_settings: { ...current.workload_settings, min_time_off_penalty_weight: event.target.value } }))} /></label>
                    <label className="inline-checkbox-field"><span>Circadian rhythm enabled</span><input type="checkbox" checked={formState.workload_settings.circadian_enabled} onChange={(event) => setFormState((current) => ({ ...current, workload_settings: { ...current.workload_settings, circadian_enabled: event.target.checked } }))} /></label>
                    <label className="facility-field"><span>Circadian penalty</span><input type="number" value={formState.workload_settings.circadian_penalty_weight} onChange={(event) => setFormState((current) => ({ ...current, workload_settings: { ...current.workload_settings, circadian_penalty_weight: event.target.value } }))} /></label>
                    <label className="facility-field"><span>Min days in a row</span><input type="number" value={formState.workload_settings.min_days_in_row} onChange={(event) => setFormState((current) => ({ ...current, workload_settings: { ...current.workload_settings, min_days_in_row: event.target.value } }))} /></label>
                    <label className="facility-field"><span>Min days in a row penalty</span><input type="number" value={formState.workload_settings.min_days_in_row_penalty_weight} onChange={(event) => setFormState((current) => ({ ...current, workload_settings: { ...current.workload_settings, min_days_in_row_penalty_weight: event.target.value } }))} /></label>
                    <label className="facility-field"><span>Max days in a row</span><input type="number" value={formState.workload_settings.max_days_in_row} onChange={(event) => setFormState((current) => ({ ...current, workload_settings: { ...current.workload_settings, max_days_in_row: event.target.value } }))} /></label>
                    <label className="facility-field"><span>Max days in a row penalty</span><input type="number" value={formState.workload_settings.max_days_in_row_penalty_weight} onChange={(event) => setFormState((current) => ({ ...current, workload_settings: { ...current.workload_settings, max_days_in_row_penalty_weight: event.target.value } }))} /></label>
                    <label className="facility-field"><span>Min same shifts in a row</span><input type="number" value={formState.workload_settings.min_same_shifts_in_row} onChange={(event) => setFormState((current) => ({ ...current, workload_settings: { ...current.workload_settings, min_same_shifts_in_row: event.target.value } }))} /></label>
                    <label className="facility-field"><span>Min same shifts in a row penalty</span><input type="number" value={formState.workload_settings.min_same_shifts_in_row_penalty_weight} onChange={(event) => setFormState((current) => ({ ...current, workload_settings: { ...current.workload_settings, min_same_shifts_in_row_penalty_weight: event.target.value } }))} /></label>
                    <label className="facility-field"><span>Max same shifts in a row</span><input type="number" value={formState.workload_settings.max_same_shifts_in_row} onChange={(event) => setFormState((current) => ({ ...current, workload_settings: { ...current.workload_settings, max_same_shifts_in_row: event.target.value } }))} /></label>
                    <label className="facility-field"><span>Max same shifts in a row penalty</span><input type="number" value={formState.workload_settings.max_same_shifts_in_row_penalty_weight} onChange={(event) => setFormState((current) => ({ ...current, workload_settings: { ...current.workload_settings, max_same_shifts_in_row_penalty_weight: event.target.value } }))} /></label>
                  </div>
                </div>
              )}

              {activeTab === 'facilities' && (
                <div className="contract-section-stack">
                  <h3>Allowed Facilities</h3>
                  <div className="days-grid">
                    {activeFacilities.map((facility) => {
                      const checked = formState.facility_ids.includes(facility.id)
                      return (
                        <label key={facility.id} className="day-option">
                          <input
                            type="checkbox"
                            checked={checked}
                            onChange={(event) => {
                              const nextIds = new Set(formState.facility_ids)
                              if (event.target.checked) {
                                nextIds.add(facility.id)
                              } else {
                                nextIds.delete(facility.id)
                              }
                              setFormState((current) => ({
                                ...current,
                                facility_ids: Array.from(nextIds).sort((a, b) => a - b),
                              }))
                            }}
                          />
                          <span>{facility.name}</span>
                        </label>
                      )
                    })}
                  </div>
                  {!activeFacilities.length && <div className="empty-state">No active facilities found.</div>}
                </div>
              )}

              {activeTab === 'shifts' && (
                <div className="contract-section-stack">
                  <h3>Shift Rules</h3>
                  {!formState.facility_ids.length && (
                    <div className="request-existing-note">Select facilities first.</div>
                  )}
                  {formState.shift_settings.rules.map((shiftRule, shiftIndex) => (
                    <div key={shiftRule.id} className="contract-shift-card">
                      <div className="contract-shift-card-header">
                        <label className="facility-field">
                          <span>Rule label</span>
                          <input
                            type="text"
                            value={shiftRule.label}
                            placeholder="Nocturnist set"
                            onChange={(event) => setFormState((current) => ({
                              ...current,
                              shift_settings: {
                                ...current.shift_settings,
                                rules: current.shift_settings.rules.map((item, itemIndex) => (
                                  itemIndex === shiftIndex ? { ...item, label: event.target.value } : item
                                )),
                              },
                            }))}
                          />
                        </label>
                        <button
                          type="button"
                          className="contract-remove-button"
                          onClick={() => setFormState((current) => ({
                            ...current,
                            shift_settings: {
                              ...current.shift_settings,
                              rules: current.shift_settings.rules.filter((_, itemIndex) => itemIndex !== shiftIndex),
                            },
                          }))}
                          disabled={formState.shift_settings.rules.length === 1}
                        >
                          Remove rule group
                        </button>
                      </div>

                      <div className="request-template-list">
                        {templatesForSelectedFacilities.map((template) => {
                          const checked = shiftRule.shift_template_ids.includes(template.id)
                          return (
                            <label key={template.id} className="day-option">
                              <input
                                type="checkbox"
                                checked={checked}
                                onChange={(event) => {
                                  setFormState((current) => ({
                                    ...current,
                                    shift_settings: {
                                      ...current.shift_settings,
                                      rules: current.shift_settings.rules.map((item, itemIndex) => {
                                        if (itemIndex !== shiftIndex) {
                                          return item
                                        }

                                        const nextTemplateIds = new Set(item.shift_template_ids)
                                        if (event.target.checked) {
                                          nextTemplateIds.add(template.id)
                                        } else {
                                          nextTemplateIds.delete(template.id)
                                        }

                                        return {
                                          ...item,
                                          shift_template_ids: Array.from(nextTemplateIds).sort((a, b) => a - b),
                                        }
                                      }),
                                    },
                                  }))
                                }}
                              />
                              <span>{template.name} ({template.facility_name})</span>
                            </label>
                          )
                        })}
                        {!!formState.facility_ids.length && !templatesForSelectedFacilities.length && (
                          <div className="request-existing-note">No active shift templates found for the selected facilities.</div>
                        )}
                      </div>

                      <div className="contract-rule-grid">
                        {shiftRule.period_rules.map((rule, periodIndex) => (
                          <div key={rule.id} className="contract-rule-row">
                            <div className="contract-minmax-columns">
                              <div className="contract-minmax-column">
                                <div className="contract-minmax-title">Min</div>
                                <label className="facility-field"><span>Min value</span><input type="number" value={rule.min_value} onChange={(event) => setFormState((current) => ({ ...current, shift_settings: { ...current.shift_settings, rules: current.shift_settings.rules.map((item, itemIndex) => itemIndex === shiftIndex ? { ...item, period_rules: item.period_rules.map((periodRule, targetPeriodIndex) => targetPeriodIndex === periodIndex ? { ...periodRule, min_value: event.target.value } : periodRule) } : item) } }))} /></label>
                                <label className="facility-field"><span>Min penalty</span><input type="number" value={rule.min_penalty_weight} onChange={(event) => setFormState((current) => ({ ...current, shift_settings: { ...current.shift_settings, rules: current.shift_settings.rules.map((item, itemIndex) => itemIndex === shiftIndex ? { ...item, period_rules: item.period_rules.map((periodRule, targetPeriodIndex) => targetPeriodIndex === periodIndex ? { ...periodRule, min_penalty_weight: event.target.value } : periodRule) } : item) } }))} /></label>
                              </div>
                              <div className="contract-minmax-column">
                                <div className="contract-minmax-title">Max</div>
                                <label className="facility-field"><span>Max value</span><input type="number" value={rule.max_value} onChange={(event) => setFormState((current) => ({ ...current, shift_settings: { ...current.shift_settings, rules: current.shift_settings.rules.map((item, itemIndex) => itemIndex === shiftIndex ? { ...item, period_rules: item.period_rules.map((periodRule, targetPeriodIndex) => targetPeriodIndex === periodIndex ? { ...periodRule, max_value: event.target.value } : periodRule) } : item) } }))} /></label>
                                <label className="facility-field"><span>Max penalty</span><input type="number" value={rule.max_penalty_weight} onChange={(event) => setFormState((current) => ({ ...current, shift_settings: { ...current.shift_settings, rules: current.shift_settings.rules.map((item, itemIndex) => itemIndex === shiftIndex ? { ...item, period_rules: item.period_rules.map((periodRule, targetPeriodIndex) => targetPeriodIndex === periodIndex ? { ...periodRule, max_penalty_weight: event.target.value } : periodRule) } : item) } }))} /></label>
                              </div>
                            </div>
                            <div className="contract-rule-controls-grid">
                              <label className="facility-field"><span>Period</span><select value={rule.period_type} onChange={(event) => setFormState((current) => ({ ...current, shift_settings: { ...current.shift_settings, rules: current.shift_settings.rules.map((item, itemIndex) => itemIndex === shiftIndex ? { ...item, period_rules: item.period_rules.map((periodRule, targetPeriodIndex) => targetPeriodIndex === periodIndex ? { ...periodRule, period_type: event.target.value as PeriodType } : periodRule) } : item) } }))}><option value="WEEK">Week</option><option value="MONTH">Month</option><option value="SCHEDULE_BLOCK">Schedule Block</option></select></label>
                              <label className="facility-field"><span>Units</span><select value={rule.units} onChange={(event) => setFormState((current) => ({ ...current, shift_settings: { ...current.shift_settings, rules: current.shift_settings.rules.map((item, itemIndex) => itemIndex === shiftIndex ? { ...item, period_rules: item.period_rules.map((periodRule, targetPeriodIndex) => targetPeriodIndex === periodIndex ? { ...periodRule, units: event.target.value as RuleUnits } : periodRule) } : item) } }))}><option value="HOURS">Hours</option><option value="SHIFTS">Shifts</option></select></label>
                              <label className="inline-checkbox-field"><span>Spread violations</span><input type="checkbox" checked={rule.spread_violations} onChange={(event) => setFormState((current) => ({ ...current, shift_settings: { ...current.shift_settings, rules: current.shift_settings.rules.map((item, itemIndex) => itemIndex === shiftIndex ? { ...item, period_rules: item.period_rules.map((periodRule, targetPeriodIndex) => targetPeriodIndex === periodIndex ? { ...periodRule, spread_violations: event.target.checked } : periodRule) } : item) } }))} /></label>
                            </div>
                            <button type="button" className="contract-remove-button" onClick={() => setFormState((current) => ({ ...current, shift_settings: { ...current.shift_settings, rules: current.shift_settings.rules.map((item, itemIndex) => itemIndex === shiftIndex ? { ...item, period_rules: item.period_rules.filter((_, targetPeriodIndex) => targetPeriodIndex !== periodIndex) } : item) } }))} disabled={shiftRule.period_rules.length === 1}>Remove</button>
                          </div>
                        ))}
                      </div>

                      <button
                        type="button"
                        className="primary-action"
                        onClick={() => setFormState((current) => ({
                          ...current,
                          shift_settings: {
                            ...current.shift_settings,
                            rules: current.shift_settings.rules.map((item, itemIndex) => (
                              itemIndex === shiftIndex
                                ? { ...item, period_rules: [...item.period_rules, cloneRule(makeMinMaxRule('MONTH'))] }
                                : item
                            )),
                          },
                        }))}
                      >
                        Add Period Rule To Group
                      </button>
                    </div>
                  ))}

                  <button type="button" className="primary-action" onClick={() => setFormState((current) => ({ ...current, shift_settings: { ...current.shift_settings, rules: [...current.shift_settings.rules, makeShiftRule()] } }))}>
                    Add Shift Rule Group
                  </button>
                </div>
              )}

              {activeTab === 'nights' && (
                <div className="contract-section-stack">
                  <h3>Night Rules</h3>
                  <div className="contract-rule-grid">
                    {formState.night_settings.period_rules.map((rule, index) => (
                      <div key={rule.id} className="contract-rule-row">
                        <div className="contract-minmax-columns">
                          <div className="contract-minmax-column">
                            <div className="contract-minmax-title">Min</div>
                            <label className="facility-field"><span>Min night shifts</span><input type="number" value={rule.min_shifts} onChange={(event) => setFormState((current) => ({ ...current, night_settings: { ...current.night_settings, period_rules: current.night_settings.period_rules.map((item, itemIndex) => itemIndex === index ? { ...item, min_shifts: event.target.value } : item) } }))} /></label>
                            <label className="facility-field"><span>Min penalty</span><input type="number" value={rule.min_penalty_weight} onChange={(event) => setFormState((current) => ({ ...current, night_settings: { ...current.night_settings, period_rules: current.night_settings.period_rules.map((item, itemIndex) => itemIndex === index ? { ...item, min_penalty_weight: event.target.value } : item) } }))} /></label>
                          </div>
                          <div className="contract-minmax-column">
                            <div className="contract-minmax-title">Max</div>
                            <label className="facility-field"><span>Max night shifts</span><input type="number" value={rule.max_shifts} onChange={(event) => setFormState((current) => ({ ...current, night_settings: { ...current.night_settings, period_rules: current.night_settings.period_rules.map((item, itemIndex) => itemIndex === index ? { ...item, max_shifts: event.target.value } : item) } }))} /></label>
                            <label className="facility-field"><span>Max penalty</span><input type="number" value={rule.max_penalty_weight} onChange={(event) => setFormState((current) => ({ ...current, night_settings: { ...current.night_settings, period_rules: current.night_settings.period_rules.map((item, itemIndex) => itemIndex === index ? { ...item, max_penalty_weight: event.target.value } : item) } }))} /></label>
                          </div>
                        </div>
                        <div className="contract-rule-controls-grid">
                          <label className="facility-field"><span>Period</span><select value={rule.period_type} onChange={(event) => setFormState((current) => ({ ...current, night_settings: { ...current.night_settings, period_rules: current.night_settings.period_rules.map((item, itemIndex) => itemIndex === index ? { ...item, period_type: event.target.value as PeriodType } : item) } }))}><option value="WEEK">Week</option><option value="MONTH">Month</option><option value="SCHEDULE_BLOCK">Schedule Block</option></select></label>
                          <label className="inline-checkbox-field"><span>Spread violations</span><input type="checkbox" checked={rule.spread_violations} onChange={(event) => setFormState((current) => ({ ...current, night_settings: { ...current.night_settings, period_rules: current.night_settings.period_rules.map((item, itemIndex) => itemIndex === index ? { ...item, spread_violations: event.target.checked } : item) } }))} /></label>
                        </div>
                        <button type="button" className="contract-remove-button" onClick={() => setFormState((current) => ({ ...current, night_settings: { ...current.night_settings, period_rules: current.night_settings.period_rules.filter((_, itemIndex) => itemIndex !== index) } }))} disabled={formState.night_settings.period_rules.length === 1}>Remove</button>
                      </div>
                    ))}
                  </div>
                  <button type="button" className="primary-action" onClick={() => setFormState((current) => ({ ...current, night_settings: { ...current.night_settings, period_rules: [...current.night_settings.period_rules, makeNightRule('MONTH')] } }))}>Add Night Period Rule</button>

                  <div className="contract-input-grid">
                    <label className="facility-field"><span>Min consecutive night shifts</span><input type="number" value={formState.night_settings.min_consecutive_night_shifts} onChange={(event) => setFormState((current) => ({ ...current, night_settings: { ...current.night_settings, min_consecutive_night_shifts: event.target.value } }))} /></label>
                    <label className="facility-field"><span>Min consecutive night shifts penalty</span><input type="number" value={formState.night_settings.min_consecutive_night_shifts_penalty_weight} onChange={(event) => setFormState((current) => ({ ...current, night_settings: { ...current.night_settings, min_consecutive_night_shifts_penalty_weight: event.target.value } }))} /></label>
                    <label className="facility-field"><span>Max consecutive night shifts</span><input type="number" value={formState.night_settings.max_consecutive_night_shifts} onChange={(event) => setFormState((current) => ({ ...current, night_settings: { ...current.night_settings, max_consecutive_night_shifts: event.target.value } }))} /></label>
                    <label className="facility-field"><span>Max consecutive night shifts penalty</span><input type="number" value={formState.night_settings.max_consecutive_night_shifts_penalty_weight} onChange={(event) => setFormState((current) => ({ ...current, night_settings: { ...current.night_settings, max_consecutive_night_shifts_penalty_weight: event.target.value } }))} /></label>
                    <label className="facility-field"><span>Days off after night block before non-night</span><input type="number" value={formState.night_settings.days_off_after_night_block} onChange={(event) => setFormState((current) => ({ ...current, night_settings: { ...current.night_settings, days_off_after_night_block: event.target.value } }))} /></label>
                    <label className="facility-field"><span>Non-night recovery penalty</span><input type="number" value={formState.night_settings.days_off_after_night_block_penalty_weight} onChange={(event) => setFormState((current) => ({ ...current, night_settings: { ...current.night_settings, days_off_after_night_block_penalty_weight: event.target.value } }))} /></label>
                    <label className="facility-field"><span>Days off after night block before next night block</span><input type="number" value={formState.night_settings.days_off_before_next_night_shift} onChange={(event) => setFormState((current) => ({ ...current, night_settings: { ...current.night_settings, days_off_before_next_night_shift: event.target.value } }))} /></label>
                    <label className="facility-field"><span>Next night block recovery penalty</span><input type="number" value={formState.night_settings.days_off_before_next_night_shift_penalty_weight} onChange={(event) => setFormState((current) => ({ ...current, night_settings: { ...current.night_settings, days_off_before_next_night_shift_penalty_weight: event.target.value } }))} /></label>
                  </div>
                </div>
              )}

              {activeTab === 'weekends' && (
                <div className="contract-section-stack">
                  <h3>Weekend Rules</h3>
                  <div className="contract-rule-grid">
                    {formState.weekend_settings.period_rules.map((rule, index) => (
                      <div key={rule.id} className="contract-rule-row">
                        <div className="contract-minmax-columns">
                          <div className="contract-minmax-column">
                            <div className="contract-minmax-title">Min</div>
                            <label className="facility-field"><span>Min weekend volume</span><input type="number" value={rule.min_volume} onChange={(event) => setFormState((current) => ({ ...current, weekend_settings: { ...current.weekend_settings, period_rules: current.weekend_settings.period_rules.map((item, itemIndex) => itemIndex === index ? { ...item, min_volume: event.target.value } : item) } }))} /></label>
                            <label className="facility-field"><span>Min penalty</span><input type="number" value={rule.min_penalty_weight} onChange={(event) => setFormState((current) => ({ ...current, weekend_settings: { ...current.weekend_settings, period_rules: current.weekend_settings.period_rules.map((item, itemIndex) => itemIndex === index ? { ...item, min_penalty_weight: event.target.value } : item) } }))} /></label>
                          </div>
                          <div className="contract-minmax-column">
                            <div className="contract-minmax-title">Max</div>
                            <label className="facility-field"><span>Max weekend volume</span><input type="number" value={rule.max_volume} onChange={(event) => setFormState((current) => ({ ...current, weekend_settings: { ...current.weekend_settings, period_rules: current.weekend_settings.period_rules.map((item, itemIndex) => itemIndex === index ? { ...item, max_volume: event.target.value } : item) } }))} /></label>
                            <label className="facility-field"><span>Max penalty</span><input type="number" value={rule.max_penalty_weight} onChange={(event) => setFormState((current) => ({ ...current, weekend_settings: { ...current.weekend_settings, period_rules: current.weekend_settings.period_rules.map((item, itemIndex) => itemIndex === index ? { ...item, max_penalty_weight: event.target.value } : item) } }))} /></label>
                          </div>
                        </div>
                        <div className="contract-rule-controls-grid">
                          <label className="facility-field"><span>Period</span><select value={rule.period_type} onChange={(event) => setFormState((current) => ({ ...current, weekend_settings: { ...current.weekend_settings, period_rules: current.weekend_settings.period_rules.map((item, itemIndex) => itemIndex === index ? { ...item, period_type: event.target.value as 'MONTH' | 'SCHEDULE_BLOCK' } : item) } }))}><option value="MONTH">Month</option><option value="SCHEDULE_BLOCK">Schedule Block</option></select></label>
                          <label className="inline-checkbox-field"><span>Spread violations</span><input type="checkbox" checked={rule.spread_violations} onChange={(event) => setFormState((current) => ({ ...current, weekend_settings: { ...current.weekend_settings, period_rules: current.weekend_settings.period_rules.map((item, itemIndex) => itemIndex === index ? { ...item, spread_violations: event.target.checked } : item) } }))} /></label>
                        </div>
                        <button type="button" className="contract-remove-button" onClick={() => setFormState((current) => ({ ...current, weekend_settings: { ...current.weekend_settings, period_rules: current.weekend_settings.period_rules.filter((_, itemIndex) => itemIndex !== index) } }))} disabled={formState.weekend_settings.period_rules.length === 1}>Remove</button>
                      </div>
                    ))}
                  </div>
                  <button type="button" className="primary-action" onClick={() => setFormState((current) => ({ ...current, weekend_settings: { ...current.weekend_settings, period_rules: [...current.weekend_settings.period_rules, makeWeekendRule('SCHEDULE_BLOCK')] } }))}>Add Weekend Period Rule</button>

                  <div className="contract-section-stack">
                    <h3>Consecutive Weekends</h3>
                    <div className="contract-rule-row">
                      <div className="contract-minmax-columns">
                        <div className="contract-minmax-column">
                          <div className="contract-minmax-title">Min</div>
                          <label className="facility-field"><span>Min consecutive weekends</span><input type="number" value={formState.weekend_settings.min_consecutive_weekends} onChange={(event) => setFormState((current) => ({ ...current, weekend_settings: { ...current.weekend_settings, min_consecutive_weekends: event.target.value } }))} /></label>
                          <label className="facility-field"><span>Min penalty</span><input type="number" value={formState.weekend_settings.min_consecutive_weekends_penalty_weight} onChange={(event) => setFormState((current) => ({ ...current, weekend_settings: { ...current.weekend_settings, min_consecutive_weekends_penalty_weight: event.target.value } }))} /></label>
                        </div>
                        <div className="contract-minmax-column">
                          <div className="contract-minmax-title">Max</div>
                          <label className="facility-field"><span>Max consecutive weekends</span><input type="number" value={formState.weekend_settings.max_consecutive_weekends} onChange={(event) => setFormState((current) => ({ ...current, weekend_settings: { ...current.weekend_settings, max_consecutive_weekends: event.target.value } }))} /></label>
                          <label className="facility-field"><span>Max penalty</span><input type="number" value={formState.weekend_settings.max_consecutive_weekends_penalty_weight} onChange={(event) => setFormState((current) => ({ ...current, weekend_settings: { ...current.weekend_settings, max_consecutive_weekends_penalty_weight: event.target.value } }))} /></label>
                        </div>
                      </div>
                    </div>

                    <h3>Consecutive Weekend Shifts</h3>
                    <div className="contract-rule-row">
                      <div className="contract-minmax-columns">
                        <div className="contract-minmax-column">
                          <div className="contract-minmax-title">Min</div>
                          <label className="facility-field"><span>Min consecutive weekend shifts</span><input type="number" value={formState.weekend_settings.min_consecutive_weekend_shifts} onChange={(event) => setFormState((current) => ({ ...current, weekend_settings: { ...current.weekend_settings, min_consecutive_weekend_shifts: event.target.value } }))} /></label>
                          <label className="facility-field"><span>Min penalty</span><input type="number" value={formState.weekend_settings.min_consecutive_weekend_shifts_penalty_weight} onChange={(event) => setFormState((current) => ({ ...current, weekend_settings: { ...current.weekend_settings, min_consecutive_weekend_shifts_penalty_weight: event.target.value } }))} /></label>
                        </div>
                        <div className="contract-minmax-column">
                          <div className="contract-minmax-title">Max</div>
                          <label className="facility-field"><span>Max consecutive weekend shifts</span><input type="number" value={formState.weekend_settings.max_consecutive_weekend_shifts} onChange={(event) => setFormState((current) => ({ ...current, weekend_settings: { ...current.weekend_settings, max_consecutive_weekend_shifts: event.target.value } }))} /></label>
                          <label className="facility-field"><span>Max penalty</span><input type="number" value={formState.weekend_settings.max_consecutive_weekend_shifts_penalty_weight} onChange={(event) => setFormState((current) => ({ ...current, weekend_settings: { ...current.weekend_settings, max_consecutive_weekend_shifts_penalty_weight: event.target.value } }))} /></label>
                        </div>
                      </div>
                    </div>

                    <div className="contract-input-grid">
                      <label className="inline-checkbox-field"><span>Do not work Friday night before weekend off</span><input type="checkbox" checked={formState.weekend_settings.block_friday_night_before_weekend_off} onChange={(event) => setFormState((current) => ({ ...current, weekend_settings: { ...current.weekend_settings, block_friday_night_before_weekend_off: event.target.checked } }))} /></label>
                      <label className="facility-field"><span>Friday night weekend-off rule penalty</span><input type="number" value={formState.weekend_settings.block_friday_night_before_weekend_off_penalty_weight} onChange={(event) => setFormState((current) => ({ ...current, weekend_settings: { ...current.weekend_settings, block_friday_night_before_weekend_off_penalty_weight: event.target.value } }))} /></label>
                    </div>
                  </div>
                </div>
              )}

              {activeTab === 'users' && (
                <div className="contract-section-stack">
                  <h3>Assign Users In Domain</h3>
                  <div className="request-template-list">
                    {usersForSelectedDomain.map((physician) => {
                      const checked = formState.assigned_user_ids.includes(physician.id)
                      return (
                        <label key={physician.id} className="day-option">
                          <input
                            type="checkbox"
                            checked={checked}
                            onChange={(event) => {
                              setFormState((current) => {
                                const next = new Set(current.assigned_user_ids)
                                if (event.target.checked) {
                                  next.add(physician.id)
                                } else {
                                  next.delete(physician.id)
                                }
                                return {
                                  ...current,
                                  assigned_user_ids: Array.from(next).sort((a, b) => a - b),
                                }
                              })
                            }}
                          />
                          <span>{physicianLabel(physician)}</span>
                        </label>
                      )
                    })}
                  </div>

                  <h3>Request Settings</h3>
                  <div className="contract-request-group">
                    <h4>Allowed Request Types</h4>
                    <div className="contract-request-allowed-grid">
                      <label className="contract-request-allowed-item"><input type="checkbox" checked={formState.request_settings.allow_day_off} onChange={(event) => setFormState((current) => ({ ...current, request_settings: { ...current.request_settings, allow_day_off: event.target.checked } }))} /><span>Day Off allowed</span></label>
                      <label className="contract-request-allowed-item"><input type="checkbox" checked={formState.request_settings.allow_shift_off} onChange={(event) => setFormState((current) => ({ ...current, request_settings: { ...current.request_settings, allow_shift_off: event.target.checked } }))} /><span>Shift Off allowed</span></label>
                      <label className="contract-request-allowed-item"><input type="checkbox" checked={formState.request_settings.allow_day_on} onChange={(event) => setFormState((current) => ({ ...current, request_settings: { ...current.request_settings, allow_day_on: event.target.checked } }))} /><span>Day On allowed</span></label>
                      <label className="contract-request-allowed-item"><input type="checkbox" checked={formState.request_settings.allow_shift_on} onChange={(event) => setFormState((current) => ({ ...current, request_settings: { ...current.request_settings, allow_shift_on: event.target.checked } }))} /><span>Shift On allowed</span></label>
                    </div>
                  </div>

                  <div className="contract-request-group">
                    <h4>Priority Settings</h4>
                    <div className="contract-priority-grid">
                      <div className="contract-priority-header">Low</div>
                      <div className="contract-priority-header">Medium</div>
                      <div className="contract-priority-header">High</div>
                      <div className="contract-priority-header">Fixed</div>
                      <div className="contract-priority-header">Weekend</div>

                      <label className="facility-field"><span>Request limit</span><input type="number" value={formState.request_settings.low_request_limit} onChange={(event) => setFormState((current) => ({ ...current, request_settings: { ...current.request_settings, low_request_limit: event.target.value } }))} disabled={formState.request_settings.low_request_unlimited} /></label>
                      <label className="facility-field"><span>Request limit</span><input type="number" value={formState.request_settings.medium_request_limit} onChange={(event) => setFormState((current) => ({ ...current, request_settings: { ...current.request_settings, medium_request_limit: event.target.value } }))} /></label>
                      <label className="facility-field"><span>Request limit</span><input type="number" value={formState.request_settings.high_request_limit} onChange={(event) => setFormState((current) => ({ ...current, request_settings: { ...current.request_settings, high_request_limit: event.target.value } }))} /></label>
                      <div className="contract-priority-spacer" aria-hidden="true" />
                      <label className="facility-field"><span>Request limit</span><input type="number" value={formState.request_settings.weekend_request_limit} onChange={(event) => setFormState((current) => ({ ...current, request_settings: { ...current.request_settings, weekend_request_limit: event.target.value } }))} /></label>

                      <label className="contract-request-allowed-item contract-priority-low-unlimited"><input type="checkbox" checked={formState.request_settings.low_request_unlimited} onChange={(event) => setFormState((current) => ({ ...current, request_settings: { ...current.request_settings, low_request_unlimited: event.target.checked } }))} /><span>Low unlimited</span></label>
                      <div className="contract-priority-spacer" aria-hidden="true" />
                      <div className="contract-priority-spacer" aria-hidden="true" />
                      <div className="contract-priority-spacer" aria-hidden="true" />
                      <div className="contract-priority-spacer" aria-hidden="true" />

                      <label className="facility-field"><span>Request weight</span><input type="number" value={formState.request_settings.weight_low} onChange={(event) => setFormState((current) => ({ ...current, request_settings: { ...current.request_settings, weight_low: event.target.value } }))} /></label>
                      <label className="facility-field"><span>Request weight</span><input type="number" value={formState.request_settings.weight_medium} onChange={(event) => setFormState((current) => ({ ...current, request_settings: { ...current.request_settings, weight_medium: event.target.value } }))} /></label>
                      <label className="facility-field"><span>Request weight</span><input type="number" value={formState.request_settings.weight_high} onChange={(event) => setFormState((current) => ({ ...current, request_settings: { ...current.request_settings, weight_high: event.target.value } }))} /></label>
                      <label className="facility-field"><span>Request weight</span><input type="number" value={formState.request_settings.weight_fixed} onChange={(event) => setFormState((current) => ({ ...current, request_settings: { ...current.request_settings, weight_fixed: event.target.value } }))} /></label>
                      <label className="facility-field"><span>Request weight</span><input type="number" value={formState.request_settings.weight_weekend} onChange={(event) => setFormState((current) => ({ ...current, request_settings: { ...current.request_settings, weight_weekend: event.target.value } }))} /></label>
                    </div>
                  </div>
                </div>
              )}
            </div>

            <div className="shift-modal-actions">
              <button className="secondary" type="button" onClick={closeModal}>Back to Contract List</button>
              <button type="button" onClick={saveContract} disabled={isSaving}>{isSaving ? 'Saving...' : 'Save Contract'}</button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
