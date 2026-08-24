# Optimizer Benchmark Baseline

## Benchmark status

**Best-chain baseline established.** The primary scheduler benchmark completed
against the real Nov 2026 PREVIEW Schedule Block on 2026-08-17. The block remained
PREVIEW and its existing optimizer runs remained unchanged.

The earlier 435000 -> 295000 -> 291000 -> 282000 result is not a reliable
best-chain measurement. Accepted benchmark runs inherited legacy unscoped manual
rows when reloaded, so their stored score and next visible source schedule differed.

## Baseline run

- Command: `python manage.py benchmark_optimizer --schedule-block-id 5 --domain Physician --runs 10 --mode best-chain`
- Date/time completed: 2026-08-17 15:35 America/New_York
- Schedule block ID: 5
- Domain: Physician
- Mode: `best-chain` (`CURRENT_SCHEDULE` candidates after selecting the active completed run)
- Schedule Block status before/after: `PREVIEW` / `PREVIEW`
- Initial run/score: run 35 / 435000.0
- Runs attempted by the optimizer: 10 of 10
- Runs completed: 10
- Runs failed inside the optimizer: 0
- Best score: 185000.0
- Absolute improvement: 250000.0
- Percent improvement: 57.47%
- Accepted improvements: 10
- Accepted improvement path: 435000 -> 295000 -> 285000 -> 275000 -> 265000 -> 250000 -> 220000 -> 210000 -> 205000 -> 190000 -> 185000
- Accepted best scores monotonically improved: yes
- Total runtime: 34.52 seconds

## Safety and interpretation

Independent random attempts remain available through `--mode independent`, but
they are not the main Atlas product benchmark. Atlas needs the lowest total score
reachable through repeated improvement of the best known schedule, not the score
distribution of unrelated fresh fills.

Best-chain executes inside one outer database transaction. Every
candidate is a new optimizer run whose read-only source is the current best run.
An improving candidate becomes the next source; a non-improving candidate is
rejected and the previous best remains the next source.

Persistence is explicit:

- The default `persistence_mode` is `rollback`. The whole chain is rolled back
  after reporting, so no benchmark OptimizerRun, assignment, active-run change,
  or locked-open projection is retained. Summary output calls the winning ID
  `temporary_best_run_id`; it is not a durable workspace run.
- `--retain-best` is available only with `--mode best-chain`. It deletes every
  losing benchmark run and its run-scoped assignments before commit, retains only
  the winning benchmark-created run, and reports it as `retained_best_run_id`.
  The retained run has `run_kind=BENCHMARK`, a best-chain note, and retained-best
  summary metadata, so it is identifiable and selectable in Build Workspace.
  The workspace's pre-benchmark active run, version score cache, and locked-open
  projection are restored; existing/source runs and Schedule Block lifecycle are
  unchanged. If no candidate beats an already-persisted source, no duplicate run
  is retained and `retained_best_run_id` is null.

A later best-chain may start from any completed scored run in the selected Schedule
Version with `--source-run-id <id>`. The source may be active or inactive and is
read-only. This supports continuing from a retained benchmark result:

```text
python manage.py benchmark_optimizer --schedule-block-id 5 --domain Physician --runs 10 --mode best-chain --retain-best
python manage.py benchmark_optimizer --schedule-block-id 5 --domain Physician --runs 10 --mode best-chain --source-run-id <retained_best_run_id> --retain-best
```

Console and JSON output include `persistence_mode`, `source_run_id`,
`retained_best_run_id`, and `schedule_changes_retained`. Rollback reports never
present a temporary winning run ID as durable.

BUILD and PREVIEW Schedule Blocks are accepted. The command prints the Schedule
Block status before and after the benchmark and never changes lifecycle status.
Each candidate is a real optimizer run with `run_kind=BENCHMARK`, and the command
labels it as such in console, JSON, and CSV output. Benchmark-created run IDs in
the report are transient. Total score alone controls acceptance; component scores
are diagnostics only.

## Assessment

The prior conclusion that the optimizer stalled at 282000 was invalid because the
benchmark source snapshots were contaminated by legacy assignment leakage. After
isolation and the hard source/start guard, all ten candidates improved monotonically
to 185000. That validates this benchmark chain, not the optimizer strategy in
general. Algorithm work remains paused until assignment ownership correctness is
accepted and repeated runs confirm the invariant.

## CURRENT_SCHEDULE audit

Best-chain preserved best-so-far correctly. It passed each accepted candidate as
the next `source_run` and retained the 282000 run when later candidates scored
308000-313000.

The worse candidates are not caused by fresh-fill fallback or partial assignment
loading. Audit output showed `assignments_same_at_start=yes`, all visible source
assignment pairs copied, and zero assignment changes before the first scoring step.
Locked assignment and locked-open metadata follow the selected source run.

The measured root cause is a score-to-persisted-visible-state mismatch involving
legacy unscoped manual assignments. The optimizer allows unlocked manual assignments
to move and scores the resulting state. It persists that state on the candidate run,
but normal non-copy run visibility then unions legacy unscoped manual rows back into
the run. A moved physician's old manual row can therefore reappear alongside the
new optimizer assignment after final scoring. Assignment-pair counts in the real
chain grew from 224 to 225 and then upward across accepted candidates.

The accepted run consequently keeps a score describing its in-memory optimized
state, while the next CURRENT_SCHEDULE attempt correctly loads a larger visible
assignment set. For example, the accepted 295000 run's visible assignments
recomputed to 381000 even with internal night heuristics disabled. Its next
candidate improved that actual loaded state to 316000, but correctly lost the
benchmark comparison against the stale stored 295000 score.

`score_audit.total_score` matches `final_score` at the moment both are calculated;
the arithmetic is consistent. They become stale relative to visible run state when
legacy manual rows are reintroduced by run visibility after persistence. Locked-open
projection removed zero assignments in the measured chain, so it was ruled out as
the cause here. The optimizer does not fresh-fill or partially copy the source in
the audited iterations.

There is also a secondary objective-alignment risk: optimizer `initial_score` and
move acceptance enable internal night-search heuristics, while persisted final
total disables them. That difference can allow search-objective improvement without
official-total improvement, but it did not explain the observed 295000-to-381000
recomputation; legacy assignment leakage did.

## Correctness rule

Benchmark runs now own isolated assignment snapshots. Unlike legacy ordinary
optimizer runs, `BENCHMARK` visibility includes only rows scoped to that exact run;
schedule-version-level legacy manual rows are not unioned back into the snapshot.
Locked and unlocked manual assignments are copied into the candidate run, locked
rows remain fixed, locked-open IDs are preserved, and the source run is read-only.

Before every CURRENT_SCHEDULE attempt, best-chain recomputes the official total
directly from the source run's authoritative visible assignments. The optimizer
reports its official `initial_score` immediately after copying that same assignment
set and before fill, repair, or search mutation. A benchmark is valid only when:

`source_recomputed_score == optimizer_initial_score`

The command also requires source and loaded assignment counts/pairs to match. Any
mismatch aborts and rolls back the entire benchmark with source/candidate IDs,
stored and recomputed scores, assignment counts, assignment IDs, shift-instance
IDs, and missing/extra pairs. Algorithm improvement work remains stopped until
this run-assignment correctness invariant remains green.

## Concrete next algorithm recommendation

Do not add perturbation yet. First resolve whether legacy unscoped manual rows should
remain visible on run-scoped optimizer results after those assignments move, or
whether each run must own an isolated assignment snapshot. Then decide whether
internal night heuristics belong in the official total or must remain search guidance
that cannot approve a worse official total. Both decisions precede a new strategy.

## Verification

`python manage.py test apps.scheduling -v 1` passed all 102 scheduling tests after
benchmark snapshot isolation and the CURRENT_SCHEDULE correctness guard.
