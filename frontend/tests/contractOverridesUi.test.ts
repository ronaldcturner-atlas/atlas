import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const contractsSource = readFileSync(
  new URL('../src/components/ContractsView.tsx', import.meta.url),
  'utf8',
)
const overridesSource = readFileSync(
  new URL('../src/components/UserOverridesView.tsx', import.meta.url),
  'utf8',
)
const sharedRulesSource = readFileSync(
  new URL('../src/components/SharedRulesView.tsx', import.meta.url),
  'utf8',
)
const stylesSource = readFileSync(
  new URL('../src/index.css', import.meta.url),
  'utf8',
)
const shiftsSource = readFileSync(
  new URL('../src/components/ShiftsView.tsx', import.meta.url),
  'utf8',
)

describe('Contract user overrides', () => {
  it('provides the User Overrides navigation beside Shared Rules', () => {
    expect(contractsSource).toContain('User Overrides')
    expect(contractsSource).toContain("setPageTab('user_overrides')")
  })

  it('identifies overridden users inside the parent contract user list', () => {
    expect(contractsSource).toContain('contract-override-badge')
    expect(contractsSource).toContain('View / Edit Overrides')
  })

  it('shows inherited defaults and supports restoring all defaults', () => {
    expect(overridesSource).toContain('Contract default')
    expect(overridesSource).toContain('User override')
    expect(overridesSource).toContain('Restore All Defaults')
    expect(overridesSource).toContain('Blank override fields continue to inherit')
  })

  it('pairs min and max rules with penalties and marks tabs containing overrides', () => {
    expect(overridesSource).toContain('Corresponding penalties')
    expect(overridesSource).toContain("parentPath.includes('.period_rules.')")
    expect(overridesSource).toContain("title: 'Allowed Request Types'")
    expect(overridesSource).toContain("activeTab === 'request_settings' ? 'Request weight'")
    expect(overridesSource).toContain("' single-column'")
    expect(overridesSource).toContain("'Allowed Request Types',")
    expect(overridesSource).toContain("'Low Requests',")
    expect(overridesSource).toContain("'Medium Requests',")
    expect(overridesSource).toContain('ruleGroupSortValue(activeTab, a.title)')
    expect(overridesSource).toContain('user-override-tab-count')
    expect(overridesSource).toContain("className={`${activeTab === tab.id ? 'active' : ''}${overrideCount ? ' has-override' : ''}`}")
  })

  it('configures compact per-Facility targets and exposes them to user overrides', () => {
    expect(contractsSource).toContain('Facility Eligibility and Targets')
    expect(contractsSource).toContain('facility_settings: {')
    expect(contractsSource).toContain('Add Period Rule')
    expect(contractsSource).toContain('<option value="SHIFTS">Shifts</option>')
    expect(contractsSource).toContain('<option value="HOURS">Hours</option>')
    expect(overridesSource).toContain("{ id: 'facility_settings', label: 'Facilities' }")
    expect(overridesSource).toContain("facility_settings: 'Facility Rules'")
  })

  it('keeps numeric Contract and User Override inputs compact', () => {
    expect(stylesSource).toContain('.contract-modal .facility-field input[type="number"]{width:calc(6ch + 22px)')
    expect(stylesSource).toContain('.user-override-value-pair{display:grid;grid-template-columns:calc(6ch + 22px) calc(6ch + 22px)')
    expect(stylesSource).toContain('.contract-priority-grid{display:flex;align-items:flex-start;gap:10px;flex-wrap:wrap}')
    expect(contractsSource).toContain('<span>Maximum</span>')
    expect(contractsSource).toContain('<span>Penalty</span>')
    expect(contractsSource).not.toContain('value={formState.request_settings.weight_weekend}')
    expect(contractsSource).toContain('Weekend has no separate penalty.')
    expect(contractsSource).toContain('<small>Admin only</small>')
    expect(stylesSource).toContain('.contract-priority-admin-card{margin-left:114px;border:2px solid #7c3aed')
    expect(stylesSource).toContain('.schedule-light-theme .contract-priority-admin-card{border-color:#7c3aed')
  })

  it('keeps Contract-level Shared Rule settings synchronized and scoped', () => {
    expect(contractsSource).toContain('enabled: rule.enabled')
    expect(contractsSource).toContain('Disable for this Contract')
    expect(contractsSource).toContain('Enable for this Contract')
    expect(contractsSource).toContain('Unable to load the latest Contract settings.')
    expect(sharedRulesSource).toContain('Affected Contracts:')
    expect(sharedRulesSource).toContain('acknowledge_affected_contracts=true')
  })

  it('explains minimum work streaks and exposes Spread on every rule family', () => {
    expect(contractsSource).toContain('Leave Minimum blank to allow solitary shifts.')
    expect(contractsSource).toContain('min_time_off_spread_violations')
    expect(contractsSource).not.toContain('checked={formState.workload_settings.circadian_spread_violations}')
    expect(contractsSource).toContain('the next shift may start no more than one hour earlier')
    expect(contractsSource).toContain('days_in_row_spread_violations')
    expect(contractsSource).toContain('same_shifts_in_row_spread_violations')
    expect(contractsSource).toContain('facility-target-spread')
    expect(contractsSource).toContain('consecutive_night_shifts_spread_violations')
    expect(contractsSource).toContain('night_block_recovery_spread_violations')
    expect(contractsSource).toContain('consecutive_weekends_spread_violations')
    expect(contractsSource).toContain('consecutive_weekend_shifts_spread_violations')
    expect(contractsSource).toContain('friday_night_before_weekend_off_spread_violations')
    expect(sharedRulesSource).toContain('Spread violations can be set independently')
  })

  it('allows Monday to be explicitly designated as a weekend shift day', () => {
    expect(shiftsSource).toContain("['Friday', 'Saturday', 'Sunday', 'Monday']")
  })
})

