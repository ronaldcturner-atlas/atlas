# Atlas handoff: v1 final and v2 starting point

Date: September 29, 2026

## Repository checkpoint

- Final v1 tag: `v1.0.0`
- Final v1 branch at release: `main`
- Atlas v2 development branch: `v2`
- Previous parallel-optimizer checkpoint: `3fe11a0`
- Previous simplified proportionality-report checkpoint: `7aca95d`

The `v1.0.0` tag is the authoritative end of Atlas v1. Do not rewrite or move it. Maintenance fixes intended for v1 must be explicitly identified; optimizer architecture work belongs on `v2`.

## Final v1 optimizer behavior

- Two independent optimizer runs may execute concurrently.
- Every run retains its own assignments, score, diagnostics, lineage, and explicit source.
- Fresh Fill has no implicit historical anchor.
- A run may start from any eligible completed previous run, including the same source used by another run.
- Only locked manual assignments and locked-open shifts are immutable. Previous optimizer assignments remain movable.
- Current authoritative requests override conflicting optimizer assignments from a source run.
- Completed independent runs do not activate automatically.
- Published blocks are historical snapshots and are not rescored against later contract or request changes unless unpublished.
- Official configured penalties always outrank proportionality.
- Facility and time proportionality account for optimizer-controlled opportunity supply, physician eligibility, manual-only occupancy, and explicit contract shift rules.
- Raw proportionality scores remain scheduler-facing lower-is-better information only.
- Score-neutral proportionality tie-breaking uses normalized 40% facility and 60% time-of-day priority.

## Final v1 verification

- Scheduling tests: 376 passed.
- Frontend production build passed at the immediately preceding v1 reporting checkpoint; the final weighting change is backend-only.
- Backend, PostgreSQL, and both optimizer workers were healthy.
- Both optimizer workers loaded the final normalized proportionality implementation.
- No optimizer run was active when the workers were restarted.

## Atlas v2 first milestone

ShiftAdmin routinely evaluated more than 100 million schedules in approximately 3,000 seconds. Atlas v2 must treat that as the performance baseline, not an aspirational comparison.

Run 92 established the Atlas v1 baseline:

- Runtime: 2,969 seconds.
- Candidate attempts: 246,194, approximately 83 per second.
- Full authoritative score evaluations: 5,293, approximately 1.8 per second.
- Score-cache hits: 2,994; misses: 5,293.

The v2 target is at least 35,000 genuinely distinct, meaningfully evaluated candidate schedules per second so 100 million candidates fit within roughly 3,000 seconds. Do not inflate this count with duplicate states, combinations rejected before producing a candidate schedule, or repeated reads of the same schedule.

The first implementation step is an isolated benchmark kernel built from a fixed saved-run snapshot. It should:

1. Encode assignments, physicians, shifts, eligibility, requests, rest, and configured rules as compact integer arrays or bitsets.
2. Evaluate swaps and reassignments with exact incremental deltas limited to affected physicians and shifts.
3. Track unique candidate states with compact deterministic hashes.
4. Perform full authoritative rescoring only for new best candidates and periodic consistency audits.
5. Prove score agreement before the kernel is allowed to persist an optimizer result.
6. Benchmark two-person swaps first, then extend to reassignments and multi-person neighborhoods.

If a Python/array implementation cannot sustain the target, move the candidate kernel to compiled C++ or Rust while retaining Django for configuration, run management, history, and persistence.

## Requirements that v2 must preserve

- Score only configured contract rules; do not add implicit default penalties.
- Never accept proportionality improvement at the cost of a worse official penalty.
- Exclude manual-assignment-only physicians from optimizer movement and proportionality scoring.
- Honor current Shift On and applicable Request Off requests.
- Preserve locked assignments and locked-open shifts exactly.
- Retain independent run results and explicit activation.
- Keep Fresh Fill independent from historical schedules.
- Preserve published schedule snapshots until explicitly unpublished.
- Keep both worker slots independent and safe across logout or browser closure.

## Startup for the next development session

1. Read `docs/engineering/atlas-source-of-truth.md` completely.
2. Read this handoff completely.
3. Confirm the current branch is `v2` and the repository is clean.
4. Confirm `v1.0.0` resolves to the parent checkpoint of v2 work.
5. Confirm no optimizer run is active before restarting workers or changing optimizer execution code.
6. Build the isolated candidate-kernel benchmark before changing the production search controller.
