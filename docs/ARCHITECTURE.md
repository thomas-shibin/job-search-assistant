# Architecture

## Why a modular monolith

The platform serves a few hundred users at most for its first year, run by one small
team on one server. A single FastAPI process with one database is the cheapest thing to
run, deploy (one container) and debug. Microservices would add network calls,
deployment pipelines and data consistency problems without a scaling need behind them.

The cost of a monolith is that code tangles over time. So the code is organised by
**business area** (`app/modules/*`), each module owning its models, rules and routes,
with explicit dependency rules between them. If one area ever needs to scale or deploy
separately, its boundary already exists.

## Layers

```
main.py                      app wiring: includes every module's routers
app/web/                     page shell: templates, static files, public pages, view helpers
app/modules/<area>/
  routes.py, api.py          HTTP: parse the request, call services, build the response
  service.py (and friends)   business rules and workflows (no FastAPI objects)
  models.py                  SQLAlchemy models the module owns
  views.py, schemas.py       page renderers / JSON shapes shared by several routes (where needed)
app/integrations/            external services: job boards, AI provider, email, Telegram, LibreOffice
app/workers/                 scheduled background work
app/core/                    settings, database, tenancy, paths, logging, exception bases
app/shared/                  small pure helpers
```

Modules only have the layers they need. Most data access is SQLAlchemy queries inside
services. There is no separate repository layer, because the tenant-scoped session
(below) already isolates and centralises data access.

## Modules

| Module | Responsibility |
|---|---|
| `auth` | Accounts, sign-up/sign-in by email code or password, Google sign-in, sessions, password reset, account deletion; the route guards (`deps.py`) every member route uses |
| `access` | Roles, permissions and which route needs which (`rbac.py`); feature switches; escalation rules and the audit log (`service.py`); admin pages and `/api/admin` |
| `jobs` | Job search across the job board and the company-careers job pool, showing only postings whose title matches the typed title, best match first (`relevance.py`: whole words, synonyms such as HR = Human Resources; a job that only mentions the term in its description is left out); smart search, which reads a typed sentence into titles, places and filters and fixes spelling (`query.py` rules, `interpret.py` asking the AI provider only when the rules can't); radius search ("within 25 km of Victoria", `places.py`: distances between Canadian towns from a bundled GeoNames list, the job board's own `distance` for its results); postings and filters, saved jobs, the search-term catalog, ingestion of postings into a user's jobs |
| `applications` | Tracked jobs: the list, a job's page, the status workflow (`state_machine.py`), application files, retention |
| `matching` | Keyword scoring, AI requirement extraction and fit assessment, the analyze/tailor workflows |
| `resumes` | Resume import and editing, the Canadian resume check and converter, tailoring with a truthfulness check, DOCX/PDF output, Quick Tailor |
| `profiles` | Job seeker profile (experience, education, preferences) and the personal Settings page |
| `alerts` | Watches, alert matching, and sending by email (`email_alerts.py`: after every scan, or a daily or weekly summary, with one-click unsubscribe) and Telegram; Telegram chat linking and bot commands |
| `usage` | Activity log, hourly limits on searches and AI actions, AI call costs |
| `admin` | Activity and usage dashboards, per-user limits and suspension, job sources, platform settings, health |

## Dependency direction

```
routes / api  ->  services  ->  models, integrations  ->  database / external APIs
       \______________________________________________/
                       all may use core/ and shared/
```

Rules:
- `core/` and `shared/` never import from `modules/`, `web/` or `workers/` at import
  time. There is one deliberate exception: `core/config/store.py` looks up the user's
  name with a function-level import.
- A module may import another module's services and models, never its routes.
  Rendering shared by several modules' routes lives in a `views.py`, for example the
  tracked-job page (`applications/views.py`) or the watchlist page (`alerts/views.py`).
- Services don't take FastAPI `Request` objects. Uploads are passed as a filename and
  bytes (`resumes/uploads.py`); `import_upload` is the thin adapter for routes.
- Integrations don't call modules. Where an integration implements an interface a
  module defines, it imports that module's data types: AI providers receive
  `MatchScoreBreakdown`, and desktop notifiers receive `JobNotification`.
- The few imports that must stay inside functions to avoid a cycle say why in a comment.

## Authentication and authorization

Every member and admin route depends on `auth.deps.require_member`, through
`member_session` or `admin_session`. It authenticates the session cookie, then checks
the route's permission from `access/rbac.py`'s `ROUTE_PERMISSIONS`, which is read
fresh from the database on each request. A route missing from that table is refused,
and a test fails. Permission names (`jobs.view`, `users.suspend`) are what code checks,
never role names. See [MULTI_TENANCY.md](MULTI_TENANCY.md).

Sessions are server-side rows (`auth/sessions.py`); the cookie only carries a random
token. Each request also checks the session's time limits, a platform setting on the
admin Settings page ("Sign-in sessions"): administrators are signed out after 30
minutes without a request and 12 hours after signing in, everyone else after 7 days
idle and 14 days in all (guests: 24 hours). The limits follow the user's current
permissions, so granting the admin role tightens an open session at once.

## Database access

- One engine and session factory: `app/core/database/session.py` (`DATABASE_URL`,
  default SQLite at `data/job_assistant.db`, WAL mode). Requests get a session from
  `get_session`; background work uses `tenant_session` or `system_session`.
- **Per-user isolation is enforced by the session** (`app/core/tenancy.py`). Models
  holding a user's data inherit `TenantOwned`, and a session bound to a user only ever
  sees and writes that user's rows. Cross-user reads (admin totals, the scheduler
  choosing whom to scan) use an explicit system session.
- Each module owns its models. `app/models/__init__.py` imports them all, so Alembic
  sees the whole schema. Schema changes are Alembic migrations; settings defaults are
  seeded by migrations, and later additions to the permission catalog are synced at
  start-up.
- Raw SQL isn't used on user tables. Queries go through the ORM so tenancy applies,
  which also keeps a later move to PostgreSQL to a `DATABASE_URL` change plus data copy.

## External integration boundaries

| Integration | Where | Notes |
|---|---|---|
| Adzuna job search | `integrations/job_boards/adzuna.py` | Key-authenticated; descriptions are previews (about 500 characters), so the job pool supplies full text when it has the job |
| Greenhouse, Lever, Ashby | `integrations/job_boards/*.py` | Public careers-site feeds, read into the shared job pool (`modules/jobs/pool`) |
| AI provider | `integrations/ai/` | `AIProvider` interface; OpenAI or a free deterministic mock; every call logged with cost (`usage/ai_calls.py`) |
| Email | `integrations/email.py` | SMTP, plain text plus optional HTML; without `SMTP_HOST`, messages are saved to `MAIL_OUTBOX_DIR`. Message texts live with their feature (`auth/emails.py`, `alerts/email_alerts.py`) |
| Telegram | `integrations/telegram.py` | Bot API client; linking and commands are in `modules/alerts` |
| LibreOffice | `integrations/libreoffice.py` | DOCX -> PDF; failure degrades to DOCX only |

Bundled data: `app/modules/jobs/data/ca_places.tsv` lists Canadian towns and cities of
500+ people with their coordinates, from GeoNames (CC BY 4.0, credited on the Terms page).
`scripts/build_places.py` rebuilds it from a GeoNames download. Nothing about places is
looked up online at search time.

Each integration has its own settings class read from `.env`, its own timeouts and
retries (`shared/retry.py`), and raises `IntegrationError` subclasses. Business code
calls integrations; routes don't. Provider names never appear in user-facing pages.

## Background tasks

`app/workers/background.py` starts APScheduler inside the web process (`cli.py serve`
or the Docker container):
- **Scheduled scan** (`workers/run.py`): for every member allowed job alerts, runs
  their searches and watches in a session bound to that member, scores new jobs, and
  sends alerts. A file lock (`workers/lock.py`) prevents overlapping runs; one failing
  source never aborts the others. The interval is a platform setting.
- **Job pool refresh** (`modules/jobs/pool/service.py`): refreshes stale company feeds
  in the background.
- **Clean-up**: expired sessions, tokens and email codes, old activity, and job
  retention run after each scan.

There is no task queue. Work that a user waits for (analysis, tailoring) runs in the
request, behind hourly limits.

## Deterministic vs AI

| Step | Deterministic | AI |
|---|---|---|
| Reading a typed search ("remote HR jobs in Toronto over 80k") | Yes; rules and spelling correction against the job-title vocabulary | Only for searches the rules can't read; at most 20 an hour per member, answers kept for a day |
| Duplicate detection, hard filters | Yes | |
| Requirement extraction from a job description | | Yes; one call per description, cached by content hash |
| Match score (weighted formula) | Yes; pure function, reproducible | |
| Fit assessment ("Analyze match") | | Yes; once per job and resume version unless re-run |
| Resume tailoring | | Yes; every bullet must cite its source bullet |
| Truthfulness validation | Yes; a bullet that invents numbers or cites nothing falls back to the original text | |
| DOCX/PDF generation, address swap | Yes | |
| Canadian resume check (photo, personal details, dates, spelling, length...) and its fixes | Yes; `resumes/canadian.py`, fixes written into the member's own Word file | |
| Canadian equivalents of job titles | | Optional, one call per request; added in brackets after the original title, never more senior |

## Job source policy

Only public, documented APIs are automated: Adzuna, and the careers-site feeds
companies publish through Greenhouse, Lever and Ashby. Users can also paste any
posting themselves. LinkedIn and Indeed are never scraped or scripted; they appear
only as links the user opens. A compliant future path for LinkedIn jobs is parsing the
user's own job-alert emails.

## Future scalability

The current design fits one server and SQLite comfortably for the expected first users.
In order of likely need:
1. **PostgreSQL** when concurrent writes grow: change `DATABASE_URL` and copy the data.
   Migrations and the ORM are already database-neutral.
2. **Shared state for multiple processes.** Rate limits, the search cache, remembered
   search results and per-user quota locks are in-process memory today (in
   `jobs/search.py`, `jobs/postings.py`, `usage/quota.py` and `auth/ratelimit.py`).
   Running several app processes needs these moved to the database or a cache first.
3. **Separate worker process.** The scheduler can run as its own container (it only
   needs the database) once scans compete with web requests for CPU.
4. **Splitting a module out.** Each module's routes, services and models are already
   together, so a busy area (for example search) could become its own service behind
   the same API paths if it ever needed to scale independently.
