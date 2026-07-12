# Schedule Build Workspace Run-State Architecture

This note defines the optimizer-run ownership boundary used by Schedule Build
Workspace. It is subordinate to `atlas-source-of-truth.md`: optimization is a
new search and never an invisible mutation of an existing result.

## Run roles

- **viewed_run** is the completed run selected by `optimizer_run_id` and shown
  in the workspace. Selection falls back to the active completed run, then the
  most recent completed run. Failed, timed-out, and running runs remain visible
  in history but are not resolved as editable schedule state.
- **active_run** is the single current editable run for a Schedule Version.
  The viewed and active runs may differ. A viewed run is editable only when it
  is also active.
- **source_run** is the completed viewed run supplied to Save Copy or to a new
  optimizer execution. It is read-only during either operation.
- **created_run** is the new run produced by Save Copy or optimizer execution.
  Creation never overwrites the source run.

These are code-level roles, not new database fields. `OptimizerRun.is_active`
continues to persist active ownership. The shared rules live in
`backend/apps/scheduling/run_state.py`.

## Workspace API contract

The Build Workspace context includes `run_state`, containing `viewed_run_id`,
`active_run_id`, and explicit booleans for edit, activation, copy, and optimizer
source eligibility. Existing `selected_optimizer_run` remains as a compatibility
alias for the serialized viewed run.

Old runs whose `start_mode` was introduced by a later migration remain
loadable. Start mode metadata does not decide whether a completed run can be
viewed. Failed/running runs cannot be activated, copied, rescored, edited, or
used as an optimizer source.

## Action ownership

| Action | Mutates | Creates a run |
| --- | --- | --- |
| View/select | Nothing | No |
| Activate | Active pointer, version cache, instance locked-open projection | No |
| Manual assign/remove/lock/open | Viewed run, only when viewed is active | No |
| Recalculate Score | Viewed active run's score/report/debug data | No |
| Save Copy | Newly created exact duplicate; then active/viewed state | Yes |
| Run optimizer | Newly created run | Yes |

Manual edits mark only the edited run stale. `ScheduleVersion` score fields are
the cache for the active run and are updated with active-run mutations.
Assignment visibility and locked-open snapshots are resolved using the viewed
run; legacy unscoped manual rows remain supported for non-copy runs.

## Optimizer start modes

`CURRENT_SCHEDULE` uses the viewed completed run as `source_run`. Its visible
assignments seed the new search. Locked manual assignments and locked-open shift
instances are fixed; unlocked assignments are seed state and may move. The
optimizer creates `created_run` and leaves `source_run` unchanged.

`FRESH_FILL` also reads locked manual assignments and locked-open shift
instances from the viewed run, but does not copy unlocked assignments wholesale
as starting state. It creates a new run and leaves the viewed source unchanged.

Both public optimizer endpoints call the same backend orchestration command so
validation, source selection, running-run conflict handling, and error mapping
cannot drift.
