import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const statsSource = readFileSync(
  new URL('../src/components/StatsView.tsx', import.meta.url),
  'utf8',
)
const styles = readFileSync(
  new URL('../src/index.css', import.meta.url),
  'utf8',
)

describe('desktop My Stats layout', () => {
  it('shows the shift detail list in both scheduler and user views', () => {
    expect(statsSource).not.toContain("!limitedToHours && <div className=\"stats-shift-list\"")
    expect(statsSource).toContain('<div className="stats-shift-list">')
  })

  it('places compact totals before the shift list', () => {
    expect(statsSource.indexOf('<aside className="stats-totals">')).toBeLessThan(
      statsSource.indexOf('<div className="stats-shift-list">'),
    )
    expect(styles).toContain('grid-template-columns:190px minmax(0,560px)')
    expect(styles).toContain('grid-template-columns:minmax(170px,1fr) 94px 72px')
  })
})
