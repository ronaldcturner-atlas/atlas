# Atlas Product Source of Truth

## Purpose

This document records product decisions that are already established by the current Atlas codebase and engineering documentation. It is a guardrail for future implementation: preserve these concepts and boundaries unless the product owner explicitly changes them.

This is a product-behavior summary, not a claim that every roadmap phase is complete. Where roadmap language and the running code differ, the code describes current behavior while the roadmap describes sequencing and future scope.

## Product Principles

1. Atlas is a physician scheduling platform.
2. Atlas provides configurable mechanisms, not one organization's scheduling policies.
3. Prefer simple, explicit workflows over hidden automation.
4. A feature should solve a real scheduling problem before it is added.
5. Product decisions precede implementation decisions.
6. Optimization is future workflow behavior; each optimization run must be treated as a new search, not as an invisible mutation of an existing result.

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

- one Facility;
- start and end times;
- one or more active days selected from Sunday through Saturday;
- zero or more weekend designation days, limited to Friday, Saturday, and Sunday;
- a `night_shift` flag;
- a required staffing count, with a minimum and default of 1;
- an active/disabled state.

The template name is system-generated from the Facility short name and time range, for example `Berkeley 7a-4p`. Users do not supply an independent template name.

A weekend designation is not the same as an active day. A day may be designated as a weekend day only when it is also active for that template. The old single `weekend_shift` boolean has been explicitly replaced by the `weekend_days` list.

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
- Only `PREVIEW` blocks can be published.
- Publishing stamps `published_at` and moves the block to `ARCHIVE`.
- Archived blocks are read-only and cannot return to preview.

Overlapping planning blocks are allowed. Creating a block that overlaps already-published dates requires acknowledgement. Publishing over already-published dates also requires acknowledgement: the newer publication replaces the Live Schedule for overlapping dates, while the previous published block remains archived as historical schedule-of-record data.

## Schedule Build Workspace

### Settled purpose

The Schedule Build Workspace is the pre-optimizer workspace for a Schedule Block. It generates and displays the dated, unassigned staffing requirements that later scheduling work will fill.

- A Schedule Version belongs to one Schedule Block and one Domain.
- The initial generated version has `BUILD` status.
- Generating creates one Schedule Shift Instance for each active Shift Template on each matching active day inside the Schedule Block.
- Each instance carries its date, source template, Facility, start/end datetimes, required staffing, and open assignment state.
- Overnight instances end on the following calendar day when their template end time is earlier than or equal to its start time.
- Generation safely reuses the existing BUILD version and does not duplicate a template/date instance.
- Generated instances begin open and unassigned.

The workspace is not the Live Schedule, preview, publication, optimization, or automatic assignment. It does not evaluate Contracts, penalties, or physician eligibility.

Shift Templates do not currently have a Domain relationship. The Schedule Version is Domain-scoped, while V1 generation reads the existing global active Shift Template catalog. Atlas must not infer a hidden Template-to-Domain mapping through Contracts or Facilities.

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

- Ordinary authenticated physician users can view and modify only their own `USER` requests.
- Staff, superusers, and members of the Admin or Scheduler groups can work with any physician and can create `ADMIN` requests.
- Users without a linked Physician profile receive a read-only calendar context rather than an invented clinician identity.

Single and bulk request writes are allowed only while the block is `PRE_BUILD` or `BUILD`. `PREVIEW` and `ARCHIVE` requests are read-only.

Schedulers can bulk-apply a request to multiple physicians and dates. The UI includes date helpers for every occurrence of a weekday, remaining occurrences of a weekday, and the nth weekday of each month. These helpers select explicit dates; they do not create a permanently recurring request rule.

### Current enforcement boundary

The request open/close timestamps currently produce display status only. The API gates writes by Schedule Block lifecycle, not by whether the current time is inside the request window.

When a physician has one unambiguous active Contract assignment, Request Builder uses that Contract to:

- limit an ordinary user's available request types;
- limit Shift Templates to the Contract's eligible Facilities;
- enforce High, Medium, Low, and Weekend limits for user-scope requests;
- treat Low as unlimited when configured;
- calculate and display request counters.

Staff, superusers, Admins, and Schedulers always receive all request type options. Admin-scope requests coexist with and remain hidden from the physician's user-scope request. Contract limits apply to user-scope requests; admin-scope requests do not consume them.

Weekend counting follows Shift Template weekend designations. Day Off counts once when at least one eligible, active template designates that date's weekday as a weekend day. Shift Off counts once when at least one selected template carries that designation. Multiple selected shifts on the same request still count as one request.

If Atlas cannot resolve exactly one active Contract for the physician, Request Builder does not infer cross-domain policy. An ordinary user cannot create a new request type until an applicable Contract is unambiguous; they may still select `None` to remove an existing user request. Admins and Schedulers retain all request type options. Resolving multiple domain Contracts requires a separate product decision.

`FIXED` is a high-penalty preference, not a hard scheduling constraint.

## Contracts

### Settled purpose

A Contract is a named, domain-specific scheduling rule set. Contract names are unique within a Domain, not globally.

Each Contract has:

- one Domain;
- active/inactive status;
- one or more selected Facilities in the current UI;
- configurable workload, shift, night, weekend, and request settings;
- zero or more assigned physicians.

The supported rule configuration includes:

- workload minimums and maximums by week, month, or Schedule Block, measured in hours or shifts;
- separate minimum and maximum penalty weights and optional spreading of violations;
- minimum time off, circadian handling, days-in-row, and same-shifts-in-row settings;
- named Shift Template groups with their own period rules;
- night volume, consecutive-night, and recovery settings;
- weekend volume, consecutive-weekend, consecutive-weekend-shift, and Friday-night-before-weekend-off settings;
- allowed request types, request limits, and request weights.

These are configurable mechanisms. Atlas must not hard-code a universal workload, night, weekend, or request policy.

### Assignment and lifecycle rules

A physician can have at most one default Contract per Domain. Assigning that physician to another Contract in the same Domain replaces the previous default assignment.

Inactive Contracts cannot receive physician assignments unless reactivated. Contracts are deactivated rather than deleted. Duplicating a Contract copies its Domain, Facilities, and rule-setting dictionaries, gives it a unique `(Copy)` name, and creates it inactive. Physician assignments are not copied.

Domain-specific physician membership is not modeled yet. The current Contract editor therefore offers all active physicians on its Users tab. This fallback must not be mistaken for a settled cross-domain membership policy.

### Current enforcement boundary

Contract rules are persisted as structured JSON configuration. Request Builder consumes the applicable Contract's Facility eligibility, allowed request types, request limits, and unlimited-Low setting. No optimizer or schedule-building engine in the current repository evaluates workload, shift, night, or weekend scheduling rules. The repository does not yet contain an operational schedule constraint engine.

## Facilities

A Facility represents a medical location where shifts are scheduled. Its settled fields are:

- full name;
- required short name;
- timezone, defaulting to `UTC`;
- display color, defaulting to `#2563eb`;
- active/disabled status.

The short name is the compact scheduling label and is used to generate Shift Template names. Timezone and color are presentation/scheduling metadata used in the scheduler experience.

Facilities are disabled and re-enabled rather than deleted. Creation and selection workflows generally offer only active Facilities; disabling preserves existing relationships and history.

## Physicians

A Physician profile is linked one-to-one with an authentication user. Its settled fields are:

- first and last name;
- unique email, also used as the username;
- optional display name;
- optional primary Facility;
- clinician type: Physician, PA, or NP;
- FTE, defaulting to `1.00`;
- active/disabled status.

Display name is preferred in scheduling interfaces, with the user's full name and username as fallbacks.

The primary Facility is descriptive/default affiliation; it does not restrict the physician to that Facility. Contract Facilities and actual Shift assignments are separate concepts.

Physicians are disabled rather than deleted. Existing code does not define domain membership for Physicians and does not automatically assign a Contract based on FTE, clinician type, or primary Facility.

## What Atlas Explicitly Should Not Build

Atlas should not build:

- organization-specific scheduling policy as hard-coded product behavior;
- physician assignment, dated scheduling, or My Schedule integration inside Shift Builder;
- a second editable name for Shift Templates or Schedule Blocks when the established name is generated;
- a single `weekend_shift` flag in place of explicit Friday/Saturday/Sunday designations;
- hidden lifecycle edits that bypass Schedule Block transitions;
- destructive deletion as the normal lifecycle for Facilities, Physicians, Shift Templates, Contracts, or published Schedule Blocks;
- silent replacement of a published Live Schedule without explicit overlap acknowledgement;
- recurring request records when the current pattern helper is only a way to select explicit dates;
- claims that Contract workload, shift, night, or weekend scheduling rules are enforced before the optimizer integration actually exists.

The following roadmap items are deferred, not permanently prohibited: the schedule version tree beyond the initial BUILD version, penalty engine, background optimization, preview/publish/live editing beyond the current block lifecycle, and report builder/export. They should be implemented only in their intended phase and after their product behavior is settled.

## Known Gaps That Must Not Be Filled by Assumption

- Organization has no current data model.
- Domain membership for Physicians is not modeled.
- Shift Templates are not currently assigned to Domains; BUILD generation therefore uses the global active template catalog.
- Contract workload, shift, night, and weekend scheduling rules are not evaluated by an optimizer or schedule builder.
- Request Builder cannot choose among multiple active Contracts across different Domains; Contract enforcement requires one unambiguous active assignment.
- Request timestamps do not currently authorize or reject request writes.
- Publishing records block history, but the repository does not yet contain the full Live Schedule editing workflow described by the roadmap.
- Optimization, schedule version branching, penalties, comments, reports, and exports remain roadmap work.

These gaps require explicit product decisions. Their absence is not permission to infer policy.
