# Atlas production environment

Atlas production configuration is intentionally fail-fast. The backend will not
start with a missing or development-grade secret key, an unrestricted hostname,
or development database credentials.

## Backend settings module

Production processes must set:

```text
DJANGO_SETTINGS_MODULE=config.settings.production
```

The WSGI and ASGI entry points default to production settings as an additional
safeguard. Local `manage.py` commands continue to default to development settings.

## Required backend variables

- `SECRET_KEY`: a unique random value of at least 50 characters. Never commit it.
- `ALLOWED_HOSTS`: comma-separated public backend hostnames. `*` is rejected.
- Database configuration, preferably Railway's `DATABASE_URL`. If it is not
  present, `DB_NAME`, `DB_USER`, `DB_PASSWORD`, and `DB_HOST` are required.

Railway's `RAILWAY_PUBLIC_DOMAIN` is automatically added to `ALLOWED_HOSTS` and
to the trusted backend origin when Railway provides it.

## Browser origins

Set `FRONTEND_ORIGINS` to the comma-separated HTTPS origins allowed to call the
API, for example:

```text
FRONTEND_ORIGINS=https://app.example.com
```

`CORS_ALLOWED_ORIGINS` and `CSRF_TRUSTED_ORIGINS` may be supplied separately
when their values need to differ. Otherwise they inherit `FRONTEND_ORIGINS`.

## Frontend build variable

When the frontend and backend use different public services, set this while
building the frontend:

```text
VITE_API_BASE_URL=https://api.example.com/api
```

When they share one hostname, omit it and Atlas uses `/api`. Local Vite
development also uses `/api` and proxies it to `http://localhost:8000`.

Demo credentials render only in a development build. The server-side Role test
tool is forcibly disabled by production settings, even if a conflicting
environment variable is supplied.

Production also rejects the shared local-development password `atlas`, even if
an imported test account still has that password hash. New users and Org Admin
password resets receive a unique random temporary password that is displayed
once. The user's existing sessions are revoked, and the temporary password must
be changed before any application API other than logout and account recovery is
available.

## First Org Admin and beta-user onboarding

Production disables self-service Organization creation; the first authenticated
account can never claim an empty deployment. On a new empty database, the
bootstrap command creates the initial Organization, Region, Domain, default Role
templates, and Org Admin together.

An organization must always have an active Org Admin. Set a strong one-time
`INITIAL_ORG_ADMIN_PASSWORD` environment variable and run:

```text
python manage.py bootstrap_org_admin --organization-name "Example Organization" --region-name "Example Region" --domain-name "Physician" --email admin@example.com --first-name First --last-name Admin
```

For an Organization that already exists, use `--organization-id` instead. The
command will not create another Organization after any Organization has an
active membership. An unused placeholder created by initial migrations does not
prevent first-time setup.

The command refuses to run when that organization already has an active Org
Admin. It never prints the password. Remove `INITIAL_ORG_ADMIN_PASSWORD`
immediately after the command succeeds; the new Org Admin must replace it on
first login.

Afterward, create the small number of controlled-beta accounts from User
Management. Copy each generated password at creation and deliver it directly to
that specific tester through a separate trusted channel. The password is not
recoverable after its one-time display. If it is lost, an Org Admin can issue a
new temporary password from that user's profile. Never share one temporary
password among multiple users.

An Org Admin may reset credentials only for a user whose every Organization is
administered by that Org Admin. This prevents one Organization from taking over
a shared user's account. Platform support/superuser intervention remains the
recovery path when no active Org Admin can sign in.

Region, Domain, Facility, and Domain-access identities are treated as durable
security boundaries. Editing may rename or deactivate those records, but it
cannot move them to another Region, Domain, user, or Organization. Remove and
recreate access deliberately when its scope must change. A user shared across
Organizations can have their global profile or password changed only by a
superuser or by an administrator who controls every Organization to which that
user belongs.

The current shift-trade approval switch remains application-wide. In a deployment
with more than one active Organization, only a platform superuser may change it;
an Org Admin cannot alter every other Organization's behavior.

## Sessions and HTTPS

Production session cookies are always secure and HTTP-only. Atlas uses a
12-hour sliding inactivity window by default, controlled by
`SESSION_COOKIE_AGE_SECONDS`. Every authenticated request refreshes that window.
Logout invalidates the server-side session immediately.

`SESSION_COOKIE_SAMESITE` defaults to `Lax`, which is appropriate when the
frontend and API share a site, including sibling subdomains. Use `None` only if
the frontend truly runs on a different site; secure cookies remain mandatory.
`Strict` is also supported for a same-site deployment with no external login
links.

Production users can launch only the current V2 optimizer. Historical V1 runs
remain readable, while new V1 run requests are rejected. Internal V1-derived
construction and scoring modules remain in the deployed image because V2 still
uses those validated components.

Atlas honors Railway's forwarded HTTPS header, redirects direct HTTP requests to
HTTPS, and begins beta with a one-hour HSTS duration. `SECURE_HSTS_SECONDS` can
be increased after the HTTPS configuration has been verified in production.
Subdomain inclusion and browser preload are intentionally disabled during beta.

## Browser request protection

Atlas issues a secure CSRF cookie before checking the signed-in user. The
token is also returned by the bootstrap endpoint so a separately hosted
frontend never needs to read a cookie belonging to the API hostname. The
frontend automatically returns it on every state-changing Atlas API request.
Login is explicitly CSRF-protected, and authenticated API requests use
Django REST Framework's standard session CSRF enforcement. Do not add
CSRF-exempt session authentication to new endpoints.

Repeated login attempts are limited independently by source address and by the
normalized email being attempted. The beta defaults are `20/min` per source and
`5/min` per account; `LOGIN_IP_RATE` and `LOGIN_ACCOUNT_RATE` can adjust them.
These counters use Django's configured cache. A single Railway backend instance
is sufficient for the controlled beta; configure a shared cache before running
multiple backend replicas so all replicas enforce one common limit.

## Production web process and health checks

The backend image starts with Gunicorn rather than Django's development server.
Startup applies database migrations, collects static assets, and stops without
serving traffic if either operation fails. Railway supplies `PORT`; optional
`WEB_CONCURRENCY`, `GUNICORN_THREADS`, and `GUNICORN_TIMEOUT_SECONDS` variables
control the process without changing the image.

Use `/api/ready/` as Railway's health-check path. It returns success only when
the database is reachable and every application migration is applied. Its error
response deliberately omits connection details. `/api/health/` remains a
lightweight liveness endpoint that proves the web process can respond without
making a database outage look like a crashed process.

Health responses use `ATLAS_RELEASE`, then Railway's commit SHA, as the release
identifier. This makes it possible to confirm which build is running without
exposing configuration or source details.

## Production logs

Production application logs are emitted as one JSON object per line to stdout,
which Railway can retain and search without a filesystem log volume. Every HTTP
response includes `X-Request-ID`; request logs contain that same identifier,
method, path without its query string, response status, elapsed time, and the
authenticated user ID when available. Request bodies, passwords, cookies,
authorization headers, and query strings are never included.

Normal health-check traffic is omitted to reduce noise. Errors, rejected
requests, and requests slower than `ATLAS_SLOW_REQUEST_MS` remain visible. The
default threshold is 1000 milliseconds, and `LOG_LEVEL` defaults to `INFO`.
When a beta tester reports an error, ask for the request ID if the interface
shows it or obtain it from the affected browser request in its network panel.

## Backups and recovery

Enable Railway's managed PostgreSQL backups before inviting beta testers. For
the controlled beta, retain at least seven daily recovery points. Provider
backups are the primary recovery mechanism because they remain available when
an application container or attached application volume fails.

Atlas also includes a supplemental backup command. Attach a persistent Railway
volume at `/data`, set `ATLAS_BACKUP_DIR=/data/backups`, and run this from a
scheduled Railway service if an independently downloadable archive is desired:

```text
python manage.py backup_database
```

Each backup is written atomically in PostgreSQL custom format and receives a
separate SHA-256 checksum. Database passwords are passed to PostgreSQL through
the child-process environment and never placed in command arguments or output.
Verify an archive after downloading it and before relying on it:

```text
python manage.py verify_database_backup /path/to/atlas.dump
```

Perform the first recovery drill before beta launch and repeat it after major
schema changes. Restore into a separate temporary PostgreSQL database first,
run migrations, and inspect users, Organizations, schedule blocks, published
schedules, and optimizer history. Only restore production after the web and
optimizer services have been stopped and the target database name has been
checked explicitly:

```text
python manage.py restore_database_backup /path/to/atlas.dump --confirm-database exact_database_name
```

The restore command verifies both the checksum and PostgreSQL archive structure
before changing the target. It restores in a single transaction and uses
`--clean --if-exists`; therefore it replaces the target database's Atlas objects
and must never be run casually against the live service.

See `backend/.env.production.example` and `frontend/.env.example` for templates.

## Railway service layout

Use one Railway project with these services:

- `postgres`: Railway PostgreSQL with managed backups enabled.
- `backend`: repository root directory `/backend`, config file
  `/backend/railway.web.json`, private networking only.
- `optimizer-worker`: repository root directory `/backend`, config file
  `/backend/railway.worker.json`, no public domain.
- `optimizer-worker-2`: the same worker configuration, no public domain.
- `frontend`: repository root directory `/frontend`, using
  `/frontend/railway.json`, with the only public domain.

Keep the backend service name exactly `backend`, or set
`BACKEND_INTERNAL_URL` on the frontend to the backend's Railway private-network
hostname and port. The frontend proxies `/api` over private networking, so the
browser uses one origin and `VITE_API_BASE_URL` should remain unset.

Set `ALLOWED_HOSTS` on the backend to the frontend public hostname and set
`FRONTEND_ORIGINS` to its full HTTPS origin. Give the backend and both workers
the same `SECRET_KEY`, `DATABASE_URL`, and production optimizer variables.
Only the frontend receives a public domain.

Deploy the backend first. Its startup applies migrations and `/api/ready/`
confirms the schema is current. Workers run `migrate --check` and restart rather
than processing jobs against a pending schema. After the backend is ready,
deploy the frontend and workers.

Configure watch paths in Railway to avoid unrelated rebuilds:

- Backend and workers: `/backend/**`
- Frontend: `/frontend/**`

Railway configuration remains an operator-controlled step. Do not create the
project or expose a public domain until the final pre-deployment checklist and
backup plan have been reviewed.
