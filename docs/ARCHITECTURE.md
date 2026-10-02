# Architecture — Local AI Job Application Assistant

This is the permanent reference for the system design agreed before implementation
began. Update it when a milestone changes the design, not just when code changes.

## 1. Frontend choice: server-rendered (FastAPI + Jinja2 + HTMX), not React

This is a single-user, local, review-and-approve dashboard (list jobs, view a diff,
click Approve/Skip) — the sweet spot for HTMX partial-page updates, with no build
pipeline or separate dev server. A React SPA would add a second process, CORS handling,
and a state-management layer for what is fundamentally server-owned state, which
contradicts the "don't prematurely introduce infrastructure" principle.

This does not box the project in: `app/api/` exposes a real JSON API backed by the same
`app/services/` layer the templates use. A future React/Electron/mobile client would
talk to the same API with no backend rework. Resume diffing (base vs. tailored) renders
as server-side HTML with a small client-side diff library — no SPA required.

## 2. Directory structure

```
job-search-assistant/
  app/
    config/            # Pydantic settings, YAML loader, config schema, validation
    database/          # engine/session setup, declarative Base
    models/            # SQLAlchemy ORM models
    sources/           # JobSource interface + implementations
    ingestion/         # normalization, fingerprinting, dedup, hard filters
    matching/          # deterministic scorer, requirement extraction orchestration
    ai/                # AIProvider interface + OpenAI impl + mock impl, prompt templates
    resume/            # candidate profile parser, tailoring pipeline, truthfulness validator
    documents/         # DOCX rendering, PDF conversion (LibreOffice headless)
    applications/      # state machine, status history, application folder management
    notifications/     # NotificationProvider interface + Windows toast impl
    scheduler/         # APScheduler wiring, run-lock, run history recording
    api/               # FastAPI routers (JSON) — jobs, matches, resumes, applications
    ui/                # Jinja2 templates + HTMX routers (server-rendered dashboard)
    services/          # orchestration layer used by both api/ and cli.py
    utils/             # filename sanitization, hashing, structured logging
    cli.py             # Typer CLI entrypoint
    main.py            # FastAPI app factory / ASGI entrypoint
  alembic/             # migrations
  data/
    resumes/           # base_resume.docx (git-ignored, personal data)
    applications/      # generated per-job artifacts (git-ignored)
  config/
    config.example.yaml
    config.yaml        # your real config, git-ignored
  tests/
    unit/
    integration/
```

## 3. Core database entities

- **CandidateProfile** — singular row, versioned by `source_hash` (hash of the parsed
  base resume). `summary`, `raw_resume_path`, `parsed_at`.
- **Employment** — `company`, `title`, `start_date`, `end_date`, `is_current`, `location`.
- **AccomplishmentBullet** — `employment_id`, `text`, `order_index`. Source-of-truth
  bullets; every AI-tailored bullet must trace back to one of these.
- **Skill** — `name`, `category`, optional `employment_id` (to support "years of
  experience with X" derivation later).
- **Education**, **Certification**, **Project** — straightforward fields.
- **Job** — `external_id`, `source`, `source_url`, `company`, `title`, `location`,
  `remote_type`, `salary_min/max`, `currency`, `description`, `date_posted`,
  `date_discovered`, `search_profile_name`, `status`, plus `fingerprint` and
  `description_hash` for dedup/caching.
- **JobStatusHistory** — append-only audit trail (`from_status`, `to_status`,
  `changed_at`, `reason`, `actor`); `Job.status` is never silently overwritten.
- **JobRequirements** — structured extraction per job, cached by `description_hash`.
- **MatchAnalysis** — one row per scoring run: overall + per-dimension scores, strong
  /partial/missing requirement lists, explanation, `weights_snapshot` (reproducible even
  if config weights change later).
- **TailoredResume** — `docx_path`, `pdf_path`, `summary_text`, `skills_order`,
  `validation_status`.
- **TailoredBulletProvenance** — `generated_text` + `source_bullet_id` FK +
  `validation_passed`/`rejection_reason`. The auditable half of the truthfulness
  guardrail.
- **ApplicationArtifactSet** — file paths for the generated bundle under
  `data/applications/...`.
- **AICallLog** — `provider`, `operation`, `job_id`, tokens, `estimated_cost`,
  `cache_hit`. Cost tracking per §19 of the original spec.
- **SchedulerRun** / **SourceRunResult** — run start/end and per-source
  discovered/new/duplicate/qualified/failure counts.

## 4. Module interfaces

```python
# app/sources/base.py
class RawJob(BaseModel): ...
class JobSource(Protocol):
    name: str
    def discover_jobs(self, search_profile) -> list[RawJob]: ...

# app/ai/base.py
class AIProvider(Protocol):
    def extract_job_requirements(self, job_description: str) -> ExtractedJobRequirements: ...
    def tailor_resume(self, candidate, job, requirements, address) -> TailoredResumeDraft: ...
    def generate_match_explanation(self, score: MatchScoreBreakdown) -> str: ...

# app/matching/scorer.py
def score_job(candidate, requirements, search_profile, weights) -> MatchScoreBreakdown: ...
# pure function, no AI call, fully deterministic and unit-testable

# app/resume/validator.py
def validate_tailored_resume(draft: TailoredResumeDraft, candidate) -> ValidationResult: ...
# rejected bullets fall back to the original source text verbatim

# app/documents/pdf.py
class PdfConverter(Protocol):
    def convert(self, docx_path: Path) -> Path: ...
# LibreOfficePdfConverter (soffice --headless --convert-to pdf) is the V1 implementation

# app/notifications/base.py
class NotificationProvider(Protocol):
    def notify(self, notification: JobNotification) -> None: ...
```

## 5. Deterministic vs. AI-powered

| Step | Deterministic | AI |
|---|---|---|
| Dedup / fingerprinting | Yes | |
| Hard filters (location, negative keywords, already-applied) | Yes | |
| Job requirement extraction from free text | | Yes — 1 call, cached by `description_hash` |
| Match scoring (weighted formula) | Yes — pure function | |
| Match explanation text | Yes — templated by default | optional AI polish, off by default |
| Resume tailoring (summary, skill order, bullet rewrite) | | Yes — 1 call, must cite `source_bullet_id` per bullet |
| Truthfulness validation | Yes — deterministic post-check against cited source bullets | |
| Address swap | Yes — config substitution | |
| DOCX/PDF generation | Yes | |

The scoring formula is never AI-derived: this keeps scores reproducible, explainable,
and reproducibly cacheable (an unchanged job is never re-scored or re-billed).

## 6. Job source risk assessment

Two source types are unambiguously safe:

1. **Manual import** — paste a job URL/description (CLI or dashboard). Zero ToS risk,
   works for any site, and is the fallback the whole system degrades to. Exists
   regardless of what else ships.
2. **Public ATS JSON endpoints** (Greenhouse `boards-api.greenhouse.io`, Lever
   `api.lever.co/v0/postings/...`) — intentionally public, documented, unauthenticated
   APIs that companies use to power their own careers pages. Reading them is consuming a
   public API as designed, not scraping a restricted platform.
3. **Adzuna** (`api.adzuna.com/v1/api/jobs/...`) — a public, documented, key-authenticated
   job-aggregator API (free developer signup at developer.adzuna.com). Unlike Greenhouse/
   Lever, which each cover one employer's own board, Adzuna indexes postings from many
   employers/boards and is queried by keyword/location rather than a fixed company token —
   the practical answer to "search broadly" without touching LinkedIn/Indeed. Same
   "consuming a documented API with an issued key" legal footing as any other API
   integration, not scraping.

**Explicitly out of scope**: LinkedIn/Indeed scraping, automated Easy Apply, CAPTCHA
handling, or anything touching authenticated sessions on those platforms. LinkedIn was
raised as a possible source during design — see the note below on why it's excluded and
what the compliant alternative looks like. LinkedIn/Indeed only ever appear as
`source_url` destinations opened manually via **Open Application**.

### Why not LinkedIn directly, and what to do instead

LinkedIn's User Agreement (`legal.linkedin.com/user-agreement`) prohibits automated
scraping/data collection and automated interaction with the platform (including
automated Easy Apply) without their permission, and they actively enforce this
technically (rate limits, bot detection, account suspension, and they've pursued and won
litigation over scraping in the past, e.g. *hiQ Labs v. LinkedIn*, which does not grant a
license to scrape logged-in content — it addressed only public page access). Building an
automated `JobSource` against LinkedIn would mean either scraping (prohibited) or driving
a logged-in browser session to mimic a user (also prohibited, and risks the user's
account). Both are excluded by this project's own ground rules, not just by caution.

The compliant path to LinkedIn-sourced jobs, which is on the roadmap as a near-term
extension point rather than V1 scope: **parse the job-alert emails LinkedIn already sends
you** (via IMAP or the Gmail/Outlook API). That's reading your own inbox — not
interacting with LinkedIn's platform at all — so it carries none of the ToS risk while
still surfacing LinkedIn-posted jobs into the same pipeline. `JobSource` is designed so
this slots in as `EmailAlertJobSource` without touching the rest of the system.

## 7. Milestone roadmap

1. **Foundation** *(done)* — scaffold, config loader/validation, DB models + Alembic,
   structured logging, `init`/`validate-config` CLI.
2. **Resume ingestion** *(done)* — deterministic parsing of a `.docx` or `.pdf` base
   resume into `CandidateProfile` (`app/resume/parser.py`), with profile versioning
   (`CandidateProfile.version`/`is_active`) and an unparsed-content safety net
   (`raw_unparsed_sections`) so nothing is silently dropped. Both formats converge on a
   shared `LineInfo` intermediate representation before section detection runs; PDF
   extraction (via `pdfplumber`) has no bold/style metadata, so it relies on ALL-CAPS/
   alias-match heading heuristics, Private-Use-Area bullet glyph detection (common when
   Word exports a bulleted list to PDF), and wrapped-line bullet merging. Wired to
   `import-resume`.
3. **Job ingestion** *(done)* — manual-import source (`app/sources/manual.py`),
   deterministic normalization (`app/ingestion/normalize.py`, including remote-type
   inference), fingerprint + near-duplicate-description dedup
   (`app/ingestion/fingerprint.py`), and the orchestration pipeline
   (`app/ingestion/pipeline.py`) that persists `Job` rows with a `JobStatusHistory` audit
   trail and structured `JOB_DISCOVERED`/`JOB_DUPLICATE` log events. Wired to `discover`
   (manual source only; the `JobSource.discover_jobs(search_profile)` path used by
   automated adapters lands in milestone 12).
4. **Hard filters + deterministic scoring** *(done)* — hard filters
   (`app/ingestion/filters.py`: location, negative keywords, already-processed
   status) and the weighted deterministic scorer (`app/matching/scorer.py`),
   orchestrated by `app/matching/pipeline.py` which persists `MatchAnalysis`
   and advances `Job.status` DISCOVERED → ANALYZED → (if above
   `matching.minimum_score`) QUALIFIED. Wired to `analyze`. Since AI-based
   requirement extraction (`JobRequirements`) doesn't exist until Milestone 5,
   this milestone can only recognize requirements that are either configured
   (`required_or_high_value_skills`, `preferred_skills`) or detectable with
   generic regexes ("N+ years", a degree keyword, a seniority keyword) — a
   skill the job asks for that isn't in your configured lists is invisible to
   scoring today. Two explicit neutral defaults avoid false precision when a
   dimension is genuinely ambiguous: `NO_REQUIREMENT_SCORE` (100, the posting
   states no requirement for this dimension) and `UNKNOWN_FIT_SCORE` (50, we
   can't tell either way, e.g. no search profile to check location against).
5. **AI integration** *(done)* — `AIProvider` interface (`app/ai/base.py`), an OpenAI
   implementation (`app/ai/openai_provider.py`, Chat Completions with a JSON response
   format; retried with backoff on a transient connection/rate-limit error per Milestone
   13's `app/utils/retry.py`) and a free, deterministic `MockAIProvider` (`app/ai/mock.py`,
   also selectable via `ai.provider: mock` for a no-cost/offline mode, not just tests).
   `app/ai/service.py` extracts and persists
   `JobRequirements` per job, with two cache layers so a job is never billed twice: an
   existing `JobRequirements` row for the same job, or one belonging to a different job
   with an identical `description_hash`. Every outcome (including free cache hits) is
   logged as an `AICallLog` row (tokens, estimated cost, `cache_hit`) per spec §19.
   `score_job()` (`app/matching/scorer.py`) takes the extracted `ExtractedJobRequirements`
   as an optional parameter and uses it only to *extend* the deterministic
   skill/years/seniority/remote-type signals from Milestone 4 -- never to override an
   explicit signal already present in the job's own text or `Job.remote_type`. Off by
   default (`ai.enabled: false` in config.yaml) so `analyze` never makes a billed call
   until the user opts in and sets `OPENAI_API_KEY`. `ai.generate_match_explanations`
   (also off by default) optionally replaces the templated explanation with an
   AI-polished rewrite of the same facts, falling back to the deterministic text if that
   call fails. Wired to `analyze`.
6. **Resume tailoring + truthfulness validation** *(done)* — `AIProvider.tailor_resume`
   (both `OpenAIProvider` and `MockAIProvider`) drafts a `TailoredResumeDraft` (summary,
   skill order, per-bullet rewrites each citing a `source_bullet_id`). `app/resume/
   validator.py` is the deterministic guardrail: a bullet is accepted only if its
   `source_bullet_id` matches a real `AccomplishmentBullet` and introduces no
   number/percentage the source bullet doesn't already contain; otherwise it falls back to
   that source bullet's own text verbatim (or is dropped entirely if the id doesn't match
   anything). Skills work the same way -- `tailor_skills` may only reorder/select from the
   candidate's actual profile. `app/resume/tailor.py` persists `TailoredResume` +
   `TailoredBulletProvenance` and renders a DOCX (`app/documents/docx.py`, a fresh
   generated layout rather than editing the base resume's file in place -- python-docx
   can't reliably round-trip arbitrary Word formatting). Address swap
   (`resume.change_address`) only applies when the job has a `search_profile_name`.
   `TailoredResume.validation_status` is `PASSED` only if nothing needed a fallback,
   otherwise `PARTIAL`. Wired to `generate-resume` (via `app/resume/pipeline.py`, shared
   with Milestones 7-8 below).
7. **PDF generation** *(done)* — `LibreOfficePdfConverter` (`app/documents/
   libreoffice.py`) shells out to `soffice --headless --convert-to pdf`. A missing binary
   or failed conversion raises a specific `PdfConversionError` that `app/resume/
   pipeline.py` catches -- a DOCX-only tailored resume is still a usable result, so PDF
   generation degrades gracefully rather than failing `generate-resume`.
8. **Application state machine + file organization** *(done)* — `app/applications/
   state_machine.py` is an explicit allowed-transition map over the review/apply/outcome
   part of `JobStatus` (RESUME_GENERATED -> READY_FOR_REVIEW -> APPROVED -> ... ->
   APPLIED -> INTERVIEW/REJECTED -> ...); SKIPPED/ARCHIVED are reachable from any
   non-terminal status so the user can always back out. Every transition is recorded via
   `JobStatusHistory`, same as the automated pipeline stages. `app/applications/
   bundle.py` writes `job.json`/`job_description.txt`/`match_analysis.json`/`metadata.json`
   under `data/applications/<company>_<title>_<job_id>/` and keeps one
   `ApplicationArtifactSet` row per job (upserted, not duplicated, on repeat calls).
   `app/resume/pipeline.py::generate_resume_bundle` ties Milestones 6-8 together: tailor
   -> PDF -> artifact bundle -> `READY_FOR_REVIEW`, used by both `generate-resume` and the
   scheduler's automated run. Wired to `approve`/`skip`/`open-application`/`mark-applied`/
   `mark-interview`/`mark-offer`/`mark-rejected`/`archive`.
9. **Dashboard** *(done)* — server-rendered FastAPI + Jinja2 + HTMX (`app/ui/`), per the
   frontend-choice decision above: `hx-boost` on `<body>` gives normal links/forms
   AJAX-style partial swaps with no separate partial-template per fragment, degrading to
   plain navigation with JS disabled. `app/services/jobs.py` (list/detail/diff logic) is
   shared with a minimal JSON API (`app/api/routes.py`) so a future non-HTML client needs
   no backend rework. The resume diff view is a server-side word-level diff
   (`app/ui/diff.py`, stdlib `difflib`) rather than a client-side diff library, showing
   accepted vs. fallback bullets with the rejection reason. Wired to `serve`.
10. **Notifications** *(done)* — `WindowsToastNotificationProvider`
    (`app/notifications/windows_toast.py`, via `win10toast`) called from the automated
    run pipeline (not manual `analyze`, which already prints its result to the terminal)
    when a job qualifies and its score meets `notifications.minimum_score`
    (`app/notifications/service.py`). Uses `threaded=False` deliberately: `win10toast`'s
    threaded mode raises inside a background thread that can't be caught, so a blocking
    call (already on the scheduler's own thread, not the web server's) is what makes
    failures actually catchable and loggable rather than crashing silently.
11. **Scheduler** *(done)* — `app/scheduler/run.py::run_once` is one discover -> analyze
    -> notify -> generate-resume pass across every enabled search profile's configured
    sources, isolating each source's failures onto its own `SourceRunResult` row (spec
    §21: one failing source must never hide or abort the others) under one `SchedulerRun`
    row. `app/scheduler/lock.py` is a file-based run-lock (`data/.scheduler.lock`) so an
    overlapping background tick and manual `run` never execute concurrently.
    `app/scheduler/background.py` wires this into an APScheduler `BackgroundScheduler` on
    `scheduler.interval_minutes`, re-reading config.yaml every tick. Wired to `run` (one
    pass) and `serve` (dashboard + this on a timer).
12. **Greenhouse/Lever source adapters** *(done)* — `GreenhouseSource`
    (`app/sources/greenhouse.py`) and `LeverSource` (`app/sources/lever.py`) against their
    public, documented, unauthenticated job-board APIs (`boards-api.greenhouse.io`,
    `api.lever.co`), configured per search profile via `greenhouse_boards`/
    `lever_companies`. HTML posting content is converted to plain text
    (`app/utils/htmlstrip.py`, a minimal tag-stripper, not a full HTML parser).
13. **Hardening pass** *(done)* — `app/utils/retry.py` (exponential backoff, no new
    dependency) wraps `OpenAIProvider`'s three call sites (retrying only
    `APIConnectionError`/`RateLimitError` -- a 4xx/auth failure won't succeed on retry) and
    both source adapters' HTTP requests (`httpx.TransportError`). Per-source failure
    isolation was built directly into Milestone 11's scheduler rather than bolted on
    after. The AI cost-tracking view is the dashboard's `/costs` page
    (`app/services/costs.py`, grouped `AICallLog` totals by operation). This README (see
    root `README.md`) documents every milestone's actual CLI/config surface.
