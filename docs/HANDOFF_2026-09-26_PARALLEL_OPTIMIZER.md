# Atlas handoff: stable parallel optimizer checkpoint

Date: September 26, 2026

## Purpose

Continuity record for moving development into a new chat after implementing two independent optimizer workers. Read this before further optimizer or Schedule Build Workspace changes.

## Repository state

- Branch: `main`
- Pre-parallel checkpoint: `5ce1b61 checkpoint: stable pre-parallel optimizer`
- Parallel checkpoint message: `checkpoint: stable parallel optimizer with two independent runs`
- Added and applied migration: `backend/apps/scheduling/migrations/0033_optimizercontrol_parallel_runs.py`

Changed implementation files: `api.py`, `run_optimizer_worker.py`, `models.py`, `optimizer.py`, scheduling tests, backend settings, `docker-compose.yml`, `ScheduleBuildWorkspace.tsx`, and `index.css`.

## Parallel behavior

- Atlas permits two concurrent optimizer runs.
- Either may start as Fresh Fill or from a selected run.
- Run numbers are assigned at submission and reflect start order.
- Two workers claim separate controls.
- Each run has an isolated assignment snapshot, stop control, timer, live score, seed, diagnostics, and lineage.
- A selected source is snapshotted when the child is submitted and cannot be deleted while that child runs.
- Completed parallel results never activate automatically; the scheduler explicitly reviews and activates one.
- Run history remains until manually deleted.

## Live verification

Schedule block `Nov 2026-Jan 2027`, Schedule Version 150:

- Run 26: from Run 25; 577,000 initial; 547,000 final; one hour.
- Run 27: Fresh Fill; 3,619,000 initial; 679,000 final; one hour.
- They began 15 seconds apart, overlapped for about 59:44, saved 2,300 assignments each, and had only 156 assignment pairs in common.
- All 108 fixed Shift On requests were satisfied in both.
- Run 28: Fresh Fill; 3,544,000 initial; 520,000 final; two hours.
- Run 29: Fresh Fill; 3,490,000 initial; 571,000 final; two hours.
- Runs 28 and 29 began 24 seconds apart and completed normally at the runtime limit.
- Run 25 remains active at 577,000. No parallel result was silently activated.

Run 28 breakdown: night 105,000; weekend 266,000; same shift 4,000; shift rules 145,000; total 520,000.

Run 29 breakdown: night 105,000; weekend 280,000; same shift 6,000; shift rules 180,000; total 571,000.

Both have zero coverage, workload, request, rest, overlap, consecutive-days, invalid-assignment, and underutilization penalties.

## Validation

- Verified backup: `C:\Users\turne\Desktop\atlas-backups\atlas-parallel-checkpoint-20260926.dump`
- Backend scheduling tests: 344 passed.
- Frontend production build: passed.
- Backend, PostgreSQL, and both workers were healthy.

## Requirements to preserve

- Score only configured contract rules; no implicit default penalties.
- Manual-assignment-only physicians are excluded from optimizer movement. Their valid Shift On requests are fixed.
- Check fixed Shift On requests before suggesting optimizer changes.
- Ignore inapplicable Request Off templates on a date while honoring applicable selected templates.
- Weekend rules use templates designated as weekend shifts in Shift Builder.
- Prior live schedules inform boundary recovery and consecutive rules without importing prior-block counts into new monthly or block quotas.
- Every run retains its own result, starting score, and lineage.
- Timed or manually stopped runs preserve the best valid result.
- Never implicitly anchor Fresh Fill to a historical best; historical runs are explicit starting choices only.

## New-chat startup

Repository: `C:\Users\turne\Desktop\atlas`

1. Read this handoff.
2. Confirm `git status --short` is clean.
3. Confirm `backend`, `postgres`, `optimizer-worker`, and `optimizer-worker-2` are running.
4. Check for running controls before restarting services or applying migrations.
5. Keep the backup outside the repository and out of GitHub.
6. Verify Runs 28 and 29 open correctly and can be manually activated.
7. Confirm the UI reports zero of two optimizer slots in use.

## Decision point

Parallel optimization passed two live concurrent-run tests. No optimizer strategy change is pending. Continue from this stable checkpoint and address the user's next explicit priority.
