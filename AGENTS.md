# Atlas Repository Instructions

These instructions apply to every task performed in this repository. The product owner may explicitly change them at any time.

## Required first step

Before inspecting, editing, testing, committing, deploying, or operating Atlas:

1. Read `docs/engineering/atlas-source-of-truth.md` completely.
2. Verify that the working directory is the primary checkout at `C:\Users\turne\Desktop\atlas`.
3. Verify that the active branch is `mobile-beta` and that it tracks `origin/mobile-beta`.
4. Check the working tree and compare the local branch with `origin/mobile-beta` before making changes. Preserve any existing user changes.

If any of these checks disagree, stop and resolve the discrepancy before changing anything. Do not silently work from a project mirror, copied source tree, additional worktree, temporary branch, `main`, or `v2`.

## Single authoritative pathway

Atlas has one authoritative development and deployment pathway:

1. Local repository: `C:\Users\turne\Desktop\atlas`
2. Local branch: `mobile-beta`
3. GitHub repository: `ronaldcturner-atlas/atlas`
4. GitHub branch: `mobile-beta`
5. Railway environment: `mobile-beta`

Desktop, responsive mobile, and the installed PWA are one product. They use the same application code, backend, permissions, workflows, and Railway data. Do not create a separate mobile application or a separate desktop/mobile deployment path.

Do not inspect, modify, deploy, restart, delete, or treat another Railway environment as active unless the product owner explicitly changes the authoritative environment.

## Data and local services

The Railway `mobile-beta` database is the authoritative shared beta-test data store. The local database is a development and optimizer-testing clone, not a second authoritative database. Never overwrite Railway from local data merely because the local copy is newer.

Use the canonical local Atlas Docker stack and the active local database volume documented in the source of truth. Do not create or retain alternate Atlas containers, database volumes, exported databases, worktrees, source copies, or backup branches unless a bounded task requires one. Clearly label any temporary artifact and remove it after its verified changes are incorporated into `mobile-beta`.

## Changes and handoff

- Make product and code changes only in the authoritative checkout and branch.
- Preserve unrelated user changes.
- Test changes in proportion to their risk before committing.
- When asked to commit or push, commit to `mobile-beta` and push only to `origin/mobile-beta`.
- Railway deployments must come from the same GitHub branch and commit.
- Before reporting completion, state whether the working tree is clean, whether local and GitHub match, and whether anything remains uncommitted or unpushed.
- Update `docs/engineering/atlas-source-of-truth.md` whenever settled product behavior, terminology, permissions, workflows, or the authoritative pathway changes.
