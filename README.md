# Job Assistant

A job search and resume-tailoring platform for Canada. Members search jobs from a
job board (Adzuna) and from companies' own careers sites (Greenhouse, Lever, Ashby),
save and track them, see how well each one fits their resume, and get a truthful
tailored resume for the ones they apply to. Job alerts arrive by Telegram.
Administrators manage users, roles and permissions, features, job sources and
platform settings.

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
    job_boards/         Adzuna search; Greenhouse, Lever, Ashby feeds
    ai/                 analysis provider interface, OpenAI, free mock
    email.py            SMTP delivery (or a local outbox in development)
    telegram.py         Telegram Bot API client
    libreoffice.py      DOCX -> PDF
    desktop/            desktop notifications (native single-user install)
  modules/              one package per business area
    auth/               sign-up/sign-in (email code, password, Google), sessions, account
    access/             roles, permissions, feature switches, audit log, admin pages
    jobs/               search, job pool, postings/filters, saved jobs, catalog, ingestion
    applications/       tracked jobs, status workflow, application bundles, retention
    matching/           keyword scoring, AI fit assessment, analyze/tailor workflows
    resumes/            resume import/editing, tailoring + validation, DOCX/PDF, Quick Tailor
    profiles/           job seeker profile, personal Settings page
    alerts/             watchlist, alert matching and sending, Telegram bot commands
    usage/              activity log, hourly limits, AI call costs
    admin/              activity/usage dashboards, job sources, platform settings, health
  web/                  page shell: templates, static files, public pages, shared view helpers
  workers/              scheduled scans and job-pool refresh, run lock and history
  shared/               small pure helpers
  models/__init__.py    imports every model so Alembic sees the whole schema
alembic/                database migrations
tests/                  unit/ and integration/ tests
docs/                   architecture, multi-tenancy, go-live plan
```

### Module responsibilities

| Module | Owns | Entry points |
|---|---|---|
| `auth` | accounts, email codes, passwords, sessions, Google sign-in, account deletion | `routes.py` (pages), `api.py` (`/api/auth/*`), `deps.py` (route guards) |
| `access` | roles, permissions, route permission table, feature switches, audit log | `rbac.py`, `service.py`, `routes.py` (`/admin/roles`, `/api/admin/*`) |
| `jobs` | job search, job pool, postings and filters, saved jobs, search-term catalog | `service.py`, `routes.py` (`/jobs`, `/saved`), `api.py` (`/api/jobs/*`) |
| `applications` | tracked jobs and their statuses, application files, retention | `service.py`, `state_machine.py`, `routes.py` (`/applications`, `/jobs/{id}`) |
| `matching` | keyword scoring, AI requirement extraction and fit assessment | `workflows.py`, `routes.py` (analyze buttons) |
| `resumes` | resume parsing/editing, tailoring with provenance checks, documents | `tailor.py`, `uploads.py`, `routes.py` (`/resume`, `/quick`) |
| `profiles` | job seeker profile, experience/education, personal settings | `service.py`, `routes.py` (`/profile`, `/api/profile`), `settings_routes.py` |
| `alerts` | watches, alert matching, Telegram linking and bot commands | `watchlist.py`, `routes.py` (`/watchlist`) |
| `usage` | activity log, hourly search/AI limits, AI costs | `quota.py`, `costs.py` |
| `admin` | dashboards, per-user limits, job sources, platform settings, health | `routes.py` (`/admin/*`) |

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

```bash
docker compose up --build -d        # http://127.0.0.1:8001; applies migrations on start
docker compose logs -f app
docker compose exec app python cli.py <command>
docker compose down                 # data/ and config/ are bind-mounted and kept
```

Compose reads `.env` via `env_file` and expands `$` in values: quote any value that
contains `$` with single quotes.

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
| `OPENAI_API_KEY` | AI analysis and tailoring (or set the AI provider to `mock` in platform settings) |
| `TELEGRAM_BOT_TOKEN` | job alerts by Telegram |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | optional Google sign-in |

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

Back up by copying `data/job_assistant.db` (with the app stopped, or with SQLite's
`.backup` while it runs). `DATABASE_URL` can point at PostgreSQL
(`pip install -e ".[postgres]"`), but SQLite is what the app is currently run and tested on.

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
python cli.py <command> --help
```

## What data leaves the server

- **AI provider** (OpenAI, unless the provider is set to `mock`): a job's description,
  and for analysis and tailoring the structured fields of the user's resume relevant
  to it. Never the original file, never other users' data.
- **Job sources**: search terms go to Adzuna; the job pool reads public careers feeds.
- **Email** (SMTP provider): sign-in codes and account emails.
- **Telegram**: title, company, location, score and link of jobs matching a user's
  watches, sent to the chat they linked.

API keys stay in `.env`, which is git-ignored and never logged.
