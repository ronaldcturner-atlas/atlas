# Optimizer promotion record: v2.temporal.1

Date: 2026-10-19

## Decision

Promote `v2.temporal.1` as the local Atlas optimizer revision. The previous
revision, `v2.availability.1`, remains executable for immediate rollback.

Candidate implementation commit: `5ecc5cf` (`Add bounded temporal optimizer
revision`)

Scoring revision: `score.2026-10-17.1`

## Change under evaluation

The candidate adds a bounded temporal large-neighborhood reconstruction to the
existing compiled search. It derives targets from authoritative violation
reports rather than schedule-specific dates or physician identities. The
reconstruction now:

- routes circadian violations into structural search;
- models immediate-predecessor circadian penalties, including published-block
  boundary context;
- includes configured Shift-group streak and Facility-period penalties;
- supplies current-state hints; and
- caps atomic ownership changes at 24 for deep reconstruction and 16 for the
  fallback reconstruction.

All behavior is gated by the immutable `v2.temporal.1` revision.
`v2.availability.1` retains its prior behavior for rollback.

## Correctness evidence

- Relevant scheduling regression suite: 163 passed.
- Django system checks: passed with no issues.
- Direct synthetic parity tests cover circadian repair, Facility-period rules,
  Shift-group streak rules, and the reassignment cap.
- Every read-only benchmark completed without compiled-score versus
  authoritative-score divergence.
- Runs 185 and 186 persisted the lowest verified authoritative score.
- Both live runs retained 2,225 assignments and zero unfilled shifts.
- Coverage, invalid-assignment, overlap, rest, workload, consecutive-day,
  underutilization, and distribution penalties remained zero.
- Run 184 and the active schedule were not modified by either continuation.

## Fixed-seed benchmark

Source: Run 184, Schedule Version 147

Source schedule fingerprint:
`f271669de834a35e22faae2ae6c9b361`

Configuration digest used by the corresponding live snapshot:
`a4205b39eac8be6722a7559a9b8e36d9910eac53aea996d77f5161f4452a33ea`

Each paired screen used the same source, scoring revision, 60-second configured
runtime, distribution focus, and seed. Bounded solver calls may finish after
the nominal deadline, so measured wall time is also reported.

| Seed | Availability score | Temporal score | Availability kernel/s | Temporal kernel/s | Availability wall (s) | Temporal wall (s) |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1901 | 438,200 | 437,200 | 916,828 | 943,379 | 62.73 | 63.62 |
| 1902 | 438,200 | 438,200 | 906,043 | 886,203 | 62.85 | 62.77 |
| 1903 | 438,200 | 418,200 | 869,634 | 969,348 | 61.81 | 65.13 |
| 1904 | 438,200 | 438,200 | 988,172 | 921,677 | 70.63 | 72.25 |
| 1905 | 438,200 | 438,200 | 910,100 | 895,102 | 65.74 | 65.66 |

- Stable median score: 438,200.
- Candidate median score: 438,200.
- Stable best score: 438,200.
- Candidate best score: 418,200.
- Candidate improved two of five samples and did not regress any sample.
- Stable median kernel rate: approximately 910,100 candidates/second.
- Candidate median kernel rate: approximately 921,677 candidates/second.
- Mean kernel rate was approximately 918,155 for stable and 923,142 for the
  candidate.
- Mean wall time increased approximately 1.8%, within the 10% limit.
- Every sample exceeded the 600,000 candidates/second absolute gate.

## Component-level live continuation evidence

Runs 185 and 186 independently continued from Run 184. No constraints changed,
and both runs recorded the same configuration digest and scoring revision.

| Component | Run 184 | Run 185 | Run 186 |
| --- | ---: | ---: | ---: |
| Weekend | 260,000 | 260,000 | 260,000 |
| Circadian | 98,000 | 98,000 | 96,000 |
| Night | 40,000 | 40,000 | 40,000 |
| Shift rule | 28,000 | 27,000 | 29,000 |
| Request | 8,200 | 8,200 | 8,200 |
| Same shift | 4,000 | 4,000 | 4,000 |
| All hard and remaining components | 0 | 0 | 0 |
| **Total** | **438,200** | **437,200** | **437,200** |

Run 185:

- Seed: `8871865015756727016`
- Runtime limit: 600 seconds
- Initial score: 438,200
- Completion score: 437,200
- Time to first verified feasible checkpoint: approximately 22.30 seconds
- Time to best score: approximately 87.37 seconds
- Plateau after best score: approximately 511.44 seconds
- Evaluated candidates: 297,319,976
- Accepted transitions: 154
- Kernel rate: approximately 1,052,835 candidates/second
- End-to-end rate: approximately 496,517 candidates/second
- Cache hits/misses: 153/9
- Phase seconds: preparation 92.25, kernel 282.40, authoritative validation
  222.27, transition refresh 58.80, command overhead 1.54
- Structural patches: 4 accepted from 4 attempts, with 1,000 official penalty
  reduction and no official penalty increase

Run 186:

- Seed: `7309153964681628114`
- Runtime limit: 600 seconds
- Initial score: 438,200
- Completion score: 437,200
- Time to first verified feasible checkpoint: approximately 21.36 seconds
- Time to best score: approximately 155.25 seconds
- Plateau after best score: approximately 444.07 seconds
- Evaluated candidates: 299,155,349
- Accepted transitions: 155
- Kernel rate: approximately 1,051,746 candidates/second
- End-to-end rate: approximately 499,160 candidates/second
- Cache hits/misses: 154/9
- Phase seconds: preparation 89.51, kernel 284.44, authoritative validation
  223.49, transition refresh 58.43, command overhead 1.53
- Structural patches: 4 accepted from 4 attempts, with 1,000 official penalty
  reduction and no official penalty increase

The two live runs reached the same total through different component paths.
Run 185 reduced Shift-rule penalty by 1,000. Run 186 reduced Circadian penalty
by 2,000 while increasing Shift-rule penalty by 1,000. This is evidence that
the neighborhood is using general configured constraints rather than a
block-specific target.

## Parallel-search reproducibility exception

OR-Tools parallel CP-SAT worker scheduling is nondeterministic, so an identical
seed does not guarantee an identical patch fingerprint. A one-worker variant
was repeatable but failed to improve the 438,200 source during the five-minute
quality test. It was rejected because deterministic replay would have removed
the demonstrated quality benefit.

The stochastic behavior was therefore evaluated distributionally using five
paired fixed seeds, followed by two independent production-shaped ten-minute
runs. Both live runs independently reached 437,200 with authoritative score
parity and preserved every hard constraint. Revision pinning, immutable run
provenance, and the prior executable revision provide operational
reproducibility and rollback even though parallel search trajectories differ.

## Rollback readiness

`v2.availability.1` remains supported and was executed during the paired
benchmark. Rollback requires changing `ATLAS_OPTIMIZER_REVISION` to
`v2.availability.1` and recreating the backend and both optimizer workers.
Completed candidate runs remain immutable and inactive unless explicitly
activated.

## Local promotion state

`ATLAS_OPTIMIZER_REVISION` is locally configured as `v2.temporal.1`. The
backend and both optimizer workers were recreated with that revision before
Runs 185 and 186. Both runs completed successfully and remain available for
audit. No GitHub push is part of this local promotion.
