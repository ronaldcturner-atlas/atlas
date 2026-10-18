# Optimizer promotion record: v2.circadian.1

Date: 2026-10-17

## Decision

Promote `v2.circadian.1` as the local Atlas optimizer revision. The previous
stable revision, `v2.stable.2026-10-17`, remains executable for immediate
rollback. This record does not authorize or record a GitHub push, Railway
deployment, or production configuration change.

Candidate implementation commit: `12cb66b` (`Add circadian-aware optimizer
candidate`)

Scoring revision: `score.2026-10-17.1`

## Change under evaluation

The candidate adds exact circadian penalty deltas to compiled pair-swap
ranking, including published-boundary transitions. It also includes the
previously omitted shift-group consecutive-streak penalty in the candidate's
compiled shift-rule table. Both behaviors are revision-gated; the stable
revision retains its previous compiled objective.

## Correctness evidence

- Focused optimizer regression tests: 75 passed.
- Django system checks: passed with no issues.
- Run 167 candidate parity sample: zero circadian, shift-rule, component-total,
  and official-score mismatches across 100 authoritatively scored candidates.
- Affected-physician cache refresh audit: exact match, including circadian
  assignment rows.
- Stable revision candidate selection was verified unchanged after revision
  gating.

## Live continuation evidence

Run 169 continued from the recalculated Run 167 schedule.

- Revision: `v2.circadian.1`
- Configuration digest:
  `1b82ca1b17a1e8d899c66d00518a88486a118f0caa5f70d46f137516b587954a`
- Seed: `7168199855196137587`
- Runtime limit: 1,800 seconds
- Initial score: 676,000
- Completion score: 438,100
- Independent authoritative rescore: 438,100
- Improvement: 237,900 (35.2%)
- Evaluated candidates: 860,644,004
- Accepted transitions: 452
- Kernel rate: approximately 978,995 candidates/second
- End-to-end rate: approximately 478,741 candidates/second
- Coverage, overlap, rest, and invalid-assignment penalties: zero
- Score-audit warnings: none

Run 167 independently remained at 676,000 with 2,225 assignments and no hard
violations after Run 169 completed.

## Live fresh-fill evidence

Run 170 exercised the independent fresh-fill construction path.

- Revision: `v2.circadian.1`
- Configuration digest:
  `9d643d76d5717d1018b0041cc3b880b40bc1af05f7f637d6cb7671c00d738217`
- Seed: `2462866403871452949`
- Runtime limit: 7,200 seconds
- Complete initial construction score: 6,607,400
- Completion score: 629,000
- Independent authoritative rescore: 629,000
- Assignments: 2,225
- Unfilled shifts: zero
- Evaluated candidates: 2,719,597,410
- Accepted transitions: 1,443
- Kernel rate: approximately 1,077,105 candidates/second
- End-to-end rate: approximately 378,066 candidates/second
- Coverage, overlap, rest, and invalid-assignment penalties: zero
- Score-audit warnings: none
- Run remained inactive; active Run 169 was preserved.

## Confirmatory continuation evidence

Run 171 provided the short post-promotion confirmation from Run 170.

- Revision: `v2.circadian.1`
- Configuration digest:
  `7b0a57593365e6fb6bf5f41e4a04bb4d9adf9b0c7b20476dcb3b7b82f6001253`
- Seed: `5529044935206496395`
- Runtime limit: 600 seconds
- Initial score: 629,000
- Completion score: 626,000
- Independent authoritative rescore: 626,000
- Improvement: 3,000 (0.48%)
- Assignments: 2,225
- Unfilled shifts: zero
- Evaluated candidates: 282,602,344
- Accepted transitions: 147
- Kernel rate: approximately 945,625 candidates/second
- End-to-end rate: approximately 472,636 candidates/second
- Coverage, overlap, rest, and invalid-assignment penalties: zero
- Score-audit warnings: none
- Run remained inactive; active Run 170 was preserved.

Run 171 confirms that the promoted local revision can resume a completed fresh
fill, preserve hard validity and assignment coverage, improve the official
score, and reconcile exactly with the independent authoritative scorer during
a short production-shaped run.

## Proportionate validation exception

The standard five-seed paired study was waived for this local promotion. The
candidate produced a large, explainable improvement when continued from an
already plateaued stable schedule, achieved exact compiled-versus-authoritative
parity for the changed components, and completed a separate two-hour fresh fill
without hard violations. The first two or three runs after any future deployed
promotion must be reviewed, and any scoring divergence or hard violation
requires immediate rollback.

## Rollback drill

The local environment was changed from `v2.circadian.1` to
`v2.stable.2026-10-17`. The backend and both optimizer workers were recreated
without recreating PostgreSQL, and all three reported the stable revision.
Runs 169 and 170 remained complete with scores 438,100 and 629,000.

The environment was then changed back to `v2.circadian.1`. The backend and both
workers were recreated again and all three reported the candidate revision.
All services were healthy afterward.

## Local promotion state

`ATLAS_OPTIMIZER_REVISION` is locally configured as `v2.circadian.1`. The
candidate is locally promoted. No GitHub push or Railway change has been made.
Rollback requires changing the local setting to `v2.stable.2026-10-17` and
recreating the backend and optimizer workers.
