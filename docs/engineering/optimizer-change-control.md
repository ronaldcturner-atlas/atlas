# Atlas optimizer change control

The optimizer is a versioned product subsystem. An optimizer behavior change is
not complete merely because its tests pass or its final score improves once.
Every change must be attributable, comparable, promotable, and reversible.

## Invariants

- The active and source schedules are never modified by an optimizer search.
- A completed optimizer result remains inactive until a user activates it.
- A run never silently executes a different optimizer revision from the one
  pinned when it was queued.
- An unsupported revision fails closed.
- Historical runs and rejected experiments are retained as evidence.
- Optimizer schema changes are additive throughout the rollback window.
- Scoring changes and search changes are evaluated and released separately.

## Revision model

`ATLAS_OPTIMIZER_REVISION` selects the revision assigned to newly queued V2
runs. It does not alter already queued runs. Every new run records:

- `optimizer_revision`
- `scoring_revision`
- `optimizer_commit`
- `configuration_digest`
- immutable completion score, component breakdown, and scoring revision
- source run, seed, focus, and maximum runtime

`configuration_digest` is the SHA-256 digest of the canonical launch snapshot,
including the assignments and mutable configuration used by the search. Runs
are comparable only when their scoring revision and configuration digest match.
Recalculation may refresh `final_score` under the current scoring revision, but
it never rewrites the immutable completion score or its component breakdown.

The user-facing engine may continue to be called Atlas V2. Internal revisions
must be immutable. A behavior change receives a new revision; an existing
revision is never redefined.

## Change workflow

1. Start from a clean branch at the currently approved stable revision.
2. Create a branch named `optimizer/<short-change-name>`.
3. Keep instrumentation, schema, scoring, search behavior, and UI changes in
   separate commits.
4. Add the candidate revision to the supported-revision registry, but leave
   `ATLAS_OPTIMIZER_REVISION` pointed at the stable revision.
5. Add authoritative parity tests for every objective component affected by
   the change.
6. Run the fixed benchmark snapshot with identical seeds, runtime, scoring
   revision, and configuration digest for stable and candidate revisions.
7. Record the benchmark result before promotion.
8. Promote by changing only the configured revision after all gates pass.
9. Keep the immediately previous stable revision executable for at least one
   release and throughout the operational rollback window.

## Required gates

### Correctness

- Zero coverage, overlap, invalid-assignment, or other hard-validity failures.
- Zero compiled-kernel versus authoritative-score mismatches.
- The persisted final schedule equals the lowest verified authoritative score.
- The source schedule and active schedule remain unchanged.
- Fixed seeds reproduce the same schedule fingerprint and score.
- A continuation run can start from a result produced by the previous stable
  revision.

### Quality

- Compare at least five fixed seeds when evaluating stochastic behavior.
- Candidate median final score must not regress from the stable median.
- Candidate best score must meet or improve the stable best unless the change
  has an explicitly approved correctness purpose.
- Report every score component, not only total score.
- Report time to first feasible schedule, time to best score, and plateau time.

### Performance

- Report end-to-end evaluations per second and total wall time.
- Report preparation, kernel, authoritative-validation, transition-refresh,
  and command-overhead time.
- Report cache hits and misses and improvement yield by operation, selection
  mode, and structural rule family.
- A throughput regression greater than 10% requires an explained and measured
  quality or correctness benefit.

## Promotion record

The promotion record must contain:

- stable and candidate revisions
- code commits
- scoring revision
- benchmark configuration digest
- source run and snapshot identifier
- fixed seeds and runtime
- component-level score comparison
- validity and parity results
- performance and telemetry comparison
- promotion decision and rollback revision

## Rollback

1. Stop assigning the candidate revision to new runs by restoring
   `ATLAS_OPTIMIZER_REVISION` to the previous stable revision.
2. Restart workers so new jobs use the restored default.
3. Leave already completed candidate runs inactive unless a user explicitly
   selected one.
4. Cancel running candidate jobs when their output is not wanted; otherwise
   allow them to finish as inactive evidence.
5. Do not delete candidate runs or rewrite their provenance.
6. Revert the isolated behavior commit only after the configuration rollback.
7. Do not reverse an additive provenance migration during an operational
   rollback.
8. If the scoring revision changed, rescore comparison runs under one common
   scoring revision before drawing conclusions.

## Current baseline

- Stable optimizer revision: `v2.stable.2026-10-17`
- Scoring revision: `score.2026-10-17.1`
- Canonical local comparison source: Run 167

Runs created before revision pinning intentionally retain blank provenance
fields. Their historical summaries remain available, but they must not be
presented as revision-controlled benchmark evidence.
