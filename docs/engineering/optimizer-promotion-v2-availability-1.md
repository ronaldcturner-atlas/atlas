# Optimizer promotion record: v2.availability.1

Date: 2026-10-19

## Decision

Promote `v2.availability.1` as the local Atlas optimizer revision. The previous
revision, `v2.circadian.1`, remains executable for immediate rollback.

Candidate implementation commit: `40c50c4` (`Add availability-aware optimizer
revision`)

Scoring revision: `score.2026-10-17.1`

## Change under evaluation

The candidate prevents the compiled search from proposing assignments on a
physician's Block Hours unavailable dates. Facility and date eligibility are
precomputed by physician and shift before candidate evaluation, so the hot
pair-swap loop performs only indexed array checks. Request inputs and legal
one-way reassignment candidates use the same availability rules.

All new behavior is gated by the immutable `v2.availability.1` revision.
`v2.circadian.1` retains its prior inputs and candidate behavior for rollback.

## Correctness evidence

- Focused optimizer regression tests: 58 passed.
- Frontend tests: 85 passed.
- Frontend production build: passed.
- Django system checks: passed with no issues.
- Candidate parity on source Run 178: zero legality, scoring, circadian, and
  shift-rule mismatches.
- Run 178 exclusion analysis found five legal improvements that the former
  neighborhood exhaustion behavior concealed.
- The rollback revision remained executable and selected its historical
  candidate under the same source schedule.

The full scheduling suite currently has 399 errors in
`test_initial_fill_index.py` from synthetic fixtures without the already
deployed shift-date field, plus three older V1 coverage-priority expectation
failures. The affected files and `optimizer.py` are unchanged from
`origin/mobile-beta`; these are recorded as existing baseline failures and are
not attributed to this candidate.

## Live continuation evidence

Runs 183 and 184 both continued from Run 180 without constraint changes.

Run 183:

- Revision: `v2.availability.1`
- Configuration digest:
  `832ff70dc0ca0bb432a354b3cdad4c794a3234ce0b9e8c54d39c7c76c30969a3`
- Runtime limit: 600 seconds
- Initial score: 457,100
- Completion score: 441,100
- Independent authoritative rescore: 441,100
- Improvement: 16,000
- Assignments: 2,225
- Unfilled shifts: zero
- Kernel rate: approximately 896,847 candidates/second
- End-to-end rate: approximately 389,645 candidates/second
- Coverage, overlap, rest, and invalid-assignment penalties: zero
- Score-audit warnings: none

Run 184:

- Revision: `v2.availability.1`
- Configuration digest:
  `832ff70dc0ca0bb432a354b3cdad4c794a3234ce0b9e8c54d39c7c76c30969a3`
- Runtime limit: 600 seconds
- Initial score: 457,100
- Completion score: 438,200
- Independent authoritative rescore: 438,200
- Improvement: 18,900
- Assignments: 2,225
- Unfilled shifts: zero
- Evaluated candidates: 249,603,274
- Accepted transitions: 128
- Kernel rate: approximately 914,560 candidates/second
- End-to-end rate: approximately 416,028 candidates/second
- Coverage, overlap, rest, and invalid-assignment penalties: zero
- Score-audit warnings: none

Run 184 improved the best correctly labeled candidate result by 2,900 and the
Run 180 source by 18,900 while preserving all hard constraints.

Runs 181 and 182 are retained as diagnostic evidence only. They exercised the
new behavior before it received its immutable revision label and therefore are
not promotion evidence for `v2.availability.1`.

## Performance evidence

A same-process alternating benchmark compared the rollback and candidate
revisions on the identical Run 180 schedule. Each sample evaluated 1,835,319
candidate schedules.

- `v2.circadian.1` median: approximately 953,958 candidates/second.
- `v2.availability.1` median: approximately 989,472 candidates/second.
- Candidate difference: approximately 3.7% faster.

The isolated benchmark confirms that the precomputed eligibility check does
not reduce kernel speed. Variation in whole-run throughput reflects different
accepted transitions, refresh work, and authoritative validation activity.

## Proportionate validation exception

The standard five-seed paired study was waived for this local promotion. Two
independent production-shaped continuations from the same unchanged source
schedule improved the official score, reconciled exactly with the independent
scorer, and preserved every hard constraint. The changed candidate path also
passed exact parity and a paired same-process throughput comparison.

## Rollback readiness

`v2.circadian.1` remains supported and was executed successfully during the
paired benchmark after the candidate was revision-gated. Rollback requires
changing `ATLAS_OPTIMIZER_REVISION` to `v2.circadian.1` and recreating the
backend and optimizer workers. Existing run records remain immutable.

## Local promotion state

`ATLAS_OPTIMIZER_REVISION` is locally configured as `v2.availability.1`. The
backend and optimizer workers were recreated with that revision before Runs
183 and 184. Both runs completed successfully and remain available for audit.
