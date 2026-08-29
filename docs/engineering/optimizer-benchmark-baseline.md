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

Best-chain can continue automatically in bounded batches with `--until-stalled`.
`--batch-runs` controls optimizer attempts per batch, `--max-batches` provides a
hard outer bound, and `--stall-batches` controls how many consecutive
non-improving batches stop the command. Defaults are 10, 10, and 3 respectively.
Each improving retained batch persists only its winning BENCHMARK run and uses it
as the next read-only source. Losing runs are removed, existing runs remain, and
workspace active state and Schedule Block lifecycle are restored. In rollback
mode, temporary batch winners remain usable only inside one outer transaction;
the entire multi-batch chain is rolled back and no temporary ID is reported as
durable.

```text
python manage.py benchmark_optimizer --schedule-block-id 5 --domain Physician --mode best-chain --source-run-id 35 --retain-best --until-stalled --batch-runs 10 --max-batches 10 --stall-batches 1
```

The exact command above was verified on 2026-08-28. Batch 1 improved the selected
run-35 chain from 435000 to 25000 and retained run 380. Batch 2 tied at 25000, so
the command stopped as stalled after 2 batches and 20 optimizer runs. Total runtime
was 141.25 seconds. Run 35 remained completed at 435000, the Schedule Block
remained PREVIEW, and the previously retained score-50 run remained available;
the command did not silently replace the explicitly selected source chain with a
different existing run.

### Benchmark randomness modes

Benchmark search is stochastic by default. When `--seed-base` is omitted, every
optimizer attempt receives a newly generated seed, including attempts from the
same unchanged best source and attempts in later until-stalled batches. Batch and
iteration output records the exact seeds used. The default `--stall-batches` is 3,
so a serious stochastic search tests multiple distinct batches before declaring a
stall.

Passing `--seed-base` selects deterministic, reproducible mode. Seed `N` starts the
sequence `N`, `N+1`, and so on across batch boundaries. This mode is intended for
focused debugging, regression tests, and replaying a known path: the same source
run and seed reproduce the same optimizer choices. An early stall from one
deterministic sequence does not establish a schedule plateau.

Real search should omit `--seed-base`, keep multiple attempts per batch, and use
multiple stall batches. Best-chain still holds the current best source until a
strictly better total score is found; stochastic attempts explore different paths
without mutating or overwriting that source.

A rollback smoke test from retained run 368 confirmed the distinction on
2026-08-28. Two attempts from the same score-50 source used seeds
`5006929684373504904` and `2555430668651092633`; the first returned 50 and the
second found 0. The temporary score-0 run was rolled back, run 368 remained
unchanged, and the Schedule Block remained PREVIEW. Different seeds are not
guaranteed to produce different scores every time, but they now drive genuinely
different seeded search paths.

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

## Verified workload range scoring

Workload range penalties are linear per unit of deviation, not flat once a
physician leaves the configured range. For each applicable period rule:

- below minimum: `(minimum - assigned value) * minimum penalty weight`;
- above maximum: `(assigned value - maximum) * maximum penalty weight`;
- inside the inclusive minimum/maximum range: zero.

The assigned value is hours for an `HOURS` rule and shift count for a `SHIFTS`
rule. Focused tests verify that an HOURS range of 45-55 with weight 10000 scores
55/56/57/58 hours as 0/10000/20000/30000 and 44/43 hours as 10000/20000.
They also verify that one and two shifts outside a SHIFTS range produce one and
two penalty units in either direction. This documents existing scoring behavior;
no optimizer or scoring implementation changed.

## Concrete next algorithm recommendation

The optimizer now includes a narrow `final_plateau_repair` phase after its existing
repair and hill-climb phases. It uses the official persisted total-score objective
(without internal night-search heuristics) and targets only remaining workload-range
deviations, post-night recovery violations, and next-night-block spacing violations.
It tries legal reassignment candidates and targeted night swaps, preserves coverage,
locked manual assignments, and locked-open instances, and accepts a candidate only
when `new_total_score < current_total_score`. Workload ranges and scoring math are
unchanged.

Debug output records total attempts/accepts, workload and night repair attempts and
accepts, scores before/after the phase, and a final reason (`improved`,
`no_target_violations`, `no_legal_improving_move_or_swap`, or `runtime_limit`). This
is a bounded plateau cleanup, not broad perturbation, simulated annealing, or tabu
search. Broader strategy changes remain deferred.

### Retained run 279 plateau diagnostic

The read-only command below exhaustively tests violation-involved single-shift
reassignments and pairwise swaps and emits structured rejection reasons without
mutating the selected run:

```text
python manage.py explain_optimizer_plateau --schedule-block-id 5 --domain Physician --optimizer-run-id 279
```

For retained run 279 (score 60000), the diagnostic found no improving single-shift
move, but it did find improving pairwise swaps. The best tested delta was -15000 by
swapping Bauerband's Nov 3 Sawmill night shift (instance 48) with Miller's Nov 6
Sawmill night shift (instance 96). Therefore the 60000 result is not a local optimum
under the diagnostic's tested pairwise-swap neighborhood: the bounded final repair
did not reach a legal improving swap. This finding does not prove a global optimum,
global impossibility, or overall schedule feasibility.

The optimizer and diagnostic now share the same official-score pairwise-swap
evaluator. Final plateau repair prioritizes assignments involved in remaining
violations on both sides of a swap before considering the broader mutable assignment
set. Its debug payload includes `pairwise_swaps_attempted`,
`pairwise_swaps_accepted`, `best_pairwise_swap_delta`, and
`accepted_pairwise_swap_details`.

The required retained best-chain rerun completed from run 279:

```text
python manage.py benchmark_optimizer --schedule-block-id 5 --domain Physician --runs 10 --mode best-chain --source-run-id 279 --retain-best
```

Iteration 1 accepted the diagnostic -15000 swap and improved 60000 to 45000.
Iteration 2 improved to 40000. The remaining eight candidates stayed at 40000.
The final retained best is run 309, with workload score 20000, night score 20000,
and every other score component zero. The Schedule Block remained PREVIEW and
run 279 remained unchanged.

### Workload micro-overage repair from run 319

Final plateau repair now gives workload-specific pairwise swaps a separate,
duration-aware candidate budget. Over-maximum physicians are paired with
below-minimum or inside-range receivers, and longer donor shifts are tested
against shorter receiver shifts. Every candidate is still evaluated using the
complete official score and is accepted only when the total strictly decreases;
workload ranges, penalties, and eligibility rules are unchanged. Locked manual
assignments and locked-open instances remain immutable.

Debug output records the considered over-maximum physicians, workload move and
swap counts, best total delta, best rejected reason, and accepted workload repair
details. The plateau diagnostic now reports workload move and swap attempt counts,
best workload-component and total deltas, and the most common rejection reasons.

The required retained best-chain rerun from run 319 completed on 2026-08-24:

```text
python manage.py benchmark_optimizer --schedule-block-id 5 --domain Physician --runs 10 --mode best-chain --source-run-id 319 --retain-best
```

The chain improved 40000 -> 30000 -> 20050 and retained run 329. The remaining
eight attempts tied at 20050. The Schedule Block remained PREVIEW, the source run
was not mutated, and only the final best benchmark run was retained.

### Pairwise night-recovery rescan from run 339

Final plateau repair now rebuilds post-night recovery violations and its targeted
pairwise candidate set after every accepted swap. The pairwise pass prioritizes
violation-involved assignments and other assignments owned by the involved
physicians. It covers the broader movable assignment set only for small schedules,
and remains bounded by the optimizer runtime guard, a total attempt cap, and a
round cap. Every accepted swap still must strictly lower the complete official
score and pass the existing lock, coverage, eligibility, overlap, and rest checks.

Debug output records rescan rounds, candidates considered, candidates skipped by
the cap, why candidates may have remained unevaluated, and accepts after a rescan.

The required retained best-chain rerun from run 339 completed on 2026-08-28:

```text
python manage.py benchmark_optimizer --schedule-block-id 5 --domain Physician --runs 10 --mode best-chain --source-run-id 339 --retain-best
```

Iteration 1 improved 20050 -> 50 and retained run 358. Final plateau repair
accepted four night-recovery swaps over four rounds, including three accepts after
a rescan. It considered 632 targeted pairwise candidates, skipped none by the cap,
and reported no missed candidate. The remaining nine attempts tied at 50. The
Schedule Block remained PREVIEW, run 339 remained unchanged, assignment snapshots
matched at optimizer start, and only run 358 was retained.

## Verification

`python manage.py test apps.scheduling -v 1` passed all 102 scheduling tests after
benchmark snapshot isolation and the CURRENT_SCHEDULE correctness guard.
