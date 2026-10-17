import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const shiftsSource = readFileSync(
  new URL('../src/components/ShiftsView.tsx', import.meta.url),
  'utf8',
)

describe('Shift Builder weekend designations', () => {
  it('offers Monday alongside Friday, Saturday, and Sunday', () => {
    expect(shiftsSource).toContain(
      "type WeekendDay = 'Friday' | 'Saturday' | 'Sunday' | 'Monday'",
    )
    expect(shiftsSource).toContain(
      "const WEEKEND_DAY_OPTIONS: WeekendDay[] = ['Friday', 'Saturday', 'Sunday', 'Monday']",
    )
  })
})
