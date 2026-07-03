# Shift Builder Requirements

## Purpose
Define reusable shift templates using day-of-week rules only.

## Data Rules

### Shift Template Fields

- `id`
- `facility`
- `name`
- `start_time`
- `end_time`
- `active_days_of_week`
	- Allowed values: `Sunday`, `Monday`, `Tuesday`, `Wednesday`, `Thursday`, `Friday`, `Saturday`
- `weekend_days` (array/list)
	- Allowed values only: `Friday`, `Saturday`, `Sunday`
- `night_shift` (boolean)
- `default_staffing_count` (integer, default `1`)
- `active` (boolean)

Example templates:

- Berkeley 7a-7p, active Mon Tue Wed Thu Fri, staffing 1
- Berkeley 7p-7a, active all days, night yes, staffing 1
- Berkeley Fast Track 1p-11p, active all days, staffing 1
- A template can be active Friday/Saturday/Sunday, but weekend_days can be only Saturday/Sunday.

## Field Guidance

- Remove `weekend_shift` boolean.
- Use `weekend_days` as Friday/Saturday/Sunday selections.
- Keep `night_shift` as a boolean.
- Keep `default_staffing_count`.

## Out of Scope (Current Phase)

- Do not assign users in Shift Builder.
- Do not use specific calendar dates in Shift Builder.
- Do not connect Shift Builder to My Schedule yet.
