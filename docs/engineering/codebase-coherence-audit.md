# Atlas Codebase Coherence Audit

Date: 2026-07-11

## Executive assessment

Atlas has a coherent product model and a generally understandable Django/React foundation, but the Schedule Build Workspace implementation is no longer coherent enough for additional optimizer features without a short cleanup phase.

The core domain boundaries in `docs/engineering/atlas-source-of-truth.md` are still visible in the models: Schedule Blocks contain domain-scoped Schedule Versions; shift instances are dated requirements; assignments belong to instances; optimizer runs belong to versions. The recent workflows also have meaningful end-to-end tests.

The risk is concentrated rather than repository-wide. Optimizer run history, manual edits, locked-open state, score persistence, Save Copy, and start modes have accumulated overlapping representations and feature-specific branches. The code works under the tested paths, but correctness increasingly depends on knowing whether a run is active, viewed, copied, legacy, or newly generated. This is a fragile basis for more optimizer work.

**Recommendation: pause new optimizer behavior for a bounded cleanup milestone.** Continue incremental work elsewhere if needed, but do not add another optimizer workflow until the run-state boundary has one canonical representation and contract. This is not a recommendation for a broad rewrite.

## Repository shape

The main complexity is highly concentrated:

- `backend/apps/scheduling/optimizer.py`: about 5,700 lines.
- `backend/apps/scheduling/tests.py`: about 4,750 lines.
- `backend/apps/scheduling/api.py`: about 2,350 lines.
- `frontend/src/components/ScheduleBuildWorkspace.tsx`: about 1,820 lines.
- `backend/apps/scheduling/serializers.py`: about 640 lines.
- `backend/apps/scheduling/models.py`: about 470 lines.

This concentration is not automatically wrong, but the largest files now own multiple architectural responsibilities and are difficult to change independently.

## Backend architecture

### What is coherent

- The main persisted relationships are explicit and aligned with the source of truth in `backend/apps/scheduling/models.py`: `ScheduleVersion` belongs to a block and domain; `OptimizerRun` belongs to a version; `ScheduleShiftInstance` belongs to a version; assignments belong to instances.
- Optimizer runs have an enforced per-version run number and a single-active-run database constraint (`backend/apps/scheduling/models.py`, `OptimizerRun.Meta`).
- Assignment uniqueness distinguishes legacy manual rows, run-scoped manual rows, and optimizer rows. The constraints are explicit and migrations reproduce them.
- Mutating run operations use transactions and row locks in important paths such as optimizer execution, Save Copy, activation, and score recalculation.
- Permission and BUILD-lifecycle checks are consistently present on Build Workspace endpoints.

### High-risk coherence problems

#### 1. A schedule run is not represented by one authoritative aggregate

The effective state of a viewed schedule is assembled from several places:

- assignments in `ScheduleShiftAssignment`;
- legacy manual assignments with `optimizer_run_id = NULL`;
- run-scoped assignments with `optimizer_run_id` set;
- `ScheduleShiftInstance.is_locked_open`;
- `OptimizerRun.locked_open_shift_instance_ids`;
- `ScheduleVersion.optimizer_summary`;
- `OptimizerRun.optimizer_summary`, `score_breakdown`, `optimizer_debug`, and score columns;
- `ScheduleVersion.score_is_stale` and `OptimizerRun.score_is_stale`.

The visibility rule is repeated in `backend/apps/scheduling/api.py::_visible_assignment_filter`, `backend/apps/scheduling/optimizer.py::_assignments_for_optimizer_run`, and `backend/apps/scheduling/serializers.py::ScheduleShiftInstanceSerializer._visible_assignments`. The rules are similar but not structurally shared. `run_kind == "COPY"` changes the query semantics in each location. A future change can easily update one path but not the others.

This is the primary architectural risk.

#### 2. Locked-open state has two writable sources of truth

`ScheduleShiftInstance.is_locked_open` is mutable workspace state, while `OptimizerRun.locked_open_shift_instance_ids` snapshots run state. Save Copy and run activation copy values between them in `backend/apps/scheduling/api.py::optimizer_run_save_copy` and `optimizer_run_activate`. Manual open/assignment endpoints update both through `_set_active_run_locked_open`.

Consequences:

- viewing a run and activating a run do not have the same effect;
- serializers read the run JSON when a run ID is present but otherwise read the instance field;
- optimizer startup rewrites instance flags from source-run metadata;
- correctness depends on synchronization code being called on every mutation path.

The JSON list also lacks a foreign-key constraint, so deleted or cross-version instance IDs can only be prevented procedurally.

#### 3. Manual assignment scope is conceptually overloaded

`ScheduleShiftAssignment.optimizer_run` originally described optimizer ownership. It now also scopes manual assignments. The schema supports this, but the name and legacy behavior obscure the concept:

- legacy manual rows are version-wide (`optimizer_run_id = NULL`);
- newer manual rows may be run-scoped;
- copy runs exclude legacy rows through a special `run_kind == "COPY"` branch;
- non-copy runs combine legacy rows with run rows.

See `backend/apps/scheduling/models.py` assignment constraints and the three visibility implementations cited above. This is compatible with legacy rows, but it is not a generalized schedule-state model.

#### 4. Active run and viewed run are different concepts but APIs mix them

Read APIs accept a viewed run ID. Manual edit APIs derive `_active_optimizer_run` from the version instead of accepting the viewed run. The frontend prevents editing a non-active run, which currently keeps the normal UI safe, but the backend contract itself does not express “edit this run.” This is hidden coupling between UI gating and backend mutation semantics.

Relevant code:

- viewed selection: `backend/apps/scheduling/api.py::schedule_block_build_context`;
- active lookup: `_active_optimizer_run`;
- assignment writes: `schedule_shift_assignments` and `schedule_shift_assignment_detail`;
- UI gating: `frontend/src/components/ScheduleBuildWorkspace.tsx`, `canEditAssignments`.

An API caller that omits the UI assumptions can address an assignment ID from one run while mutation logic validates against another run's active state.

#### 5. Score persistence is duplicated

The latest summary is stored on both `ScheduleVersion` and `OptimizerRun`. Recalculation updates both in `backend/apps/scheduling/optimizer.py::recalculate_schedule_version_score`; optimizer completion also updates both; activation copies a run summary back to the version; clear operations blank the version summary.

This duplication is useful as a cache, but it is not declared or enforced as a cache. There is no invariant specifying whether version fields must always equal the active run, and `score_is_stale` is likewise duplicated. Divergence is possible after partial or newly added mutation paths.

#### 6. Endpoint organization contains legacy duplication

There are two optimizer execution endpoints with nearly identical orchestration:

- `/schedule-blocks/{block}/build/versions/{version}/optimize/`;
- `/schedule-versions/{version}/run-optimizer/`.

Their functions in `backend/apps/scheduling/api.py` duplicate seed parsing, start-mode parsing, running-run conflict handling, exception conversion, and optimizer invocation. Both are registered in `backend/apps/scheduling/urls.py`. This creates a direct risk that a future validation or response change reaches only one endpoint.

The entire scheduling API module also contains requests, contracts, block lifecycle, workspace reads, optimizer runs, assignment editing, and publishing. The boundaries exist as function groups but not modules.

### Serializer and response construction

- The Build Workspace response is hand-assembled in `schedule_block_build_context`, while related endpoints return serializers or optimizer-produced dictionaries.
- The violation report is hand-assembled inside `optimizer.py`, coupling report schema to scoring internals.
- `ScheduleShiftInstanceSerializer` queries `OptimizerRun` from serializer methods to resolve run kind and locked-open IDs. Context caching limits repeated work within one serializer invocation, but the serializer is responsible for domain-state resolution rather than presentation alone.
- Type shapes are duplicated manually in TypeScript rather than generated or shared from a schema.

These patterns are serviceable at current scale, but response contracts are implicit and vulnerable to the class of runtime field/key errors that static contract tests would catch.

## Frontend architecture

### What is coherent

- The workspace keeps server state refreshed after mutations rather than attempting extensive optimistic reconstruction.
- URL query state preserves the selected optimizer run across reloads.
- Active and viewed runs are visibly distinguished, and non-active runs are gated from editing.
- Errors are generally caught and converted into user-facing messages.
- Long-running actions have separate disabled/loading state.

### Risks

#### 1. `ScheduleBuildWorkspace` is a monolith

`frontend/src/components/ScheduleBuildWorkspace.tsx` owns:

- API types and URL construction;
- workspace fetching;
- URL/run synchronization;
- run selection, activation, deletion, Save Copy, rescore, and optimizer execution;
- optimizer summary/debug rendering and clipboard behavior;
- calendar generation and rendering;
- manual-assignment popover positioning, loading, editing, locking, opening, and deletion;
- all loading, notice, and error states.

It has more than 20 `useState` calls and multiple effects coordinating refs, URL state, server state, and derived defaults. This makes local changes difficult to reason about and raises stale-closure/race risks.

#### 2. There is no API client boundary

`API_BASE` is hard-coded in multiple components, and `fetch`/JSON/error handling is repeated. The workspace alone has calls for context, activation, deletion, generation, optimizer execution, rescore, Save Copy, clear operations, and assignment mutations. Payload and response types are compile-time assertions only; runtime responses are not validated.

#### 3. Run selection has several synchronized representations

The selected run exists in:

- React state (`selectedOptimizerRunId`);
- a mutable ref (`selectedOptimizerRunIdRef`);
- the URL query parameter;
- `context.selected_optimizer_run`;
- derived `selectedRunForActions`.

The code deliberately synchronizes these, but the number of representations is a warning sign. `fetchContext` conditionally updates them based on whether a requested ID was supplied. This is a plausible source of refresh and race defects.

#### 4. Start-mode defaulting is effect-driven

The optimizer start mode resets when selected run/version IDs change by inspecting `context.shift_instances`. This is understandable, but it couples a user control default to the timing of context replacement. It should eventually be derived through a small run-workspace state boundary and tested explicitly.

#### 5. Error handling is broad but repetitive

Most operations have appropriate fallback messages, but each implements its own fetch/parse/check/catch/finally sequence. Some operations refresh in `finally`, others refresh only on success, and assignment errors use a modal-local channel while run errors use a workspace-global channel. Consistency depends on convention rather than a shared mutation helper.

### Frontend testing gap

No frontend test or specification files are present. The production TypeScript/Vite build catches syntax and type errors, but it does not verify:

- selected versus active run rendering;
- default start-mode selection;
- Save Copy switching to the new run;
- stale-score warning lifecycle;
- manual modal behavior;
- button payloads and labels;
- error rendering after a 400/409/500 response.

## Optimizer architecture

### Strengths

- Scoring is substantially separated into `_score_schedule` and focused helpers.
- Schedule state is represented as an in-memory mapping during search rather than mutated in the database for every trial.
- Validity checks, scoring rows, night reports, workload calculations, and candidate generators have named helper functions.
- Locked manual assignments are represented as `manual_pairs` and excluded from replaceable optimizer pairs.
- Score audit output checks component/report consistency.
- The optimizer uses a seed and persists it for reproducibility.

### Weaknesses

#### 1. The optimizer module contains four layers

`backend/apps/scheduling/optimizer.py` contains:

- contract/rule parsing;
- scoring and validation;
- violation report construction;
- database loading/persistence and run orchestration;
- search phases and debug instrumentation.

Although there are many helpers, `optimize_schedule_version` remains a multi-thousand-line orchestration function. Search behavior, persistence behavior, and audit serialization cannot be tested or changed independently without substantial setup.

#### 2. Start-state construction is embedded inside optimizer execution

Current Schedule versus Fresh Fill, source-run loading, legacy manual compatibility, cloning manual seed rows, locked-open synchronization, and new-run creation occur inside `optimize_schedule_version`. This is the highest-priority extraction boundary because it is policy about run state, not scoring math or local search.

#### 3. Constraints are procedural rather than represented uniformly

Overlap, rest, capacity, facility eligibility, manual locks, and locked-open shifts are handled across different helper signatures and data structures. There is no single immutable “fixed constraints” input. Candidate functions accept different subsets of `manual_pairs`, contracts, requests, and state. This makes it difficult to prove every search phase honors the same invariants.

#### 4. Debug output is valuable but ad hoc

The debug payload is a large dictionary assembled at optimizer completion. It mixes stable audit facts, counters, examples, phase-specific details, score breakdowns, warnings, and runtime metadata. Consumers access it as `Record<string, unknown>` in the frontend. There is no version marker or documented stable schema.

#### 5. Report and score are coupled correctly but physically colocated

Recalculation appropriately uses `build_violation_report`, which in turn calls the same scoring helpers. That is good for numeric consistency. However, violation report construction and persistence live in the optimizer module, expanding its responsibility and making report schema changes risky.

## Duplication and stitched-on indicators

The following are concrete signs of incremental stitching:

1. Three assignment-visibility implementations with `COPY` special cases.
2. Two optimizer execution endpoints and orchestration functions.
3. Locked-open state in both an instance Boolean and a run JSON list.
4. Score summary and stale flags on both version and run.
5. `optimizer_run` serving both provenance for optimizer rows and scope/ownership for manual rows.
6. `run_kind` being a free-form string while `start_mode` and status use choices.
7. Hand-maintained frontend types duplicating serializer fields.
8. `ScheduleBuildWorkspace` combining API, state machine, calendar, run history, score panels, and modal UI.
9. `optimizer.py` combining scoring, search, reporting, persistence, and debug schema.
10. Backward-compatibility behavior being selected by nullable foreign keys and `run_kind` branches rather than an explicit schedule-state abstraction.

These do not mean the features are fundamentally wrong. They mean the next feature will have too many places to update safely.

## Migration and schema health

### Current status

- Scheduling migrations are linear and ordered through `0020_optimizer_run_start_mode.py`.
- The model state matches the migration graph (`makemigrations --check --dry-run` reports no changes at audit time).
- Recent fields have safe defaults or nullability for legacy rows:
  - stale flags default to false;
  - copy provenance is nullable;
  - `run_kind` defaults to `OPTIMIZER`;
  - locked-open ID lists default to an empty list;
  - `start_mode` defaults to `FRESH_FILL`.
- Assignment constraint replacement in migration `0019` is ordered correctly: remove the old conditional constraint, then add legacy and run-scoped constraints.

### Risks

- Defaults make old rows load, but do not prove semantic equivalence. In particular, old runs receive an empty locked-open snapshot and `FRESH_FILL` regardless of how they were originally created.
- `run_kind` has no choices or database constraint.
- `locked_open_shift_instance_ids` is unvalidated JSON and can contain stale or cross-version IDs.
- Applying model-dependent code before running migrations will cause Build Workspace 500 errors. The backend test suite creates/migrates a test database and therefore does not test deployment against an application database that is missing a recent migration.
- There is no automated startup/deployment check that migrations are applied before serving requests.

## Testing health

### Strengths

- The scheduling suite is large and behavior-oriented. It covers requests, generation, manual assignment eligibility, overlap, optimizer scoring and seeds, run history, activation, violation reports, score recalculation, Save Copy isolation, locks, locked-open shifts, and start modes.
- Recent tests snapshot source assignments to prove copy/start operations do not mutate source runs.
- Tests verify score/report agreement and no additional run during rescore.
- Clean-database migration application occurs during normal Django test setup.

### Gaps

#### Tests that would better protect invariants

1. A parameterized assignment-visibility matrix covering legacy manual, scoped manual, optimizer, copy, active, inactive, and no-run contexts across context API, serializer, violation report, and optimizer loader.
2. A locked-open invariant matrix covering view, activation, copy, manual edit, Current Schedule, Fresh Fill, and deletion of referenced instances.
3. An invariant that `ScheduleVersion.optimizer_summary` and `score_is_stale` equal the active run after every run mutation, or an explicit test documenting when they need not.
4. Direct API tests proving assignment mutation cannot target a non-active/viewed run mismatch.
5. Both optimizer execution endpoints should be contract-tested with identical inputs until one is retired.
6. Failure/rollback tests for Save Copy and optimizer startup midway through assignment cloning.
7. Legacy-row migration fixtures rather than relying only on model defaults in a newly built database.
8. A test that deleting a copied run removes all scoped manual and optimizer assignments.

#### Would tests catch the recent Build Workspace 500 class?

Only partially.

- They would catch missing model imports, invalid serializer field declarations, migration/model drift, and many runtime response-construction errors when the relevant endpoint test runs.
- They would not catch a deployed development database that has not applied migrations `0018`-`0020`; the test database is migrated independently.
- There is no frontend smoke test that loads Build Workspace against a migrated backend and fails on a 500 response.
- There is no response-schema validation that checks the full Build Workspace payload against the TypeScript contract.

Therefore the suite is strong at backend behavioral regression but weak at integration/deployment and browser contract failures.

## Highest-priority cleanup before more optimizer work

1. **Define one run-state service and invariant document.** Centralize assignment visibility, fixed/manual locks, locked-open state, source-run loading, and active/viewed validation behind a small backend module. All serializers, reports, copies, edits, and optimizer starts should consume it.
2. **Extract optimizer run orchestration from scoring/search.** Move database run creation, seed-state preparation, cloning, persistence, activation, and stale-summary synchronization out of `optimizer.py`. Keep scoring math and search behavior unchanged and protect with characterization tests.
3. **Split the Build Workspace frontend by responsibility and add smoke tests.** Introduce a typed API client plus focused run-controller and assignment-editor hooks/components. Preserve rendering and behavior while testing run selection, start-mode payloads, copy switching, stale score, and 500 handling.

## What should not be refactored yet

- Do not redesign Schedule Block, Schedule Version, Shift Instance, Contract, Request, or publication concepts.
- Do not rewrite scoring math or local-search phases while extracting orchestration.
- Do not introduce a new global state library solely for this cleanup; local hooks/reducers are sufficient.
- Do not replace Django REST Framework or React.
- Do not normalize every JSON debug field before deciding which fields are a supported audit contract.
- Do not remove legacy endpoints or nullable legacy assignment support until usage and data migration are explicitly planned.
- Do not combine this cleanup with new optimizer rules, new contract enforcement, or lifecycle changes.

## Exact next three engineering steps

### 1. Characterize and specify run-state invariants

Create a short run-state design note and a parameterized backend test suite before moving code. Specify, for every run kind and legacy state:

- which assignments are visible;
- which assignments are fixed versus seed-only;
- where locked-open state is read;
- what active and viewed mean for reads and writes;
- which object owns score/stale state.

This step changes no production behavior.

### 2. Introduce a run-state/orchestration service under characterization tests

Extract the repeated visibility query and start-state preparation into one module. Route Build Workspace serialization, violation reporting, Save Copy, assignment mutation validation, and optimizer start modes through it. Then consolidate the two optimizer endpoint implementations behind one internal command while retaining both URLs and response shapes initially.

This should be a behavior-preserving refactor with the existing suite plus the new invariant matrix green throughout.

### 3. Establish the frontend/API integration seam

Add a small typed scheduling API client and split `ScheduleBuildWorkspace` into run controls, score/details, calendar, and assignment editor boundaries. Add component/integration smoke tests for initial load, viewed/active switching, Save Copy, Recalculate Score, start-mode payloads, stale warnings, and backend 500 handling. Add a deployment check that fails when migrations are unapplied.

Only after these three steps should new optimizer behavior resume.

## Final recommendation

Atlas is coherent enough to preserve and improve incrementally; it does not need a rewrite. It is **not coherent enough to safely continue layering optimizer features today**. Pause optimizer feature work for the three bounded steps above, then resume incremental development. The cleanup target is the run-state boundary, not the whole application.
