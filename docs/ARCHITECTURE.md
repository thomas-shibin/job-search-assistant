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
| `auth` | Accounts, sign-up/sign-in by email code or password, Google, Facebook and LinkedIn sign-in, sessions, password reset, account deletion; the route guards (`deps.py`) every member route uses |
| `access` | Roles, permissions and which route needs which (`rbac.py`); feature switches; escalation rules and the audit log (`service.py`); admin pages and `/api/admin` |
| `jobs` | Job search across the job board and the company-careers job pool, showing only postings whose title matches the typed title, best match first (`relevance.py`: whole words, synonyms such as HR = Human Resources; a job that only mentions the term in its description is left out); smart search, which reads a typed sentence into titles, places and filters and fixes spelling (`query.py` rules, `interpret.py` asking the AI provider only when the rules can't); radius search ("within 25 km of Victoria", `places.py`: distances between Canadian towns from a bundled GeoNames list, the job board's own `distance` for its results); postings and filters, saved jobs, the search-term catalog, ingestion of postings into a user's jobs |
| `applications` | Tracked jobs: the list, a job's page, the status workflow (`state_machine.py`), application files, retention |
| `matching` | Keyword scoring, AI requirement extraction and fit assessment, the analyze/tailor workflows |
| `resumes` | Resume import and editing; versions (`versions.py`: compare any two, go back to any); the Canadian check (`canadian.py`) and ATS readability check (`ats.py`), each finding backed by a source (`check_sources.py`); the converter (`canadian_convert.py`), which makes only the changes the member picks, inside their own Word file (`documents/docx_edit.py` for wording, `documents/docx_layout.py` for tables, text boxes, columns and header contact details); the free public check (`public_check.py`: no account, file held in memory 30 minutes); tailoring with a truthfulness check; DOCX/PDF output; Quick Tailor |
| `profiles` | Job seeker profile (experience, education, preferences) and the personal Settings page |
| `alerts` | Watches, alert matching, and sending by email (`email_alerts.py`: after every scan, or a daily or weekly summary, with one-click unsubscribe) and Telegram; Telegram chat linking and bot commands |
| `usage` | Activity log, hourly limits on searches and AI actions, AI call costs; the free plan's lifetime allowance per person and the hashed marks that link one person's accounts (`free_plan.py`); the plans shown on `/plans` (`plans.py`) |
| `help` | The help knowledge base (`articles/*.md`), its Markdown rendering and live numbers (`knowledge.py`), the question matching behind the Help centre search and the chat assistant (`nlp.py`), and the anonymous log of questions asked |
| `feedback` | The review card (asked once after a few minutes of use; later / never), Contact us messages (kept and emailed to the team with the member as Reply-To), and the admin Feedback & messages page |
| `admin` | Activity and usage dashboards, per-user limits and suspension, duplicate accounts, job sources, platform settings, health |
| `backups` | Nightly backups of the database and members' files: consistent SQLite copy, encrypted (`crypto.py`) before it's copied to an S3-compatible bucket (`integrations/object_storage.py`), 7 daily / 4 weekly / 6 monthly kept, restore into a new folder only ([BACKUPS.md](BACKUPS.md)) |

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
| Jooble job search | `integrations/job_boards/jooble.py` | Partner key; the second board, asked only when Adzuna can't answer (not set up, members' daily share used, or every query failed, which skips Adzuna for 10 minutes). Short previews; nothing stored |
| Greenhouse, Lever, Ashby, SmartRecruiters, Workable | `integrations/job_boards/*.py` | Public careers-site feeds, read into the shared job pool (`modules/jobs/pool`). SmartRecruiters reads only Canadian postings and fetches each description once. `pool/suggested.py` lists checked non-tech Canadian employers to add from Job sources |
| AI provider | `integrations/ai/` | `AIProvider` interface; OpenAI or a free deterministic mock; every call logged with cost (`usage/ai_calls.py`) |
| Email | `integrations/email.py` | SMTP, plain text plus optional HTML; without `SMTP_HOST`, messages are saved to `MAIL_OUTBOX_DIR`. Message texts live with their feature (`auth/emails.py`, `alerts/email_alerts.py`) |
| Telegram | `integrations/telegram.py` | Bot API client; linking and commands are in `modules/alerts` |
| LibreOffice | `integrations/libreoffice.py` | DOCX -> PDF with its own settings folder in the temp folder (so it runs as a user with no account entry, as in Docker); failure degrades to DOCX only |
| Object storage | `integrations/object_storage.py` | Any S3-compatible bucket (Cloudflare R2, Backblaze B2, AWS S3) for backups; only encrypted files are uploaded |

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
- **Backups** (`modules/backups`): every night at `BACKUP_HOUR`, and 10 minutes after
  start-up if the last one is over 26 hours old; also on demand from Admin → Health.

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
| Choosing changes one by one (each line, table or text box), by fix or by resume section | Yes; `canadian.changes` and `documents/docx_layout.preview` list them, `plan(only=...)` and `docx_layout.fix(only=...)` make only those. A PDF's fixes apply whole (the standard layout is rebuilt from content) | |
| ATS layout fixes: tables, text boxes, columns, header-only contact details | Yes; `resumes/documents/docx_layout.py`, inside the member's Word file | |
| Resume versions: compare any two, download any, go back to any | Yes; `resumes/versions.py`. Going back makes a version active again; nothing is deleted or renumbered, and new versions are numbered after the highest | |
| Canadian equivalents of job titles | | Optional, one call per request; added in brackets after the original title, never more senior |

## Job source policy

Only public, documented APIs are automated: Adzuna, Jooble, and the careers-site feeds
companies publish through Greenhouse, Lever, Ashby, SmartRecruiters and Workable.
Job-board results (Adzuna, Jooble) are shown live and cached for an hour at most,
never copied into the job pool: Adzuna's terms need a licence for commercial use
and stored or aggregated data, and Jooble's terms come with its key.

Every source follows the same two rules (`modules/jobs/dedupe.py`):

- **Current only.** Job-board ads older than `platform.board_max_job_age_days`
  (30) are never asked for or shown; boards don't take an ad down when the job is
  filled. Company-feed jobs older than `platform.pool_max_job_age_days` (60) are
  hidden, a job a feed stops listing is deleted at its next refresh, and if a feed
  can't be read for two refreshes (at least a day) its jobs are hidden until it
  can. Greenhouse jobs are dated by `first_published`, not `updated_at`. A hidden
  job's page says it's no longer available.
- **Each job once.** The same company, title and city is one job, whichever
  source lists it; the company's own feed is kept first, then Adzuna, then Jooble.
  A posting's own location outranks its company's office list when they disagree. Users can also paste any
posting themselves. LinkedIn and Indeed are never scraped or scripted; they appear
only as links the user opens. A compliant future path for LinkedIn jobs is parsing the
user's own job-alert emails.

## Deployment and where data lives

Production is one Docker container (`docker-compose.yml`) on a home server, reached
through Cloudflare Tunnel. The container runs as an unprivileged user with no Linux
capabilities and a read-only filesystem; it can write only to the data and settings
folders and an in-memory `/tmp`.

- **Data outside the code.** The host folders mounted at `/app/data` and `/app/config`
  are `HIRLUMO_DATA_DIR` and `HIRLUMO_CONFIG_DIR` (default `./data`, `./config`).
  `scripts/move-data.sh` moves them to `/srv/hirlumo`, owned by a `hirlumo` account
  (id 1990) that can't log in, readable by nobody else. The mounts are made with
  `create_host_path: false`, so a missing folder stops the start instead of starting
  on an empty database.
- **Releasing.** `scripts/release.sh`, run on the developer's computer, runs the tests,
  builds the Dockerfile's `release` stage (bytecode only: `compileall -b -o 2`, every
  `.py` removed, Alembic `sourceless = true`; the browser files from the `web` stage:
  `deploy/web-build/build.mjs` runs terser, javascript-obfuscator and clean-css, with
  versions pinned by its lockfile and no source maps), checks it (also: every `.js` and
  `.css` is one line), and pushes it through an SSH
  tunnel to a registry bound to the server's 127.0.0.1 (`deploy/registry/`). The server
  pulls it, backs up the database, pins `APP_IMAGE` in `.env.prod` and starts it with
  `--no-build`. The `dev` stage is the source, for docker-compose.dev.yml. The older
  `scripts/deploy.sh` (copy the source, build on the server) is kept as a fallback.
- **Backups** are described in [BACKUPS.md](BACKUPS.md). Removing the container or image
  never touches the data: it's a host folder.

## Future scalability

The current design fits one server and SQLite comfortably for the expected first users.
In order of likely need:
1. **PostgreSQL** when concurrent writes grow: change `DATABASE_URL` and copy the data.
   Migrations and the ORM are already database-neutral.
2. **Shared state for multiple processes.** Rate limits, the search cache, remembered
   search results, per-user quota locks, the job-board budget cache and the free
   check's held files are in-process memory today (in `jobs/search.py`,
   `jobs/postings.py`, `jobs/service.py`, `usage/quota.py`, `auth/ratelimit.py` and
   `resumes/public_check.py`). Running several app processes needs these moved to the
   database, disk or a cache first. The held files come first: memory is capped at 200
   files or 150 MB, so under load they're dropped before a visitor signs up, and a
   restart loses them. The plan is encrypted files on disk with a 30-minute lifetime,
   plus a limit on checks running at once.
3. **Separate worker process.** The scheduler can run as its own container (it only
   needs the database) once scans compete with web requests for CPU.
4. **Splitting a module out.** Each module's routes, services and models are already
   together, so a busy area (for example search) could become its own service behind
   the same API paths if it ever needed to scale independently.
