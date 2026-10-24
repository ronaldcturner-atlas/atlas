# Atlas Product Source of Truth

## Purpose

This document records product decisions that are already established by the current Atlas codebase and engineering documentation. It is a guardrail for future implementation: preserve these concepts and boundaries unless the product owner explicitly changes them.

This is a product-behavior summary, not a claim that every roadmap phase is complete. Where roadmap language and the running code differ, the code describes current behavior while the roadmap describes sequencing and future scope.

### Maintenance cadence

This source of truth must be read and compared with the current codebase and settled product decisions at least once every 24 hours. If behavior, terminology, permissions, workflows, or known future work have changed, this document must be updated during that review. The reviewer must not add assumptions merely to make the document appear complete; unresolved questions remain explicit until the product owner settles them.

### Authoritative development and deployment path

Atlas is one responsive product, not separate desktop and mobile applications. Desktop, mobile-browser, and installed-PWA experiences must use the same backend, permissions, schedule data, and workflow decisions.

The only authoritative development path is:

1. the primary local checkout at `C:\Users\turne\Desktop\atlas`;
2. the local `mobile-beta` branch in that checkout;
3. the GitHub `mobile-beta` branch in `ronaldcturner-atlas/atlas`;
4. every active Railway service in the `mobile-beta` environment deployed from that same GitHub branch and commit.

Local Docker services must mount the primary checkout above. Additional worktrees, temporary branches, copied source trees, exported databases, and test snapshots may exist only for a bounded repair or verification task. They are never authoritative, must be clearly identified while in use, and must be removed after their verified changes have been incorporated into `mobile-beta`.

The Railway `mobile-beta` database is the authoritative shared beta-test data store. A local database may be a disposable point-in-time clone for safe development and optimizer testing, but local and Railway databases are not bidirectionally synchronized. Local data must never overwrite Railway merely because it is newer locally. Production disaster-recovery backups are protected recovery artifacts, not alternate active development copies.

## Product Principles

1. Atlas is a physician scheduling platform.
2. Atlas provides configurable mechanisms, not one organization's scheduling policies.
3. Prefer simple, explicit workflows over hidden automation.
4. A feature should solve a real scheduling problem before it is added.
5. Product decisions precede implementation decisions.
6. Each optimization Run is an independent, auditable search. Starting from an earlier schedule never silently mutates that source Run or the published Live Schedule.

## Core Concept Boundaries

- A **Facility** is a scheduling location and provides identity, timezone, color, and active status.
- A **Physician** is a schedulable clinician profile linked one-to-one to an authenticated user.
- A **Shift Template** is a reusable, facility-specific recurring definition. It has no physician and no calendar date.
- A **Shift** is a dated assignment of one physician to one facility.
- A **Schedule Block** is a dated planning and publication container with its own request window and lifecycle.
- A **Schedule Version** is a Domain-scoped workspace version inside a Schedule Block.
- A **Schedule Shift Instance** is a dated staffing requirement generated from a Shift Template inside a Schedule Version.
- A **Schedule Request** is a physician preference or administrative instruction for one date inside one Schedule Block.
- A **Contract** is a domain-specific bundle of configurable scheduling and request rules that can be assigned as a physician's default contract in that domain.

These concepts are related but must not be collapsed into one another. In particular, Shift Templates are not scheduled Shifts, Schedule Blocks are not Shift Templates, and Contracts are not schedules.

## Shift Builder

### Settled purpose

Shift Builder defines generic recurring Shift Templates using day-of-week rules. It describes what kinds of shifts a facility may staff, not who works them or on which specific dates.

### Template data

Each Shift Template has:

- one Domain;
- one Facility;
- start and end times;
- one or more active days selected from Sunday through Saturday;
- zero or more weekend designation days, limited to Friday, Saturday, Sunday, and Monday;
- a `night_shift` flag;
- a required staffing count, with a minimum and default of 1;
- an active/disabled state.

The template name is system-generated from the Facility short name and time range, for example `Berkeley 7a-4p`. Users do not supply an independent template name.

A weekend designation is not the same as an active day. A day may be designated as a weekend day only when it is also active for that template. Weekend status belongs to that specific Shift Template occurrence and is determined exclusively by the weekday on which the shift starts. The entire shift and all of its hours retain that status regardless of its end date or time; Atlas does not infer weekend status for other shifts on the same date or from colloquial labels such as "Sunday night." The old single `weekend_shift` boolean has been explicitly replaced by the `weekend_days` list.

For worked-weekend streaks, a weekend-designated Monday belongs to the immediately preceding Friday-Sunday weekend. Consecutive weekend shifts remain based on actual assignments on adjacent start dates.

Disabled templates remain historical records. Current selection workflows use active templates, and Request Builder further limits them to templates active on the selected weekday.

### Explicit boundaries

Shift Builder must not:

- assign physicians or other users;
- use specific calendar dates;
- create dated Shift records;
- connect templates directly to My Schedule;
- replace weekend day selections with a single weekend boolean.

## Schedule Blocks

### Settled purpose

A Schedule Block is the container for planning a bounded schedule period. It owns:

- schedule start and end dates;
- request open and close date/times;
- lifecycle status;
- creation and update timestamps;
- publication timestamp.

The displayed name is generated from the date range (`Jul 2026` or `Jul 2026-Aug 2026`); it is not independently editable. The end date cannot precede the start date, a block cannot span more than 12 calendar months, and request close must be later than request open.

The request status (`Not Open`, `Open`, or `Closed`) is derived from the configured request timestamps rather than stored independently.

### Lifecycle

The established statuses are:

1. `PRE_BUILD`
2. `BUILD`
3. `PREVIEW`
4. `ARCHIVE`

Status is workflow-controlled and cannot be edited as an ordinary field.

- New blocks begin in `PRE_BUILD`.
- Generating the first Schedule Build Workspace moves a `PRE_BUILD` block to `BUILD`.
- Only `PRE_BUILD` blocks can be deleted.
- `PRE_BUILD` and `BUILD` blocks can enter `PREVIEW`.
- `PREVIEW` is view-only. Schedulers can either publish it or move it back to `BUILD`; moving back preserves all schedule versions, optimizer runs, and assignments.
- Only `PREVIEW` blocks can be published.
- Publishing stamps `published_at`, moves the block to `ARCHIVE`, and records the exact active Optimizer Run used by each Schedule Version as the published schedule snapshot.
- Archived block definitions and publication lifecycle are read-only. An authorized scheduler may explicitly unpublish an archived block and return it to `BUILD`; until that happens, only authorized Live Schedule operations may change its published assignments.

Publication freezes the displayed score and violation diagnostics under the contracts and requests that existed at publication time. Opening a published block does not rerun feasibility checks, and later Contract changes do not mark its stored scores stale or reevaluate it. The published Optimizer Run is visibly identified in the Schedule Build Workspace and run history. Unpublishing is the explicit action that returns the block to `BUILD`, clears the publication snapshot, re-enables feasibility checks, and marks retained run scores for reevaluation against current rules.

On the Live Schedule, an assigned user may split their own shift and may recombine all portions when the same current user set remains assigned to every portion. Cancelled, declined, approved, or otherwise historical trade activity never blocks recombination; references from removed split portions are retained against the surviving recombined assignment, and any still-pending trade affecting those portions is cancelled. If current portion owners differ, an ordinary user is directed to an administrator or scheduler. Administrators and schedulers may post, split, recombine, open, refill, reassign, and directly swap any users' published assignments. Opening a shift preserves its historical trade details while cancelling pending activity. A direct time overlap or mixed-owner recombination produces a warning with an explicit proceed-anyway option instead of a hard prohibition, because the action may be one step in a multi-shift correction. Structurally invalid input, such as a split point outside the shift, remains blocked.

Every active clinician who is marked clinically active in a Domain retains the ordinary clinician workflows in that Domain: submit their own requests, post or offer their own shifts, propose and receive trades, split their own shifts, and request pickup of shifts that another clinician posted in `PICKUP` mode. Administrative and scheduler powers are additional capabilities; they never replace these end-user capabilities. A shift cannot be posted, offered, traded, or picked up once its exact start time has been reached. A pending trade is cancelled automatically when either involved shift reaches its start time. An accepted trade updates the authoritative published assignment, not merely the trade record.

An eligible clinician may directly pick up an otherwise unassigned open shift. The open-shift workflow uses the same **Pickup Shift** action and explicit **You will be responsible for this shift.** confirmation as a posted pickup, while enforcing Domain clinical activity, pickup permission, active Contract and Facility eligibility, overlap, authoritative publication, remaining capacity, and the exact shift-start-time boundary. Scheduler/administrator direct assignment remains a separate additional capability. Trade-only postings continue to require the requesting clinician to offer one of their eligible shifts.

When a clinician posts their own shift for pickup or trade, they may include an optional comment. The posting record retains that comment while the posting is active, and other authenticated schedule viewers can read it in the shift details.

The Trade Requests control counts pending activity. A newly received, unseen request adds the request-open accent border; opening the trade list marks it seen and removes the border without requiring the user to accept, decline, or otherwise resolve the request. Sending a request does not mark the sender's own control as unseen. The Schedule page supports Group Schedule, My Schedule, and Mine & Available Shifts views. Resolved trades return to the ordinary ownership-based schedule colors.

The desktop application uses the approved light visual language with light surfaces, readable contrast, and consistent action hierarchy. The desktop Schedule retains its full controls and six-color legend: My assigned shift blue (`#1f65a6`), Available for pickup green (`#34d399`), My posted shift amber (`#fbbf24`), Trade sent purple (`#c084fc`), Trade received pink (`#f472b6`), and Open shift red (`#f87171`). Current ownership has visual precedence: a shift assigned to the signed-in user remains blue even when it is posted or involved in a pending trade. The compact mobile two-dot calendar must not replace this desktop legend.

Facility identity is shown on detailed desktop and mobile Schedule rows by a narrow left-edge stripe using the Facility's configured color, plus a subtler one-pixel outline in the same hue on the top, right, and bottom. Ordinary rows use a neutral surface rather than a facility or lavender background tint. These facility accents do not replace ownership, availability, trade, or open-shift status styling.

When a request window is open, a compact Requests Open control appears in the Schedule view for a clinically active user in the applicable Domain and links directly to that block's request entry workflow.

Administrators and schedulers may attach one calendar comment to a date in the authoritative published schedule by selecting its date number. The single-line title appears below that date's shifts at no more than one shift-row height; its optional details appear on hover. All authenticated schedule viewers may read comments, while only administrators and schedulers may add, edit, or delete them. A comment may repeat every one to four weeks on its selected weekday or monthly on the selected weekday occurrence, ending on a date, after a number of occurrences, or never. Recurring series exist independently of schedule blocks but render only on dates covered by the current authoritative published schedule; they do not appear in BUILD or PREVIEW, and a never-ending series begins appearing automatically as later schedules are published. An occurrence can be edited or deleted for that date only, that date and future dates, or the entire series.

Overlapping planning blocks are allowed. Creating a block that overlaps already-published dates requires acknowledgement. Publishing over already-published dates also requires acknowledgement: the newer publication replaces the Live Schedule for overlapping dates, while the previous published block remains archived as historical schedule-of-record data.

### Schedule Block Users planning

The Schedule Blocks list uses one **Open** action for the Build Workspace; the former duplicative **Build Schedule** action is not part of the workflow. Authorized schedulers use the adjacent **Users** action for block-specific workforce planning. This workflow has two tabs and is writable only while the block is in `PRE_BUILD` or `BUILD`; `PREVIEW` and published/archived blocks are read-only.

**Block Hours** lists every active, clinically active, non-view-only user in the block's Domain on one compact row. Every schedulable user must have an active parent Contract; a missing Contract is a configuration error rather than a reason to invent optimizer defaults. Each row contains an editable hours target and minimum/maximum, an editable shifts target and minimum/maximum, and display-only actual hours and shifts from the most recent earlier published block in the same Region and Domain. The historical actuals come from that publication's virgin snapshot, not later Live Schedule changes.

Block Hours is the single authority for block-specific workload values. When aggregate workload feasibility is outside the available coverage range, the Schedule Build Workspace reports the approximate whole-hour change required per FTE and links directly to Block Hours for review; the individual recommendations are shown only on Block Hours. Atlas apportions the aggregate correction proportionally from each user's existing target and rounds every proposed target, minimum, and maximum to whole hours, so FTE and partial-block availability already represented in that target are not applied a second time. A proposed target change moves that user's minimum and maximum by the same whole-hour amount, preserving the existing Hours − / Hours + range width. The scheduler reviews the highlighted values and explicitly saves them; no separate Schedule Version workload modifier is layered behind Block Hours. Atlas preserves the pre-adjustment target and range as the carry-forward baseline, so normal Autofill uses the adjusted target within the current block while the next block returns to the original target. Later manual entry or Autofill may replace current-block values, and feasibility is then recalculated from those visible values.

The first block without prior block-planning values begins from the midpoint of the effective parent Contract range, rounded to a whole unit, while retaining the Contract range. Later blocks carry forward the prior saved target and minimum/maximum adjustments rather than the user's actual worked totals, preventing cumulative drift from intended workload. If block length changes, Atlas proportionally normalizes the carried target by calendar-month count, carries the adjustments, and visibly states the previous and current lengths. All carried values remain individually editable.

All users are initially selected for autofill. The scheduler may select or clear all users or independently select individual users, enter separate lower and upper adjustments for hours and shifts, and autofill hours, shifts, or both for only the selected users. Autofill sets minimum to target minus the lower adjustment and maximum to target plus the upper adjustment; it must reject infeasible negative ranges or minimums greater than maximums. Manually entering block values does not create or label a Temporary Partial Block Contract. Violations use the applicable minimum or maximum penalty inherited from the user's effective parent Contract.

**Partial Block Users** begins with the same eligible Domain-user list. Selecting a user opens a prominently labelled **Temporary Partial Block Contract** for that user and Schedule Block. Its default **Availability** tab shows the entire block in compact month calendars. A selected unavailable date is a hard prohibition against assigning that user any shift whose start date is that date. Requests retained on an unavailable date remain available for audit but are inactive for request quotas and scoring.

After **Save Availability and Review**, Atlas opens Summary and highlights every tab and rule it proportionally changed. For applicable week, month, and Schedule Block volume rules, Atlas prorates by eligible calendar days within each natural period, rounds minimums down and maximums up to whole units, converts the result to a block-scoped rule, and leaves penalty weights unchanged. Sequence, adjacency, rest, and circadian rules are not prorated. The scheduler may review and manually override any rule field before saving the temporary Contract. The partial-user badge links back to this Contract. Its effective block hours and shifts are the same underlying block-specific values shown on Block Hours, so edits from either location remain synchronized. Atlas preserves the user's normal full-block values while the temporary Contract is active, preventing partial-block reductions from carrying into later blocks; removing the temporary Contract restores those preserved Block Hours values and ordinary parent-Contract behavior.

Changing requests, an ordinary Contract, Block Hours, or a Temporary Partial Block Contract does not rewrite any completed Run. The next optimizer Run reads the then-current constraints. When the scheduler starts from a previous Run, Atlas copies its assignments, authoritatively rescores them under current constraints before optimization, and records that current score as the new Run's starting penalty. The historical source score is neither reused nor displayed; the Run displays only its current starting and final penalties.

### Backdate Shifts

Backdate Shifts is an administrator/scheduler workflow for targeted corrections or additions to the current and future portion of the authoritative published schedule. It is not a substitute for unpublishing and rebuilding a schedule when changes are broad.

- The workflow is available only to a user with both Build Workspace management and published-shift batch-update permission.
- It offers only Shift Templates that have actually been added, modified, disabled, or re-enabled since the applicable published schedule snapshot.
- The first affected date cannot be earlier than today. The optional end date cannot be later than the end of the currently published schedule; leaving it blank applies the change through that published horizon.
- Only occurrences inside the selected window change. The Shift Builder definition remains in its newly configured state regardless of the selected backdate window.
- The preview is a compact date-by-date list of removals, additions, and replacements, not a calendar.
- An existing assignment is retained when its shift is modified. A newly added shift begins open unless the scheduler approves a proposed assignment transfer.
- When a shift is removed and another is added on the same date, Atlas recommends the removed shift's assigned user as the replacement when that transfer is valid. The scheduler may approve each proposal, leave it open, accept all valid proposals, or leave all additions open.
- A transfer recommendation must still satisfy staffing, clinical activity, Facility eligibility, and overlap rules.
- Pending trades involving an affected assignment are cancelled and the sender is notified with the reason.
- The applied batch is recorded for audit.

If the scheduler needs substantial restructuring, the established workflow is to unpublish the schedule, return it to `BUILD`, and use the optimizer rather than forcing the change through Backdate Shifts.

## Schedule Build Workspace

### Settled purpose

The Schedule Build Workspace is the pre-publication workspace for a Schedule Block. It generates, displays, edits, scores, and optimizes the dated staffing requirements that later become the published schedule.

- A Schedule Version belongs to one Schedule Block and one Domain.
- The initial generated version has `BUILD` status.
- Generating creates one Schedule Shift Instance for each active Shift Template on each matching active day inside the Schedule Block.
- Each instance carries its date, source template, Facility, start/end datetimes, required staffing, and open assignment state.
- Overnight instances end on the following calendar day when their template end time is earlier than or equal to its start time.
- Generation safely reuses the existing BUILD version and does not duplicate a template/date instance.
- Generated instances begin open and unassigned.

Schedulers and Admins can manually assign active physicians to generated Shift Instances in a BUILD version. Assignments belong to instances, never to Shift Templates. An instance supports separate assignment records up to its required staffing; the same physician cannot be assigned to the same instance twice or to overlapping Shift Instances in the same Schedule Version. Staffing displays as assigned/required, and an instance remains open while its assigned count is below required staffing. Manual assignments persist with the BUILD version and may be removed there.

Manual assignment currently requires an active physician with an active Contract assignment in the Schedule Version's Domain, and that Contract must include the instance's Facility. The assignment picker displays only physicians who can actually be assigned; when none qualify, it shows an explicit empty state. Domain membership and permissions remain separate access concepts, and scheduling eligibility is not inferred from primary Facility or clinician type.

The workspace is not the Live Schedule, preview, or publication. Manual assignment prevents overlapping assignments as basic schedule validity; authoritative optimizer scoring separately evaluates requests, workload, nights, weekends, rest, penalties, and the configured Contract rules.

Shift Templates belong to a Domain and Facility. BUILD generation uses only active templates in the Schedule Version's Domain and active Facilities; templates from another Domain or Region must never leak into the workspace.

### Shift Builder changes during BUILD

Shift Builder records an event when a template is added, modified, disabled, re-enabled, or deleted while an affected unpublished Schedule Block is already in `BUILD`. This event-driven notification avoids rescanning every template whenever the Build Workspace opens.

The affected workspace cannot continue until an authorized scheduler either acknowledges the changes or cancels. Cancelling clearly explains that the scheduler must correct the Shift Builder discrepancy or acknowledge the changes before re-entering that workspace. A block created after the template change already begins from the current template catalog and does not receive a retroactive acknowledgement requirement.

Runs created before an acknowledged template change remain available as optimizer starting points, but they are visibly stale and cannot be moved to preview or published. A new optimizer Run may use that schedule as a seed, incorporates the current template snapshot, explicitly represents newly valid open shifts, and fills them when eligible placement exists.

### Optimizer V2 and Run history

Optimizer behavior changes follow
[`optimizer-change-control.md`](optimizer-change-control.md). New V2 runs pin
their internal optimizer revision, scoring revision, deployment release, and
canonical launch-configuration digest when they are queued or started. A
worker must fail closed when its build does not support the pinned revision;
it must never substitute the deployment default for an already pinned run.

Optimizer V2 is the production optimizer. V1 remains an internal regression/compatibility reference and cannot be selected to launch a new production Run.

An optimizer Run executes in the background inside the selected `BUILD` Schedule Version. The scheduler may leave the page or log out while it runs. The default maximum runtime is 120 minutes, the accepted range is 1 through 240 whole minutes, and the browser remembers the most recently selected valid value. The selected focus and initiating user are stored with the Run and shown in Run history.

The supported starting sources are:

- **Fresh Fill**: build from the current valid staffing requirements with no prior schedule assignments.

Fresh Fill construction ends when Atlas has produced a complete, hard-legal schedule. It must then record that schedule as the Run's initial result and hand it to the primary optimization engine without waiting for every `FIXED` request to be satisfied. `FIXED` requests retain their full high-priority scoring and optimization treatment after the handoff, but—as documented below—are not hard scheduling constraints and must not prevent the first complete schedule from becoming available for optimization. Publication remains the separate explicit Schedule Block workflow described above.
- **Previous Optimizer Run**: copy a selected completed Run, snapshot the rules, requests, users, Facilities, templates, and other inputs that are valid now, authoritatively rescore the copied schedule, and use that current score as the new Run's starting point. The source Run is never modified.
- **Current Schedule**: start from the current authoritative published schedule, including approved trades, posts, pickups, and scheduler edits that occurred after publication.
- **Original Published Schedule**: start from the virgin schedule captured at the most recent publication, before subsequent Live Schedule changes.

Only sources that exist for the block are offered. A BUILD workspace with no publication must not label its active BUILD Run as a Current Schedule. Previous completed Runs remain selectable until the applicable retention policy removes them.

Each Run is independent and receives the next consecutive Run number. Selecting a completed Run in the Viewing Optimizer Run control makes it the active BUILD Run; there is no separate user-facing Activate action. The selected Run can be moved to preview with the explicit **Change status to preview** action only when it reflects the current acknowledged Shift Builder configuration.

If a scheduler manually edits an existing Run, the save choices appear only after an actual manual change:

- **Save as New** preserves the original Run and records the edited schedule as the next Run with a recalculated score.
- **Save Current Run and Recalculate Score** updates that Run in place and retains its number.

Manual assignments record their source as `MANUAL`; optimizer assignments record `OPTIMIZER`. An explicitly locked manual assignment remains fixed during optimization, while an unlocked manual assignment may seed and then move during the search. An instance explicitly locked open remains unfilled; any other valid open instance may be filled.

An authorized scheduler may recalculate the stored score, workload details, and violations for any completed Run in a BUILD Schedule Version without activating or otherwise changing that Run's saved assignments. Recalculating an inactive Run updates that Run only; the Schedule Version's active summary remains tied to the active Run. The Build Workspace identifies the exact Run targeted by the recalculation action.

Selecting a completed Run makes its schedule available without waiting for the Run-specific workload-feasibility analysis to finish. That read-only feasibility analysis refreshes immediately afterward and appears only if the same Run remains selected.

The Build Workspace keeps its optimizer controls visually compact. The Run activation selector is explicitly labeled **Select to activate**, the current Run is marked **Active**, and the expanded Run-management rows provide violation and deletion actions without a duplicate Select button. Development-only bulk assignment-clearing controls are not part of this workspace.

The optimizer violation report presents selected score filters with high-contrast text and includes compact schedule-wide High, Medium, and Low request made/denied totals directly in the existing score-card grid. Fixed requests remain governed separately and are not included in those three priority totals.

Before every Run, Atlas snapshots the current authoritative inputs and reconciles the chosen source to them. The source schedule is authoritatively rescored before search. Missing current shift instances are represented explicitly, and newly valid open shifts receive a proven placement pass before V2 improvement begins. The vectorized search must periodically reconcile against the authoritative scorer, and a candidate may never be retained as an improvement when the authoritative score does not improve under the required ordering.

Every completed V2 Run retains bounded production telemetry for optimizer health, comparison, and future capacity planning. Telemetry is accumulated in memory only at vectorized-neighborhood and existing authoritative-checkpoint boundaries; candidate evaluation performs no database, network, or per-candidate telemetry writes. The retained summary includes time to the first and best verified checkpoints, aggregate phase timings, cache use, stop reason, and attempts, acceptances, and authoritative penalty movement by selection mode, accepted operation, and structural rule family. Full neighborhood payloads and transition replay details remain temporary failure diagnostics rather than permanent completed-Run history. Production telemetry must remain below 1% overhead in representative benchmarks.

The two focus modes are:

- `STANDARD`, the default, which treats Facility and broad time-of-day proportionality as a score-neutral tie-breaker.
- `DISTRIBUTION`, displayed as **Facility/Shift distribution focus**, which searches the proportional-distribution neighborhood first and permits score-neutral distribution progress to renew that focus.

Both modes preserve hard validity and authoritative requests, always prefer a lower official penalty, and use proportionality only between schedules with the same official penalty. Proportionality can never worsen the official score. Facility/time distribution remains informational and is not added to the total penalty.

The Violation Report's Details tab explains the per-user contributing violations. Each user block identifies the user's parent Contract once beside the user's name; individual violation rows do not repeat the Contract and instead identify their evaluation Period as Week, Month, or Block. The Summary tab shows only rule penalties greater than zero that contribute to the authoritative total; informational totals such as nights assigned or maximum nights are not shown as violations unless they generate a penalty. Facility and time distribution remain separate lower-is-better comparison percentages and do not change assignments, rules, Run selection, preview, publication, or the Live Schedule.

Publishing records the exact active Run and a virgin Original Published Schedule snapshot. Republish resets the Run-retention clock. Unused Runs in an archived block may be purged 30 days after the most recent publication, while the published Run, current controlled Run, and running Runs are preserved. A Run protected by active state, current viewing, publication, or another active Run's source relationship cannot be deleted manually.

The Git tag `v1.0.0` remains the historical Atlas V1 baseline. V2 may replace optimizer internals, but it must preserve authoritative Contract/request scoring, independent Run history, published-schedule immutability, and the rule that proportionality never worsens the official penalty.

## Request Builder

### Settled purpose

Request Builder records preferences and administrative instructions within a Schedule Block. Requests are date-specific and physician-specific.

Supported request types are:

- `DAY_OFF`
- `SHIFT_OFF`
- `DAY_ON`
- `SHIFT_ON`

Supported weights are:

- `LOW`
- `MEDIUM`
- `HIGH`
- `FIXED`

Day-level requests cannot include Shift Templates. `SHIFT_OFF` requires one or more templates. `SHIFT_ON` requires exactly one template. Any selected template must be active and available on that date's weekday.

Choosing `None` in the single-request editor removes the request for that physician, date, and scope.

### User and administrative requests

Requests have separate `USER` and `ADMIN` scopes. Atlas permits one request of each scope for the same Schedule Block, physician, and date; the two records may coexist.

- Ordinary authenticated clinician users can view and modify only their own `USER` requests.
- Domain permissions determine who may manage other users' requests. Creating or editing `ADMIN` requests requires administrative request permission, and using `FIXED` requires the separate fixed-request permission.
- `ADMIN` requests are never exposed to a user who lacks administrative request visibility.
- Users without a linked Physician profile receive a read-only calendar context rather than an invented clinician identity.

Single and bulk request writes are allowed only while the block is `PRE_BUILD` or `BUILD`. `PREVIEW` and `ARCHIVE` requests are read-only.

An ordinary clinician may select multiple dates and save the same permitted `USER` request across those dates in one atomic operation. The batch may target only that clinician, remains subject to the ordinary request window, applicable Contract request types, Shift Template weekday eligibility, and all priority and weekend limits, and saves nothing when any part of the batch is invalid. Request counters show the projected result while dates are selected and saving is blocked when the projected batch would exceed a configured limit. Choosing `None` remains a single-date removal workflow.

Schedulers can bulk-apply a request to multiple physicians and dates. The UI includes date helpers for every occurrence of a weekday, remaining occurrences of a weekday, and the nth weekday of each month. These helpers select explicit dates; they do not create a permanently recurring request rule.

### Current enforcement

The request open and close timestamps authorize ordinary user writes. An ordinary user may submit requests only while the request window is open and the Schedule Block is in a writable lifecycle state. An authorized request manager may make administrative changes outside the ordinary user window while the block remains writable. `PREVIEW` and `ARCHIVE` remain read-only.

When a physician has an applicable active Contract assignment in the selected Domain, Request Builder uses that Contract to:

- limit an ordinary user's available request types;
- limit Shift Templates to the Contract's eligible Facilities;
- enforce High, Medium, Low, and Weekend limits for user-scope requests;
- treat Low as unlimited when configured;
- calculate and display request counters.

Authorized request managers receive the request types allowed by their Domain permissions. Admin-scope requests coexist with and remain hidden from ordinary users. Contract limits apply to user-scope requests; admin-scope requests do not consume them. The request calendar cohorts user requests above administrative requests and provides separate priority totals plus a weekend total.

Weekend counting follows Shift Template weekend designations. Day Off counts once when at least one eligible, active template designates that date's weekday as a weekend day. Shift Off counts once when at least one selected template carries that designation. Multiple selected shifts on the same request still count as one request.

The Weekend request maximum is a quota overlay, not a separate request priority or penalty. A user-scope request that qualifies as a weekend request consumes one Weekend request and one request at its selected High, Medium, or Low priority. If that request is violated, its penalty comes only from the selected priority's configured weight; Weekend has no independent penalty weight.

If the clinician has no active Contract in the selected Domain, Request Builder does not infer policy from another Domain. An ordinary user cannot create a new request until the applicable Contract is configured; they may still select `None` to remove an existing user request. An authorized request manager retains the options granted by Domain permissions.

`FIXED` is an admin-only high-penalty instruction, not a hard scheduling constraint. It does not appear as an ordinary user's request option.

## Organizations, Regions, and Domains

An Organization is Atlas's tenant-level container. An Organization has one or more named Regions, and every Region has one or more named Domains. Region names are unique within their Organization, and Domain names are unique within their Region rather than globally. Org Admins may manage Regions and Domains. Used Regions or Domains are deactivated instead of deleted. An Organization must retain at least one active Region and one active Domain. For a single-region organization, the Organization and Region may intentionally use the same name so the hierarchy is available without inventing a meaningless label.

Users belong to an Organization through a role-free Organization Membership and may belong to any number of its Domains across any Region. Roles exist only on Domain Memberships: Org Admin, Medical Director, Admin, Staff Physician, APP, Scheduler, or View Only. A user may hold a different role in every Domain. An Org Admin role in any Domain grants management authority for the containing Organization. Domain Membership records are the authoritative foundation for domain-specific access; scheduling screens must not infer Domain membership from clinician type, primary Facility, or Contract alone.

Domain permissions, rather than legacy global groups or profile labels, authorize Domain-specific work. An active membership marked clinically active always carries the base clinician workflow permissions for that Domain: submit one's own requests, post or offer one's own shifts, propose trades, split one's own shifts, request pickup of eligible posted shifts, directly pick up eligible open shifts, and receive the corresponding trade workflows. Scheduler, Admin, Medical Director, and Org Admin capabilities are additive. Marking a membership clinically inactive removes the clinical workflow basis without erasing historical schedule data.

The pre-Organization Atlas data is preserved inside the initial editable `Lowcountry Emergency Physicians` Organization and its initial `Lowcountry Emergency Physicians` Region. Existing Domains, users, Contracts, schedules, and optimizer history retain their identifiers and relationships. A commercial deployment may have its top-level Organization created by an Atlas administrator.

## Contracts

### Settled purpose

A Contract is a named, domain-specific scheduling rule set. Contract names are unique within a Domain, not globally. Contract lists are scoped by Region and then Domain so identical Domain names in separate Regions remain unambiguous.

Each Contract has:

- one Domain;
- active/inactive status;
- one or more selected Facilities in the current UI;
- optional per-Facility minimum and maximum scheduling targets;
- configurable workload, shift, night, weekend, and request settings;
- zero or more assigned physicians.

The supported rule configuration includes:

- workload minimums and maximums by week, month, or Schedule Block, measured in hours or shifts;
- per-Facility minimums and maximums by week, month, or Schedule Block, measured in hours or shifts, with separate minimum and maximum penalty weights;
- separate minimum and maximum penalty weights and optional spreading of violations. When Spread violations is enabled, Atlas uses each user's effective parent-Contract threshold and prefers that every applicable user receive a first violation before any user receives a second, then a second before any user receives a third. Spread is a fairness tie-breaker and does not change the configured penalty amount or allow a worse official penalty score;
- minimum time off, circadian handling, and minimum/maximum days-in-row settings. When Circadian Rhythm is enabled, consecutive workdays may rotate backward by no more than one hour: the circadian floor is the prior day's shift start plus 23 hours, while the separate rest floor is the prior shift end plus the user's required minimum time off. A start before either applicable floor triggers that rule's penalty, and a full calendar day without an assignment resets the circadian comparison. Circadian violations are not spread across users. Minimum days in a row is blank or at least `2`: blank permits solitary shifts, while `2` or more penalizes each completed work streak shorter than that minimum once;
- **SAME SHIFT IN A ROW**, which has only a maximum streak and its penalty; there is no minimum same-shift streak because Atlas never rewards or requires assigning a user repeatedly to one exact Shift Template;
- named Shift Template groups with their own period rules and an optional per-Contract **Same shifts in a row** maximum plus penalty. This Shift Group control counts adjacent calendar dates on which the user is assigned any selected template: a maximum of `1` discourages selected-group assignments on adjacent days. It is distinct from the workload-level **SAME SHIFT IN A ROW** rule, which evaluates consecutive occurrences of one exact Shift Template;
- night volume, consecutive-night, and recovery settings;
- weekend volume, consecutive-weekend, consecutive-weekend-shift, and Friday-night-before-weekend-off settings;
- allowed request types, High/Medium/Low request maximums and weights, the Fixed request weight, and a Weekend request maximum without a separate weekend penalty;

These are configurable mechanisms. Atlas must not hard-code a universal workload, night, weekend, or request policy.

Penalty counting follows the rule's meaning. Volume and quota rules charge once for each unit outside the effective minimum or maximum. Adjacent-date streak rules (days in a row, same shift in a row, consecutive night shifts, and consecutive weekend shifts) charge once per violating streak regardless of how far that streak misses its limit. Recovery and Friday-night-before-weekend-off rules charge once per violating event. Consecutive-weekend minimums charge once per completed short weekend streak, while consecutive-weekend maximums charge once for every additional worked weekend beyond the maximum in the same streak (for example, four weekends against a maximum of three is one violation; five is two).

Weekend rules and weekend-request counting use only the explicit `weekend_days` designation on each Shift Template and the calendar weekday on which that Shift begins. Atlas does not infer that every shift on Friday, Saturday, Sunday, or Monday is a weekend shift. A designated shift remains a weekend shift for its entire duration even when it ends on another day; an undesignated Monday-midnight shift is not treated as Sunday merely because users may describe it colloquially that way.

### Assignment and lifecycle rules

A physician can have at most one default Contract per Domain. Assigning that physician to another Contract in the same Domain replaces the previous default assignment.

A Contract assignment may carry sparse user-specific rule overrides without creating a new Contract. The physician remains assigned to and listed under the parent Contract. Every rule that is not explicitly overridden continues to inherit the current parent value; removing an override restores inheritance. Individual overrides may change configurable rule values, including minimums, maximums, request limits, rule switches, and penalty weights, but do not change the parent Contract's identity, Facilities, structural rule definitions, or other users. Authoritative request policy, feasibility analysis, optimizer scoring, input snapshots, and violation reporting use the effective Contract formed by merging the parent settings with that physician's overrides. Changing an effective override marks applicable unpublished schedule scores stale and records who changed it and when.

The Contracts interface identifies assigned users whose effective settings differ from the parent and opens their effective Contract with inherited and modified values clearly distinguished. A separate **User Overrides** tab beside **Shared Rules** lists those users alphabetically by last name, shows each parent Contract, and opens the same override editor. Users with no effective differences do not appear in that list. User-override administration is a desktop Contract-management workflow; ordinary mobile scheduling actions still consume the same effective rules through the shared APIs.

Inactive Contracts cannot receive physician assignments unless reactivated. Contracts are deactivated rather than deleted. Copying a Contract creates an active Contract because the copy is intended for immediate use or editing; physician assignments and schedule history are not copied. A same-Region copy may reuse its Facilities and applicable Domain resources. A cross-Region copy carries the general workload, night, weekend, request, and other non-Facility-specific settings, but begins with no Facilities, facility-specific Shift rules, Shared Rules, or users. Opening that Contract takes the scheduler directly to Facility setup and requires at least one destination Facility before the Contract can be saved or assigned to users.

Domain-specific user membership is modeled separately from Contract assignment. A user may belong to a Domain without a Contract, but a Contract assignment is valid only inside that Contract's Domain.

### Current enforcement

Contract rules are persisted as structured JSON configuration. Request Builder consumes the applicable Contract's Facility eligibility, allowed request types, configurable request limits, request weights, and unlimited-Low setting. Optimizer V2 and its authoritative scorer evaluate the configured coverage, workload, shift-group, night, weekend, request, overlap, rest, eligibility, and distribution rules that apply to the Run. Violation reporting must reconcile to the same authoritative score used to accept or reject schedules.

Facility selection remains the hard eligibility boundary for a Contract. A selected Facility may additionally carry one or more soft minimum/maximum target rules measured in hours or shifts for a week, month, or Schedule Block. These targets have separate minimum and maximum penalty weights and are evaluated by the authoritative optimizer and violation report. A user-specific Contract override may change a Facility rule's minimum, maximum, or penalty weights while continuing to inherit Facility eligibility, period, units, and every unmodified value from the parent Contract.

Atlas enforces at most one default Contract per physician per Domain. Policy from another Domain must never be silently merged into the selected Domain. When no active applicable Contract exists, the workflow must stop or limit itself to explicitly safe administrative, removal, or read-only behavior.

## Facilities

A Facility represents a medical location where shifts are scheduled. Its settled fields are:

- one Region;
- full name;
- required short name;
- timezone, defaulting to `UTC`;
- display color, defaulting to `#2563eb`;
- active/disabled status.

The short name is the compact scheduling label and is used to generate Shift Template names. Timezone and color are presentation/scheduling metadata used in the scheduler experience.

Facilities are disabled and re-enabled rather than deleted. Creation and selection workflows generally offer only active Facilities; disabling preserves existing relationships and history. Facility lists and ordering are scoped to their selected Region.

## Shift Templates

A Shift Template belongs to one Domain and one Facility. Its Facility must belong to the same Region as its Domain. Shift Builder is filtered by Region and Domain, and the selected context is shown when viewing, creating, editing, or copying a template. Existing templates retain their Region and Domain; copying is the supported way to create an equivalent template in another Region or Domain.

BUILD generation uses only active Shift Templates in the Schedule Version's Domain and active Facilities. Templates in any other Domain or Region must never leak into that schedule.

Shared Rules do not follow a Contract across Regions. A scheduler may copy a Shared Rule separately into another Domain as an active setup draft. The draft preserves the source rule's name, period, units, source shift labels, and contract-specific limits as reference only; it has no destination Shift Templates or Contract links until the scheduler explicitly selects them. A Shared Rule may initially apply to one Contract and can be added to more Contracts later. Its Domain is fixed after creation, and its Contract and Shift choices are limited to that Domain. Completing and saving the copied rule replaces the source reference with valid destination relationships.

Each Shared Rule–Contract relationship is the single authoritative record for that Contract's enabled state, minimum, maximum, minimum and maximum penalties, spreading behavior, and optional **Same shifts in a row** maximum and penalty. The Shared Rules editor and the individual Contract editor must read and update that same relationship so changes are immediately visible in both directions. Disabling a Shared Rule from within one Contract retains its settings and affects only that Contract. Global deactivation or deletion from the Shared Rules tab affects every associated Contract and requires an explicit warning that lists all affected Contracts before the action may proceed.

## Physicians

A Physician profile is linked one-to-one with an authentication user. Its settled fields are:

- first and last name;
- unique email, also used as the username;
- optional phone number;
- transitional profile role label retained for backward compatibility;
- optional display name;
- optional primary Facility;
- clinician type: Physician, PA, or NP;
- FTE, defaulting to `1.00`;
- active/disabled status.

Display name is preferred in scheduling interfaces, with the user's full name and username as fallbacks.

The primary Facility is descriptive/default affiliation; it does not restrict the physician to that Facility. Contract Facilities and actual Shift assignments are separate concepts.

Physicians are disabled rather than deleted. Domain access and domain-specific roles are stored through Domain Memberships. Atlas does not automatically assign a Contract based on FTE, clinician type, or primary Facility.

## Account Access and User Administration

Successful login, including completion of a required first-login password change, opens the Schedule calendar rather than an administrative directory. Navigation and actions are then limited by the user's Domain permissions and Scheduler/User view.

Creating a user or performing an Org Admin password reset issues a unique eight-character temporary password containing exactly one special character and at least one uppercase letter, lowercase letter, and digit. The temporary credential is shown once, invalidates the user's existing sessions when it replaces a password, and must be changed before the rest of Atlas can be used. An administrator must never retrieve, display, or reset a user's existing password merely to troubleshoot access.

An Org Admin may edit their own full managed profile, including Domain memberships, roles, and clinical activity, just as they may for another managed user. A non-managing user may update only their own permitted contact fields. Active status is changed only through the protected deactivation/reactivation workflow so Atlas cannot accidentally remove the last required administrator or discard historical identity.

Published Shift Details actions are permission-driven rather than view-toggle-driven. A clinically active scheduler sees ordinary user actions for their own shift first, followed in the same window by the scheduler actions granted in that Domain. Personal trade proposals use the same conflict-free candidate filtering and submission validation regardless of the user's administrative permissions. Candidate eligibility is evaluated as the complete reciprocal exchange: the two selected assignments are removed from each user's conflict calculation before both resulting assignments are checked. This permits a valid same-date exchange while excluding unrelated shifts from a user who would remain scheduled during the offered shift. Posting and split/recombine are available for one's own shift through the corresponding personal permissions and for any assigned shift through the corresponding scheduler permissions. Changing or swapping scheduled users, adjusting actual shift times, opening an assigned shift, and assigning an open shift remain scheduler-only actions and appear below ordinary user actions. Scheduler swaps may surface conflict-bearing candidates only through their distinct permission-gated workflow and retain explicit conflict confirmation.

Production sessions use a rolling seven-day default lifetime, refreshed by authenticated activity (including reopening Atlas), unless deployment configuration explicitly overrides that duration. Closing the browser does not end the session by itself. Explicit sign-out invalidates only the current browser/device session and is available from both desktop and mobile navigation; a separate authenticated session on another device remains signed in. A password reset continues to invalidate all sessions for the affected account.

## Mobile Direction

Atlas's approved mobile direction is a responsive presentation of the existing application, not a separate native application. At viewport widths up to 820 pixels, or when Atlas is running in installed standalone display mode, authenticated users enter the responsive mobile surface automatically. The explicit `/mobile-preview` route remains available for direct access and testing. Larger desktop viewports retain the full desktop application. The mobile surface reads the authoritative published schedule and accessible Domain scope through the existing APIs; it does not define a separate mobile data model or separate scheduling policy.

The approved mobile presentation is light and schedule-first. It uses compact List/Month and Group/My Schedule controls, a weekly navigator in List view, persistent device-local view selection, bottom navigation for user-facing destinations, and Region/Domain scope inside More. Administrative Schedule Block and build navigation remain desktop workflows and are not reproduced in the compact user-facing mobile navigation.

Month and List calendars keep today independently outlined when it is also the selected date, visibly mute past dates without making them difficult to read, and preserve the selected-date treatment as a separate state. Within each mobile date, Physician shifts are listed before APP shifts; the normal configured ordering remains intact inside each Domain group.

The mobile Month calendar and weekly navigator deliberately use a simplified two-dot status language:

- blue (`#1f65a6`) means the signed-in user is assigned to at least one shift on that date and remains blue regardless of posting or pending-trade state;
- amber (`#fbbf24`) means at least one other shift is open, posted for pickup, or posted for trade;
- a date shows no more than one blue dot and one amber dot;
- ordinary group shifts do not create a dot.

This mobile summary language does not replace the detailed shift rows or the desktop six-color legend. Mobile actions use the same authoritative permissions, eligibility rules, confirmation requirements, and workflows as desktop. The production-connected user workflows include direct pickup of an eligible open shift, pickup requests for another clinician's posted shift, creation/update/removal of the signed-in clinician's own pickup or trade-only posting, trade proposals and offers, the Trade Requests center and its permitted actions, and split/recombine. Posting supports the same optional comment and exact shift-start boundary.

The shift-details workflow is permission-driven rather than view-toggle-driven. A clinically active scheduler sees ordinary user actions for their own shift first, followed in the same window by the scheduler actions granted in that Domain; switching between User and Scheduler presentation is not required. Personal trade proposals always use the same conflict-free candidate filtering and submission validation regardless of the user's administrative permissions. Candidate eligibility is evaluated as the complete reciprocal exchange: the two selected assignments are removed from each user's conflict calculation before both resulting assignments are checked. This permits a valid same-date exchange while excluding unrelated shifts from a user who would remain scheduled during the offered shift. Posting and split/recombine are available for one's own shift through the corresponding personal permissions and for any assigned shift through the corresponding scheduler permissions. Changing or swapping scheduled users, adjusting actual shift times, opening an assigned shift, and assigning an open shift remain scheduler-only actions and appear below ordinary user actions. Scheduler swaps may surface conflict-bearing candidates only through their distinct permission-gated workflow and retain explicit conflict confirmation. Mobile does not broaden scheduler authority. Mobile sign-out invalidates the authenticated session. **My Stats** provides a one-month-at-a-time month/year view of the signed-in clinician's published shift list, shift count, total hours, and night hours. Custom date ranges and Group Stats remain desktop-only. The mobile **Users** destination is a production-connected, Domain-scoped directory with search and permission-filtered user contact/profile details; it does not expose administrative user editing. The device remembers the last successfully loaded Users Domain and restores it after an app restart; if that Domain is no longer permitted, Atlas discards the saved selection and falls back to the first currently permitted Domain.

Atlas is delivered as an installable Progressive Web App, not a separate native application. Its production manifest supplies Atlas home-screen icons and standalone launch behavior. Where the browser exposes a native install prompt, the mobile More menu offers **Install Atlas**; iPhone and iPad users receive Safari Share → Add to Home Screen guidance. Installation controls are hidden when Atlas is already running as an installed app.

The mobile surface never substitutes demonstration schedule data when authoritative production data cannot be loaded. It presents an unavailable/error state with an explicit retry. While the device is offline, existing visible information is read-only and mutation, trade, and schedule actions are blocked. API responses are not treated as cached authoritative data. When an updated application build is available, Atlas presents an update notice and applies the waiting service worker only after the user explicitly chooses to update and reload.

Other destinations or controls that have not been connected to an authoritative API remain preview-only. A visible mobile control is not evidence that its persistence or backend workflow is complete.

## Production Resilience and Disaster Recovery (Future Implementation)

Before Atlas becomes a production dependency at scale, it must have a tested recovery path that does not depend on the continued availability or integrity of the active Railway project or Railway account. This is an approved future production requirement, not a claim that the complete recovery system is currently implemented. Provider-specific commands, responsible personnel, credentials, and incident procedures belong in a separately maintained disaster-recovery runbook.

Atlas must maintain an independently stored full-system recovery package outside Railway. That package must be sufficient to recreate the running product in a clean environment and must include the versioned source repository, exact known-good release identity and build inputs, dependency locks, database migrations, infrastructure and service configuration, an inventory of required secrets without exposed secret values, encrypted verified database backups, and recovery instructions. Railway is replaceable infrastructure and must not be the only location of any irreplaceable product or recovery material.

Recovery material is immutable input and is never made public or executed in place. A golden recovery package is used to create a new isolated recovery candidate; the candidate is verified before traffic is redirected to it. The failed or potentially compromised environment is preserved separately when needed for investigation. A warm standby may shorten recovery time, but it does not replace historical immutable backups because corruption or malicious changes may replicate into the standby.

Maintaining a current copy of the authoritative published schedule is the highest continuity priority. Every committed mutation of a published schedule, including publication, trade completion, pickup, posting or opening, reassignment, split or recombination, actual-time adjustment, Backdate Shift change, and an operational calendar-comment change, must atomically record a durable continuity event. The schedule mutation and its pending continuity event must commit together, such as through a transactional outbox, so a successful schedule change cannot silently omit its recovery record. A worker copies those events to encrypted, versioned, append-only storage outside Railway.

At scale, Atlas does not create a complete database backup after every change. It maintains a near-continuous change journal, consolidates recent changes into a complete verified read-only published-schedule snapshot within an initial target of one to five minutes, and periodically reconciles that snapshot against the live authoritative schedule. Complete portable database backups continue on a slower schedule. The latest verified schedule snapshot for each Domain is retained at all times and includes its Domain, coverage period, facilities, shifts, current assignments, creation time, sequence or version identity, and an integrity checksum or signature.

During a catastrophic outage Atlas may direct users to an independently hosted emergency schedule viewer created from a verified recovery candidate. That viewer is read-only and clearly identifies the exact verification time and the possibility of later unrecorded changes. It must not permit trades, pickups, postings, schedule edits, password changes, or administrative mutations. The normal mobile offline state remains non-authoritative and read-only; an emergency continuity snapshot must not silently present itself as live production data.

The initial retention policy for future implementation is:

- individual published-schedule continuity events: 14 to 30 days;
- consolidated near-real-time schedule snapshots: 7 days;
- daily full database backups: 35 days;
- weekly database backups: 12 weeks;
- monthly database backups: 12 months;
- latest verified published-schedule snapshot for every Domain: always retained while the Domain is operational.

Continuity events or backups may be compacted or deleted only after the succeeding backup has completed, passed its integrity verification, been copied outside Railway, passed the required restore validation, and has overlapping recovery coverage. A daily backup does not authorize immediate deletion of the preceding 24 hours of change history. Retention and deletion controls must prevent an ordinary Atlas or compromised production administrator from prematurely erasing the protected recovery history.

Production readiness requires documented recovery-point and recovery-time targets, point-in-time database recovery, health-checked rollback, least-privilege administrative access, independent control of DNS, external monitoring, periodic isolated restore tests, and rehearsed crash and cyber-incident procedures. A cyber recovery restores into a clean environment from a known-clean point, rotates credentials and sessions, validates the authoritative schedule, and removes the access path used in the incident before normal writes resume.
## What Atlas Explicitly Should Not Build

Atlas should not build:

- organization-specific scheduling policy as hard-coded product behavior;
- physician assignment, dated scheduling, or My Schedule integration inside Shift Builder;
- a second editable name for Shift Templates or Schedule Blocks when the established name is generated;
- a single `weekend_shift` flag in place of explicit Friday/Saturday/Sunday/Monday designations;
- hidden lifecycle edits that bypass Schedule Block transitions;
- destructive deletion as the normal lifecycle for Facilities, Physicians, Shift Templates, Contracts, or published Schedule Blocks;
- silent replacement of a published Live Schedule without explicit overlap acknowledgement;
- recurring request records when the current pattern helper is only a way to select explicit dates;
- management roles that remove the same user's clinically active end-user workflows;
- treating the active BUILD Run as the Current Schedule when no published schedule exists;
- allowing a stale Run to be previewed or published after an unacknowledged Shift Builder change;
- a separate native mobile application before the existing application has been made responsive and mobile-friendly.

Atlas does not need a user-facing Schedule Version tree or preserved Build-number branching for the current product workflow. Independent Runs, the current published schedule, and the virgin Original Published Schedule provide the required sources. Report builder/export remains unsettled future work. The approved responsive mobile surface is production-connected for the named schedule, trade, statistics, directory, and session workflows; completing the remaining responsive destinations and authoritative workflows remains future engineering work.

## Remaining Future Work and Guardrails

- Every scheduling interface must continue to apply the selected Domain and its Domain Membership permissions consistently; this is a correctness invariant, not unfinished role functionality. Legacy profile labels and global groups are not substitutes.
- Request Builder and scheduling policy require an active applicable Contract in the selected Domain. A missing Contract is a configuration state to communicate clearly, not permission to borrow policy from another Domain.
- Completing production integration for the remaining responsive/mobile destinations and workflows remains future work. A separate native application is not the first mobile direction.
- General report builder/export behavior remains unsettled future work.

Future work requires explicit product decisions. A missing configuration is not permission to infer policy.

## Local Development

### Start Atlas locally

From the project root in PowerShell, run:

```powershell
.\start-atlas.ps1
```

This starts the Docker services, including the supervised Vite frontend development server, in the background. Open Atlas at [http://localhost:5173](http://localhost:5173). The frontend uses the same `restart: unless-stopped` lifecycle as the other local application services, so closing the PowerShell terminal does not stop it. Frontend source and configuration files are mounted into the development container for Vite hot reloads.

The equivalent npm convenience command is:

```powershell
npm run dev
```

### Run scheduling tests

With the Docker services running, run this from the project root:

```powershell
.\test-scheduling.ps1
```

The equivalent npm convenience command is:

```powershell
npm run test:scheduling
```

Both test commands run `docker compose exec backend python manage.py test apps.scheduling -v 2`.
