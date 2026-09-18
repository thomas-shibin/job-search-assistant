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
2. **Resume ingestion** *(done)* — deterministic parsing of `base_resume.docx` into
   `CandidateProfile` (`app/resume/parser.py`), with profile versioning
   (`CandidateProfile.version`/`is_active`) and an unparsed-content safety net
   (`raw_unparsed_sections`) so nothing is silently dropped. Wired to `import-resume`.
3. **Job ingestion** *(done)* — manual-import source (`app/sources/manual.py`),
   deterministic normalization (`app/ingestion/normalize.py`, including remote-type
   inference), fingerprint + near-duplicate-description dedup
   (`app/ingestion/fingerprint.py`), and the orchestration pipeline
   (`app/ingestion/pipeline.py`) that persists `Job` rows with a `JobStatusHistory` audit
   trail and structured `JOB_DISCOVERED`/`JOB_DUPLICATE` log events. Wired to `discover`
   (manual source only; the `JobSource.discover_jobs(search_profile)` path used by
   automated adapters lands in milestone 12).
4. **Hard filters + deterministic scoring** — filters, `MatchingWeights`, `score_job()`,
   full unit coverage.
5. **AI integration** — `AIProvider` interface, OpenAI implementation, mock
   implementation for tests, cached requirement extraction. `analyze`.
6. **Resume tailoring + truthfulness validation** — tailoring pipeline, provenance
   tracking, validator, DOCX generation. `generate-resume`.
7. **PDF generation** — `LibreOfficePdfConverter`.
8. **Application state machine + file organization** — status transitions, audit
   history, `data/applications/...` layout.
9. **Dashboard** — job list with filters/sort, match detail, resume diff view,
   approve/skip/open-application actions.
10. **Notifications** — Windows toast provider wired to the qualification pipeline.
11. **Scheduler** — APScheduler + run-lock + run history, `run`/`serve`.
12. **Greenhouse/Lever source adapters** — second `JobSource` implementation.
13. **Hardening pass** — retry/backoff, per-source failure isolation, cost-tracking UI,
    README finalization.
