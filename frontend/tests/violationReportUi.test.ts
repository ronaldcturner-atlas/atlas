import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const reportSource = readFileSync(
  new URL('../src/components/ScheduleVersionViolationReport.tsx', import.meta.url),
  'utf8',
)
const styles = readFileSync(new URL('../src/index.css', import.meta.url), 'utf8')

describe('Optimizer violation report', () => {
  it('keeps made and denied request totals inside the existing score grid', () => {
    expect(reportSource).toContain("{ key: 'HIGH', label: 'High priority' }")
    expect(reportSource).toContain("{ key: 'MEDIUM', label: 'Medium priority' }")
    expect(reportSource).toContain("{ key: 'LOW', label: 'Low priority' }")
    expect(reportSource).toContain('className="request-priority-score"')
    expect(reportSource).toContain('made /')
    expect(reportSource).toContain('denied')
    expect(reportSource).not.toContain('request-priority-outcomes')
  })

  it('uses a solid high-contrast selected score treatment', () => {
    expect(styles).toContain('.violation-score-filters>button.active')
    expect(styles).toContain('background:#1d4ed8;color:#fff')
    expect(styles).toContain('.violation-score-filters>button.active span,.violation-score-filters>button.active strong{color:#fff}')
  })

  it('shows each user contract once and uses the violation row for period scope', () => {
    expect(reportSource).toContain('<span>Contract: {userContractLabel(user)}</span>')
    expect(reportSource).toContain('<th>Period</th>')
    expect(reportSource).toContain('<td>{violationPeriodLabel(violation)}</td>')
    expect(reportSource).toContain("return 'Block'")
    expect(reportSource).not.toContain('<td>{violation.contract_name ?? \'-\'}</td>')
    expect(reportSource).not.toContain('<span>Contract: {user.workload_score.contract_name ?? \'No contract\'}</span>')
    expect(styles).toContain('.violation-user-identity{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}')
  })
})
