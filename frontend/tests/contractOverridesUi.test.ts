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
    expect(stylesSource).toContain('.contract-modal .facility-field input[type="number"]{width:112px')
    expect(stylesSource).toContain('.user-override-value-pair{display:grid;grid-template-columns:112px 120px')
    expect(stylesSource).toContain('.contract-priority-grid{display:grid;grid-template-columns:repeat(5,112px)')
  })

  it('keeps Contract-level Shared Rule settings synchronized and scoped', () => {
    expect(contractsSource).toContain('enabled: rule.enabled')
    expect(contractsSource).toContain('Disable for this Contract')
    expect(contractsSource).toContain('Enable for this Contract')
    expect(contractsSource).toContain('Unable to load the latest Contract settings.')
    expect(sharedRulesSource).toContain('Affected Contracts:')
    expect(sharedRulesSource).toContain('acknowledge_affected_contracts=true')
  })
})

