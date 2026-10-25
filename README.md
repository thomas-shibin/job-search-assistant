# Hirlumo

A job search and resume platform for Canada. Members search current jobs from job
boards (Adzuna, with Jooble as a fallback) and from companies' own careers sites
(Greenhouse, Lever, Ashby, SmartRecruiters, Workable), save and track them, see how well
each one fits their resume, and get a truthful tailored resume for the ones they apply
to. Anyone can check a resume against Canadian conventions and ATS readability for
free; members fix it change by change in their own Word design, compare versions and
go back to any of them. Job alerts arrive by email or Telegram. Administrators manage
users, roles and permissions, features, job sources and platform settings.

> **What the app does today and what's planned:** [docs/FEATURES.md](docs/FEATURES.md).

> Installing on another computer? See [INSTALL.md](INSTALL.md) for one-command installers
> (Windows `install.cmd`, Linux `./install.sh`), with or without Docker.

## Architecture overview

One FastAPI application and one database, organised as a **modular monolith**:
code is grouped by business area (jobs, resumes, auth, ...), each area owning its
models, business logic and routes. Pages are server-rendered (Jinja2 + HTMX); a
JSON API under `/api` uses the same services. Background scans run in the same
process (APScheduler). See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the
design and dependency rules, and [docs/MULTI_TENANCY.md](docs/MULTI_TENANCY.md) for
accounts, per-user data isolation and roles.

```
main.py                 builds the FastAPI app: routers, error pages, security headers
cli.py                  command line (imports, scans, admin bootstrap, ...)
app/
  core/                 infrastructure used by everything
    config/             settings schema, YAML import, per-user/platform storage, current.py
    database/           engine + sessions (DATABASE_URL, default SQLite)
    tenancy.py          per-user data isolation enforced by the session
    paths.py            project root, data/ and config locations
    exceptions.py       AppError / IntegrationError bases
    logging.py          structured JSON logging
  integrations/         external services, isolated from business logic
    job_boards/         Adzuna and Jooble search; Greenhouse, Lever, Ashby, SmartRecruiters, Workable feeds
    ai/                 analysis provider interface, OpenAI, free mock
    email.py            SMTP delivery (or a local outbox in development)
    telegram.py         Telegram Bot API client
    libreoffice.py      DOCX -> PDF
    object_storage.py   S3-compatible bucket for off-server backups
    desktop/            desktop notifications (native single-user install)
  modules/              one package per business area
    auth/               sign-up/sign-in (email code, password, Google, Facebook, LinkedIn), sessions, account
    access/             roles, permissions, feature switches, audit log, admin pages
    jobs/               search, job pool, postings/filters, saved jobs, catalog, ingestion
    applications/       tracked jobs, status workflow, application bundles, retention
    matching/           keyword scoring, AI fit assessment, analyze/tailor workflows
    resumes/            resume import/editing, versions, Canadian + ATS check and fixes, tailoring, DOCX/PDF, Quick Tailor
    profiles/           job seeker profile, personal Settings page
    alerts/             watchlist, alert matching and sending, Telegram bot commands
    usage/              activity log, hourly limits, AI call costs
    admin/              activity/usage dashboards, job sources, platform settings, health
    backups/            nightly encrypted backups, rotation, restore
  web/                  page shell: templates, static files, public pages, shared view helpers
  workers/              scheduled scans and job-pool refresh, run lock and history
  shared/               small pure helpers
  models/__init__.py    imports every model so Alembic sees the whole schema
alembic/                database migrations
tests/                  unit/ and integration/ tests
docs/                   features, architecture, multi-tenancy, backups, sign-in setup, go-live and growth plans
scripts/                deploy.sh, move-data.sh, package.py, build_places.py
```

### Module responsibilities

| Module | Owns | Entry points |
|---|---|---|
| `auth` | accounts, email codes, passwords, sessions, Google/Facebook/LinkedIn sign-in, account deletion | `routes.py` (pages), `api.py` (`/api/auth/*`), `deps.py` (route guards) |
| `access` | roles, permissions, route permission table, feature switches, audit log | `rbac.py`, `service.py`, `routes.py` (`/admin/roles`, `/api/admin/*`) |
| `jobs` | job search, job pool, postings and filters, saved jobs, search-term catalog | `service.py`, `routes.py` (`/jobs`, `/saved`), `api.py` (`/api/jobs/*`) |
| `applications` | tracked jobs and their statuses, application files, retention | `service.py`, `state_machine.py`, `routes.py` (`/applications`, `/jobs/{id}`) |
| `matching` | keyword scoring, AI requirement extraction and fit assessment | `workflows.py`, `routes.py` (analyze buttons) |
| `resumes` | resume parsing/editing, versions, the Canadian and ATS checks and their fixes, the free public check, tailoring with provenance checks, documents | `canadian.py`, `ats.py`, `canadian_convert.py`, `versions.py`, `tailor.py`, `routes.py` (`/resume`, `/resume/canadian`, `/resume/compare`, `/canadian-resume-check`, `/quick`) |
| `profiles` | job seeker profile, experience/education, personal settings | `service.py`, `routes.py` (`/profile`, `/api/profile`), `settings_routes.py` |
| `alerts` | watches, alert matching, Telegram linking and bot commands | `watchlist.py`, `routes.py` (`/watchlist`) |
| `usage` | activity log, hourly search/AI limits, AI costs | `quota.py`, `costs.py` |
| `admin` | dashboards, per-user limits, job sources, platform settings, health | `routes.py` (`/admin/*`) |
| `backups` | nightly encrypted backups, rotation, restore into a new folder | `service.py`, `crypto.py`, `cli.py backup*` |

## Local development

Requirements: Python 3.11+; LibreOffice (`soffice`) for PDF output (optional; DOCX
works without it).

```bash
python3 -m venv .venv && source .venv/bin/activate     # Windows: .\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
python cli.py init                  # data folders + a starter config/config.yaml
cp .env.example .env                # then fill in the values you need (see below)
alembic upgrade head                # create/upgrade the database
python cli.py serve                 # http://127.0.0.1:8000 (+ the background scheduler)
```

Sign in with an email listed in `ADMIN_EMAILS` to become the first Super Admin (or run
`python cli.py grant-role --email you@example.com --role SUPER_ADMIN`). Without SMTP
settings, sign-in codes are written to `data/mail_outbox/` instead of being emailed.

### Docker

Two compose files, one per place the app runs:

| | Development (this computer) | Production (https://hirlumo.com) |
|---|---|---|
| File | `docker-compose.dev.yml` | `docker-compose.yml` |
| Settings | `.env` | `.env.prod` (or `.env` on a server set up by the installer) |
| Address | `http://127.0.0.1:8001` (`APP_PORT`) | `https://hirlumo.com` (`DOMAIN`), via Cloudflare Tunnel or Caddy |

```bash
# Development
docker compose -f docker-compose.dev.yml up --build -d   # applies migrations on start
docker compose -f docker-compose.dev.yml logs -f app
docker compose -f docker-compose.dev.yml exec app python cli.py <command>
docker compose -f docker-compose.dev.yml down            # data/ and config/ are bind-mounted and kept

# Production
docker compose --env-file .env.prod --profile tunnel up -d   # or --profile https for Caddy
```

Each file fixes `PUBLIC_URL` itself (127.0.0.1 in development, `https://$DOMAIN` in
production), so sign-in callbacks and email links always match where the app runs.
On this computer both use the same `data/` folder; run one at a time. Compose expands
`$` in values: quote any value that contains `$` with single quotes.

**On the server** the data lives outside the code folder (`HIRLUMO_DATA_DIR` and
`HIRLUMO_CONFIG_DIR` in `.env.prod`, set by `sudo ./scripts/move-data.sh`), so copying
in a new version can't replace it, and the app refuses to start if that folder is
missing. Send a new version from this computer with `./scripts/deploy.sh`: it runs the
tests, copies only the code and build files (never `data/`, `config/` or `.env*`),
rebuilds and waits until the app is healthy. See [INSTALL.md](INSTALL.md) section 8.

## Environment configuration

Secrets and deployment settings live in `.env` (see [.env.example](.env.example) for
every variable with comments). Everything else is a setting stored in the database and
edited on the Settings pages.

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | database location (default `sqlite:///data/job_assistant.db`) |
| `PUBLIC_URL` | where users reach the app; used in email links and OAuth redirects |
| `ADMIN_EMAILS` | accounts that become Super Admin on sign-up (trusted bootstrap) |
| `APP_SECRET_KEY` | signs email sign-in codes; set a long random value in production |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`, `SMTP_STARTTLS` | outgoing email |
| `MAIL_OUTBOX_DIR` | where emails are saved when SMTP isn't configured |
| `ADZUNA_APP_ID`, `ADZUNA_APP_KEY` | job-board search |
| `JOOBLE_API_KEY` | a second job board, used when Adzuna can't answer |
| `HIRLUMO_DATA_DIR`, `HIRLUMO_CONFIG_DIR` | Docker: where the data and settings live on the host (default `./data`, `./config`) |
| `BACKUP_ENCRYPTION_KEY`, `BACKUP_S3_*` | nightly backups encrypted and copied off the server ([docs/BACKUPS.md](docs/BACKUPS.md)) |
| `OPENAI_API_KEY` | AI analysis and tailoring (or set the AI provider to `mock` in platform settings) |
| `TELEGRAM_BOT_TOKEN` | job alerts by Telegram |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | optional Google sign-in |
| `FACEBOOK_CLIENT_ID`, `FACEBOOK_CLIENT_SECRET` | optional Facebook sign-in |
| `LINKEDIN_CLIENT_ID`, `LINKEDIN_CLIENT_SECRET` | optional LinkedIn sign-in (setting up all three: [docs/SOCIAL_SIGN_IN.md](docs/SOCIAL_SIGN_IN.md)) |

`config/config.yaml` only seeds settings the first time the database has none
(`python cli.py import-config --force` re-imports it).

## Database

SQLite in `data/job_assistant.db`, in WAL mode so pages can read while background jobs
write. All access goes through the engine and sessions in `app/core/database/`; every
user's rows are isolated by the session itself (`app/core/tenancy.py`). Schema changes
are Alembic migrations:

```bash
alembic upgrade head
alembic revision --autogenerate -m "describe the change"   # after changing a model
```

The app backs itself up every night (database, resumes, application files, settings),
encrypted and copied to a bucket once that's set up: see [docs/BACKUPS.md](docs/BACKUPS.md).
`DATABASE_URL` can point at PostgreSQL (`pip install -e ".[postgres]"`), but SQLite is
what the app is run and tested on, and is enough for now (see "Future scalability" in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)).

## Running tests

```bash
pytest                                   # the whole suite (no network: external APIs are stubbed)
pytest tests/integration/test_rbac.py -q # one file
```

`tests/unit/test_route_inventory.py` fails if a URL or method is added, removed or
renamed; update `tests/fixtures/routes_snapshot.json` only for a deliberate change.
`tests/integration/test_rbac.py` fails if a member or admin route has no permission rule.

## Adding a feature or module

1. Put the code in the module that owns the business area, or create
   `app/modules/<name>/` with only the files it needs: `models.py`, `service.py` for
   business logic (no FastAPI objects), `routes.py` for pages, `api.py` for JSON.
2. Import new models in `app/models/__init__.py`, then create a migration
   (`alembic revision --autogenerate -m "..."`) and review it.
3. Route handlers parse the request, call services and build the response. Take the
   session from `member_session` (or `admin_session` for cross-user admin pages) so
   queries are scoped to the signed-in user.
4. Give every new route a permission in `ROUTE_PERMISSIONS` (`app/modules/access/rbac.py`).
   Add a permission to `PERMISSIONS` there if none fits; it is created at the next start.
5. Include the router in `main.py`, add templates under `app/web/templates/`, and add
   tests. A module may import another module's services and models, never its routes.

## Adding an external integration

1. Add a client under `app/integrations/` (a module, or a package for several files)
   with its own pydantic `BaseSettings` for keys and URLs read from `.env`.
2. Keep it to communication: authentication, request building, timeouts, retries
   (`app/shared/retry.py`), response parsing into the app's own types, and errors raised
   as an `IntegrationError` subclass. Never log keys or tokens.
3. Call it from a module's service, not from routes. Stub it in tests; the test suite
   makes no real network calls.

## Command reference

```bash
python cli.py init | validate-config | import-config [PATH] [--force] [--keep-watches]
python cli.py --user EMAIL import-resume FILE [--force]       # per-user commands take --user
python cli.py --user EMAIL discover --description-file job.txt [--url URL --company C --title T]
python cli.py --user EMAIL analyze JOB_ID | --all
python cli.py --user EMAIL generate-resume JOB_ID
python cli.py --user EMAIL approve|skip|open-application|mark-applied|mark-interview|mark-offer|mark-rejected|archive JOB_ID
python cli.py --user EMAIL rebuild-resumes [--job JOB_ID] [--no-pdf]
python cli.py serve [--host 0.0.0.0 --port 8000]              # web app + scheduler
python cli.py run                                             # one scheduled scan now
python cli.py cleanup-jobs [--dry-run]                        # apply the retention policy
python cli.py catalog-scan [--country ca]                     # refresh search-term suggestions
python cli.py set-password --email EMAIL                      # set an account's password from the server
python cli.py grant-role --email EMAIL --role ROLE [--revoke] # bootstrap/recover roles (audited)
python cli.py claim-legacy-data --email EMAIL                 # give pre-accounts data to one account
python cli.py backup | backup-list | backup-key               # back up now, list backups, make an encryption key
python cli.py backup-restore NAME --to DIR                    # unpack a backup into a new folder
python cli.py <command> --help
```

## What data leaves the server

- **AI provider** (OpenAI, unless the provider is set to `mock`): a job's description,
  and for analysis and tailoring the structured fields of the user's resume relevant
  to it. Never the original file, never other users' data.
- **Job sources**: search terms go to Adzuna (and to Jooble when Adzuna can't answer); the
  job pool reads public careers feeds.
- **Backups** (when `BACKUP_S3_*` is set): the nightly backup, only ever encrypted with
  `BACKUP_ENCRYPTION_KEY`, which stays on the server.
- **Email** (SMTP provider): sign-in codes and account emails.
- **Telegram**: title, company, location, score and link of jobs matching a user's
  watches, sent to the chat they linked.
- **The free resume check** sends nothing to other services. The file is read from a
  temporary folder that's removed straight after (`/tmp`, which is in memory in Docker),
  then held in memory for 30 minutes and deleted.

API keys stay in `.env`, which is git-ignored and never logged.
