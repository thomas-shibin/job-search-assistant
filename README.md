# Local AI Job Application Assistant

> **Installing on another computer?** See [INSTALL.md](INSTALL.md) -- one-command installers
> for Windows (`install.cmd`) and Linux (`./install.sh`), with or without Docker.

A local, human-in-the-loop tool that discovers jobs, scores them against your resume,
generates a truthful tailored resume for strong matches, and lets you review and approve
before anything is submitted. See `docs/ARCHITECTURE.md` for the full design and
milestone roadmap. All 13 milestones are implemented — see "Project status" below for
what each one covers.

## Requirements

- Windows (PowerShell) or Ubuntu/Linux (bash) — the same code runs on both
- Python 3.11+ (the project targets 3.12+ per the long-term design, but runs on 3.11;
  tested on 3.14 on Ubuntu)
- [LibreOffice](https://www.libreoffice.org/download/download/), specifically the
  `soffice` binary — used for DOCX -> PDF conversion of tailored resumes. On Windows it is
  also found in its default `Program Files` install folder; on Ubuntu `sudo apt install
  libreoffice` puts it on `PATH`. Optional: `generate-resume` still produces a usable DOCX
  without it, just no PDF.
- Desktop notifications: nothing extra on Windows (`win10toast` installs automatically);
  on Ubuntu, `notify-send` (package `libnotify-bin`, preinstalled on the desktop edition).

## Setup (PowerShell)

```powershell
# From the project root
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"

# Create local data folders and a starter config.yaml
python cli.py init

# Copy environment template and fill in your OpenAI API key
Copy-Item .env.example .env
notepad .env

# Edit config\config.yaml with your real candidate info, search profiles, and the
# path to your base resume (place the actual .docx under data\resumes\).
notepad config\config.yaml

# Create the local SQLite database
alembic upgrade head

# Confirm everything is wired up correctly
python cli.py validate-config

# Parse your base resume into the structured candidate profile
python cli.py import-resume data\resumes\base_resume.docx
```

## Setup (Ubuntu / Linux)

```bash
# From the project root (Ubuntu: sudo apt install python3-venv libreoffice libnotify-bin)
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

python cli.py init
cp .env.example .env && nano .env          # fill in your OpenAI API key
nano config/config.yaml                    # candidate info, search profiles, resume path
alembic upgrade head
python cli.py validate-config
python cli.py import-resume data/resumes/base_resume.docx
```

Every command below works the same on both platforms — only the venv activation line and
path separators differ (`.\.venv\Scripts\Activate.ps1` vs `source .venv/bin/activate`).

## Running with Docker (Windows or Linux)

```bash
docker compose up --build -d      # dashboard on http://127.0.0.1:8001
docker compose logs -f app
docker compose exec app python cli.py validate-config   # any CLI command, inside the container
docker compose down
```

The container bind-mounts `data/` and `config/`, so it shares the database, resumes and
settings with a native install, and applies migrations on every start. It reads `.env`
via `env_file` — Compose expands `$` in `.env` values, so wrap any value containing `$` in
single quotes (`KEY='pa$word'`). Desktop notifications don't reach the host from a container
(they're skipped with a log warning); `open-application` needs a browser, so run that one natively.

## Importing your base resume (Milestone 2)

`import-resume` parses a `.docx` or `.pdf` resume deterministically (no AI calls) into
structured data: employment history with accomplishment bullets, skills, education, and
certifications. This becomes the ground-truth `CandidateProfile` that every later
AI-tailored resume must trace back to — see "Truthfulness guardrail" in
`docs/ARCHITECTURE.md`.

It expects reasonably standard resume conventions to get full structured extraction:
- Section headings (Summary/Experience/Skills/Education/Certifications) as their own
  paragraph, ideally using a Word "Heading" style, or a short bold/all-caps line
  (for PDFs, which carry no style metadata, ALL-CAPS or an exact section-name match
  is the only heading signal available).
- Each job entry either as one line containing a date range plus company (e.g.
  `Senior Engineer, Acme Corp | Jan 2021 - Present`), or as a "Title, Dates" line
  immediately followed by a "Company — Location" line (a common layout produced by
  resume builders/PDF exports) — accomplishment bullets follow as separate bullet
  lines beneath either form. Numeric `MM/YYYY` dates and the fullwidth dash (－)
  some PDF exporters substitute for "-" are both recognized.
- Skills as a comma/pipe/bullet-separated list. PDF bullet glyphs exported from Word
  (which often land in the Unicode Private Use Area, e.g. a Wingdings bullet) are
  recognized as bullets/separators, not literal text.
- Bullet text that wraps onto a second PDF line with no bullet marker is merged back
  into the previous bullet rather than becoming a spurious extra one.

Nothing is ever silently dropped: any content the parser can't confidently classify
(unusual sections, a name/contact header block, an unparseable job entry) is preserved
verbatim in `CandidateProfile.raw_unparsed_sections`. If `import-resume` prints a warning
about unparsed content, check that field before relying on the structured profile — it
means something in your resume wasn't captured as structured data and won't be considered
during matching/tailoring until you either restructure that part of the resume or (in a
later milestone) it's manually reconciled.

Re-running `import-resume` with an unchanged file is a no-op. A changed file creates a new
version (previous versions are kept, not deleted) — pass `--force` to create a new version
even without a content change.

## Importing a job (Milestone 3)

`discover` currently supports manual import only: you paste a job URL and/or its
description, and it runs normalization + duplicate detection and stores it as a `Job` row
with status `DISCOVERED`. This is the always-available, zero-risk path — safe for LinkedIn,
Indeed, or any other posting, since it never scripts interaction with those platforms; you
just copy the posting text yourself. Automated discovery via search profiles (Greenhouse/
Lever adapters) is a later milestone.

```powershell
# Paste text directly
python cli.py discover --description "Company: Acme Corp`nTitle: Senior Backend Engineer`n..." --url https://example.com/jobs/123

# Or save the posting to a text file first (recommended for anything long)
notepad job.txt
python cli.py discover --description-file job.txt --url https://example.com/jobs/123

# Override company/title/location if they weren't guessed correctly
python cli.py discover --description-file job.txt --company "Acme Corp" --title "Senior Backend Engineer" --location "Vancouver, BC"
```

Company/title/location are extracted from `Company:`/`Title:`/`Location:` lines or a
"`<Title> at <Company>`" first line when not passed explicitly; if they can't be guessed
they default to "Unknown Company"/"Unknown Title" and the CLI tells you to pass them
explicitly. Duplicate detection checks (in order) same source+external ID, matching
company+title+location fingerprint, exact description match, and near-duplicate
description similarity — re-importing the same posting (or a lightly reworded repost)
reports "Duplicate of existing job" instead of creating a second row.

## Analyzing jobs (Milestones 4-5)

`analyze` runs hard filters, then (optionally) AI-based requirement extraction, then
deterministic scoring, against every job it's given.

```powershell
python cli.py analyze 1        # analyze one job by id
python cli.py analyze --all    # analyze every job currently in DISCOVERED status
```

Hard filters run first (location vs. the job's search profile, if any; negative keywords;
already-processed status) — a filtered job never gets scored and stays at its current
status, with the reasons printed and logged as `JOB_FILTERED`. A job that passes gets a
persisted `MatchAnalysis` (overall + eight per-dimension scores, strong/partial/missing
skill lists, an explanation) and moves to `ANALYZED`, then to `QUALIFIED` if its
`overall_score` meets `matching.minimum_score`.

Two dimensions use an explicit neutral default instead of a guessed score when data is
genuinely ambiguous, called out by name in the explanation text: `NO_REQUIREMENT_SCORE`
(100 — the posting states no requirement for that dimension, e.g. no years-of-experience
number) and `UNKNOWN_FIT_SCORE` (50 — insufficient signal either way, e.g. no search
profile to compare location against for a manually imported job).

### AI-based requirement extraction (Milestone 5)

Scoring alone (Milestone 4) can only recognize requirements that are either configured
under `matching.required_or_high_value_skills`/`matching.preferred_skills` or detectable
with generic regexes ("N+ years", a degree keyword) — a skill the job asks for that isn't
in your configured lists is otherwise invisible to scoring. Setting `ai.enabled: true` in
`config.yaml` closes that gap: before scoring, `analyze` sends the job description to the
configured AI provider once and caches the result (see below), and the extracted skills,
years-of-experience, seniority, and remote type only *extend* what the deterministic
scorer already detects — they never override an explicit signal already found in the
job's own text (e.g. an explicit "5+ years" in the posting always wins over an extracted
number).

`ai.enabled` defaults to `false` so `analyze` never makes a billed call until you opt in.
Two providers are supported via `ai.provider`:

- `openai` (default provider name once enabled): requires `OPENAI_API_KEY` in your `.env`
  (see `.env.example`); model is set via `ai.model` (default `gpt-4o-mini`).
- `mock`: a free, local, deterministic heuristic (`app/ai/mock.py`) that recognizes
  "Requirements:"/"Preferred:"-style bulleted sections — no API key needed, lower
  recall than a real model, useful for trying the pipeline or for tests.

Caching (spec §19 cost control): a job is never sent to the AI provider twice — if it
already has extracted requirements (e.g. from a prior `analyze` run), or another job with
the exact same description text does, those are reused with no new call. Every outcome,
including free cache hits, is recorded as an `AICallLog` row (tokens, estimated cost,
`cache_hit`) for cost visibility.

`ai.generate_match_explanations` (also off by default) optionally replaces the
deterministic, templated explanation with an AI-polished 1-2 sentence rewrite of the same
facts (never new claims); if that call fails, the deterministic explanation is kept rather
than failing the analysis.

## Generating a tailored resume (Milestones 6-8)

```powershell
python cli.py generate-resume 1
```

This runs the full chain for job id `1`: AI tailoring (summary rewrite, skill
reordering, bullet rephrasing) -> a deterministic truthfulness check -> a DOCX
(and PDF, if LibreOffice is on `PATH`) -> an artifact bundle under
`data\applications\<company>_<title>_<job_id>\` -> the job moves to `READY_FOR_REVIEW`.

The DOCX is **your own base resume with only the wording changed**: the file is copied
and only the summary and experience bullets are rewritten (and the location line, when a
job is elsewhere -- see the address rules below), so layout, fonts, tables, colours and
spacing stay exactly as you designed them. The skills section is left as written. Only a
PDF base resume gets a plain generated layout instead, since there is no Word design to
keep. Changed your resume's design? `python cli.py rebuild-resumes` re-creates every
tailored resume in the new design from the wording already saved (no AI calls).

Rewording needs AI: with `ai.enabled: false` (Settings → AI) the built-in stand-in keeps
every bullet word for word, so a "tailored" resume is your original.

**Truthfulness guardrail** (spec §8, `app/resume/validator.py`): every AI-rewritten bullet
must cite the `AccomplishmentBullet` id it came from, and its text may not introduce any
number/percentage the source bullet doesn't already contain. A bullet that cites an
unknown id is dropped; a bullet that invents a number falls back to that source bullet's
*original* text verbatim — the achievement is real, only the AI's specific phrasing is
rejected. Skills work the same way: `tailor_skills` may only reorder/select from your
actual profile, never invent one. `TailoredResume.validation_status` is `PASSED` only if
nothing needed a fallback; otherwise `PARTIAL` (still usable — check the dashboard's diff
view to see what was kept verbatim and why).

Address (`resume.change_address`, address profiles in Settings): the location line in your
resume's header is swapped for the job. Add a complete address -- street, unit/building and
postal code are optional -- and the resume shows it in full; without a street it shows the
city and province in the shape your resume already uses ("Victoria, Canada" -> "Toronto,
Canada"). Which address is used, first match wins: the address of the search profile that
found the job; the address profile in the job's province (as the posting states it --
"Location: Vancouver" -- else as listed); your only address profile, if you have exactly one;
with no address profiles at all, the job's own city and province. Otherwise (e.g. a remote
job) your resume's own line stays. After changing addresses, run `python cli.py
rebuild-resumes` to update resumes you've already generated.

Once a resume is generated, move the job through its lifecycle with:

```powershell
python cli.py approve 1
python cli.py open-application 1    # opens the job's source URL in your browser, marks APPLICATION_OPENED
python cli.py mark-applied 1
python cli.py mark-interview 1      # or mark-rejected / mark-offer
python cli.py archive 1
python cli.py skip 1                # available from any non-terminal status
```

Every transition is recorded in `JobStatusHistory` (same audit trail the automated
pipeline stages use) and only the allowed next steps succeed — e.g. you can't
`mark-applied` before `approve` + `open-application` (`app/applications/state_machine.py`).

## Dashboard (Milestone 9)

```powershell
python cli.py serve
```

Opens a server-rendered dashboard at `http://127.0.0.1:8000` (FastAPI + Jinja2 + HTMX, no
separate frontend build): a job list with status/score filters, a per-job detail page with
the full score breakdown and a word-level diff of every tailored bullet against its base
resume source (accepted vs. fallback, with the rejection reason shown), and buttons for
every lifecycle action above. A minimal JSON API backed by the same service layer lives
under `/api/jobs` for anything else that wants to talk to this data later.

### Finding jobs (landing page)

The landing page opens with your **default job search** (Settings → Job search) already
filled in and run. Job titles, locations and keywords are picked from searchable
dropdowns: type to filter, press Enter to pick, or add anything that isn't listed.
One Adzuna search runs per title × location, and the merged results are shown
newest-first as cards. Keywords narrow every search (a job must mention at least one) at
no extra cost. An empty box means "any", so empty titles and locations search every job
in the country. "Remote" finds remote jobs anywhere in the country. Edit the boxes for a
one-off search, or click **Save as default**.

The dropdown options come from the **search catalog** (`search_terms` table): a built-in
list of common titles, skills and Canadian provinces and cities, plus whatever a **market
scan** finds. Run a scan from Settings → Search catalog → Refresh catalog, or with
`python cli.py catalog-scan`. It costs about 50 Adzuna requests and adds every province,
region and city with its job count, recurring job titles, employers, and how often each
keyword appears. Locations a scan has mapped are then searched by their exact place
(Canada › British Columbia › Capital › Victoria) instead of free text. Anything you type
into a search, setting or watch is added to the catalog too.

Each card has **Open job** (the posting) and **Analyze with AI**. Analyze saves the
job, scores it with the free keyword scorer, and sends the job description plus your
imported resume to the configured AI provider for a fit assessment: a 0-100 score, a
verdict, what you match, the gaps, and a recommendation. This runs whenever you click,
independent of `ai.enabled`, but each job is assessed once per resume version unless you
click **Re-run analysis**. Calls show on the AI Costs page (about $0.0005 each with
gpt-4o-mini). Tailoring a resume stays a separate step on the job's page.

Adzuna's free tier allows about 250 requests a day. Dashboard searches are cached for
10 minutes. Scheduled scans use at most 60% of `search.adzuna_daily_limit` and rotate
through the title × location combinations when there are more than fit in one scan; the
Settings page shows how often each combination is checked.

## Notifications (Milestone 10)

When a job qualifies during an automated `run`/`serve` pass (not a manual `analyze`, which
already prints its result to the terminal) and its score meets
`notifications.minimum_score` (a stricter bar than `matching.minimum_score` is allowed —
being trackable isn't the same as being worth an interruption), a desktop notification is
shown — a Windows toast via `win10toast` on Windows, `notify-send` everywhere else
(`app/scheduler/run.py` picks by platform). A failed notification (no OS notification service available,
etc.) is logged and never breaks the run that triggered it.

### Telegram watchlist alerts

The **Watchlist** page holds saved searches that message you on Telegram the moment a
`serve` (or `run`) scan finds a new matching job. A watch can be as lean as a couple of
locations or keywords, picked from the same dropdowns as the job search:

- **Locations / Job titles / Keywords** -- within a box any entry matches; every box you
  fill in must match. Locations match by city ("Victoria, BC" matches Adzuna's "Victoria,
  Capital") or province ("Ontario").
- **Alert when match score is at least** -- e.g. 80: only jobs your resume scores 80+ on.
  Leave it empty to hear about every match.
- **Search for these jobs on every scan** (on by default) -- the watch runs its own Adzuna
  search, so it finds jobs your main job search doesn't cover. Its searches share the
  daily Adzuna budget with the job search (the Settings page shows how often each runs).
- **More filters** -- only/never these companies, skip jobs mentioning words, salary
  range (jobs without a listed salary still match), job type (full-time, part-time,
  permanent, contract) and how recently it was posted.

Jobs already seen in earlier scans are never re-alerted. To set up Telegram:

1. Create a bot with [@BotFather](https://t.me/BotFather) and put its token in `.env` as
   `TELEGRAM_BOT_TOKEN` (the only Telegram value kept in `.env`).
2. Open your bot in Telegram and press **Start** (or send it any message).
3. On the Watchlist page, click **Detect chat ID**, then **Send test message**.
4. Tick **Enabled**, save, and add your watches.

A watch with only companies has nothing to search Adzuna by; it checks the jobs your other
searches find (add the company's Greenhouse/Lever board under Settings → Search profiles to
cover it directly). If one scan finds more than *Max alerts per run* matches, the rest
arrive as a single summary message.

## Settings (dashboard)

All settings (candidate, search profiles and job sources, matching, AI, resume, notifications,
watchlist, scan interval) live in the database and are edited on the dashboard's
**Settings** and **Watchlist** pages. Changes apply immediately, including the scan
interval, with no restart. The Settings page also shows whether each `.env` secret is set,
when the next scan is due, and has a **Scan now** button.

`config/config.yaml` is only an import source: the first time the app loads settings and
the database has none, it copies the file in. Later edits to the file are ignored unless you
re-import it with `python cli.py import-config --force` (add `--keep-watches` to leave your
watches alone). `.env` holds only secrets: `OPENAI_API_KEY`, `ADZUNA_APP_ID`/`ADZUNA_APP_KEY`,
`TELEGRAM_BOT_TOKEN` and `DATABASE_URL`.

The database is SQLite (`data/job_assistant.db`) by default, with nothing to install. To use
PostgreSQL instead, run `pip install -e ".[postgres]"`, set
`DATABASE_URL=postgresql+psycopg://user:pass@host:5432/jobs` and run `alembic upgrade head`.

## Automated discovery + the scheduler (Milestones 11-12)

Beyond manual `discover`, `run`/`serve` can poll public, documented job-board APIs
directly:

- **Adzuna** (`api.adzuna.com`, a job aggregator covering many employers per query). It uses
  the global job search (Settings → Job search: titles × locations, country) whenever
  `search.scan_in_background` is on. It needs a free `ADZUNA_APP_ID`/`ADZUNA_APP_KEY` in
  `.env` (https://developer.adzuna.com/). Without them, Adzuna is skipped with a warning.
- **Greenhouse** (`boards-api.greenhouse.io`) and **Lever** (`api.lever.co`): specific
  companies' own job boards, listed per search profile (Settings → Search profiles) as
  `greenhouse_boards` (from boards.greenhouse.io/<token>) and `lever_companies` (from
  jobs.lever.co/<company>).

```powershell
python cli.py run      # one discover -> analyze -> notify -> generate-resume pass, then exit
python cli.py serve    # dashboard + this same pass on a scheduler.interval_minutes timer
```

Per spec's job-source risk assessment (see `docs/ARCHITECTURE.md`), these are the only
automated sources: all three are public APIs (two run by companies to power their own
careers pages, one a dedicated aggregator), not scraping. LinkedIn/Indeed are deliberately
never scripted — they only ever appear as `source_url`s opened manually via
`open-application`.

A failing source never aborts the run for the others (`SourceRunResult` records
per-source discovered/new/duplicate/qualified counts and any error, all under one
`SchedulerRun` row), and a file-based run-lock (`data\.scheduler.lock`) prevents two runs
from overlapping. AI/network calls (OpenAI, Greenhouse, Lever) automatically retry once
with backoff on a transient connection error/rate limit before giving up.

## Running tests

```powershell
.\.venv\Scripts\Activate.ps1      # Ubuntu: source .venv/bin/activate
pytest
```

## Command reference

Every command, with all its options. Run them from the project root with the virtual
environment active (`source .venv/bin/activate`; Windows: `.\.venv\Scripts\Activate.ps1`).
Using Docker? Put `docker compose exec app` in front of any `python cli.py ...` command to
run it inside the container (e.g. `docker compose exec app python cli.py rebuild-resumes`).
Once installed with `pip install -e .`, `jobassistant <command>` works too.

`--config PATH` (on the commands that take it) is only used the very first time, to import
a `config.yaml` when the database has no settings yet -- settings live in the database and
are edited on the dashboard's Settings page.

### Docker

```bash
docker compose up --build -d          # build + start (dashboard on http://127.0.0.1:8001)
docker compose up -d                  # start without rebuilding
docker compose logs -f app            # follow the logs
docker compose ps                     # status / health
docker compose restart app            # restart
docker compose down                   # stop and remove the container (data/ and config/ stay)
docker compose exec app python cli.py <command>   # run any command below inside the container
```

### Setup and settings

```bash
python cli.py init                                  # create data folders + a starter config/config.yaml
python cli.py validate-config [--config PATH]       # check the settings stored in the database
python cli.py import-config [PATH]                  # import a config.yaml into the database (first run only)
python cli.py import-config [PATH] --force          # overwrite the database settings from the file
python cli.py import-config [PATH] --force --keep-watches   # ...but leave watchlist entries alone
alembic upgrade head                                # apply database migrations (Docker does this on start)
```

### Your resume

```bash
python cli.py import-resume data/resumes/base_resume.docx           # parse your base resume (.docx or .pdf)
python cli.py import-resume data/resumes/base_resume.docx --force   # re-import even if unchanged
python cli.py rebuild-resumes                       # re-create every tailored resume in your resume's current design (no AI calls)
python cli.py rebuild-resumes --job JOB_ID          # just one job
python cli.py rebuild-resumes --no-pdf              # DOCX only, skip the PDF (no LibreOffice needed)
```

### Jobs: import, score, tailor

```bash
python cli.py discover --description-file job.txt   # import one job from a text file
python cli.py discover --description "..." --url URL --company NAME --title TITLE --location PLACE
python cli.py analyze JOB_ID [--config PATH]        # hard filters + (optional AI extraction) + scoring
python cli.py analyze --all                         # every job still in DISCOVERED
python cli.py generate-resume JOB_ID [--config PATH]   # tailor + validate + DOCX/PDF + bundle -> READY_FOR_REVIEW
```

### Application status

```bash
python cli.py approve JOB_ID            # approve a reviewed tailored resume
python cli.py open-application JOB_ID   # open the posting in your browser, mark APPLICATION_OPENED (run natively, not in Docker)
python cli.py mark-applied JOB_ID
python cli.py mark-interview JOB_ID
python cli.py mark-offer JOB_ID
python cli.py mark-rejected JOB_ID
python cli.py archive JOB_ID            # done, no further action
python cli.py skip JOB_ID               # stop pursuing (from any non-final status)
```

### Dashboard, scans and alerts

```bash
python cli.py serve                     # dashboard on http://127.0.0.1:8000 + the background scheduler
python cli.py serve --host 0.0.0.0 --port 8000      # listen on another address/port
python cli.py run [--config PATH]       # one discover -> analyze -> notify -> tailor pass, right now
python cli.py telegram-setup            # detect + save your Telegram chat id (message your bot first)
python cli.py telegram-test             # send a test Telegram message
python cli.py catalog-scan              # refresh the search dropdowns from the job market (~50 Adzuna requests)
python cli.py catalog-scan --country ca # ...for another Adzuna country (default: Settings -> Job search)
```

### Development

```bash
python -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"   # first-time install
pytest                                  # run the tests
pytest tests/unit/test_docx_edit.py -q  # one test file
alembic revision --autogenerate -m "describe the change"   # new migration after a model change
python cli.py <command> --help          # full help for any command
```

## What data leaves your machine

Per the project's security principles, only the minimum needed is ever sent to the
configured AI provider (OpenAI, when `ai.provider: openai`; nothing leaves your machine
at all with `ai.provider: mock`), and only for these operations:

- **Job requirement extraction** (Milestone 5, opt-in via `ai.enabled`): the raw text of
  one job description — nothing else, and never a job whose extraction is already cached.
- **Resume tailoring** (Milestone 6, every `generate-resume`): the job description, its
  extracted requirements, and the structured fields of your candidate profile relevant to
  that job (summary, skills, employment history with accomplishment bullets) — never the
  raw `.docx`/`.pdf` file itself, and never other jobs' data.
- **Automated discovery** (Milestone 12, opt-in via `greenhouse_boards`/`lever_companies`):
  requests only go to the public job-board APIs you've explicitly configured a
  token/company slug for.
- **Telegram watchlist alerts** (opt-in via `watchlist.enabled`): the title, company,
  location, score and posting link of each matching job, sent to your own bot's chat.

Nothing else (API keys, other candidates' data, application history) is transmitted.
API keys live only in your local `.env` file, which is git-ignored and never logged.

## Project status

All 13 milestones are done:
- **Milestone 1**: config loading/validation, the database schema and migrations,
  structured logging, and the CLI/API skeleton.
- **Milestone 2**: deterministic base-resume parsing (`app/resume/parser.py`) into the
  structured `CandidateProfile`, wired to `import-resume`, with profile versioning and a
  "never silently drop content" guarantee (see above).
- **Milestone 3**: manual job import (`app/sources/manual.py`), deterministic
  normalization (`app/ingestion/normalize.py`), duplicate detection
  (`app/ingestion/fingerprint.py`), and the ingestion pipeline
  (`app/ingestion/pipeline.py`) that persists `Job` rows with a `JobStatusHistory` audit
  trail, wired to `discover`.
- **Milestone 4**: hard filters (`app/ingestion/filters.py`) and deterministic, weighted
  scoring (`app/matching/scorer.py`), orchestrated by `app/matching/pipeline.py` and wired
  to `analyze`.
- **Milestone 5**: the `AIProvider` interface (`app/ai/base.py`) with an OpenAI
  implementation (`app/ai/openai_provider.py`) and a free deterministic mock
  (`app/ai/mock.py`); cached job-requirement extraction (`app/ai/service.py`) and
  cost-tracked `AICallLog` rows; the scorer optionally consumes extracted requirements to
  recognize skills/years/seniority/remote-type beyond what's configured or
  regex-detectable, without ever overriding an explicit signal already in the job text.
  Off by default (`ai.enabled: false`) — see "AI-based requirement extraction" above.
- **Milestone 6**: AI resume tailoring (`OpenAIProvider.tailor_resume`/
  `MockAIProvider.tailor_resume`) plus the deterministic truthfulness validator
  (`app/resume/validator.py`) and orchestration (`app/resume/tailor.py`) that persists
  `TailoredResume`/`TailoredBulletProvenance` and renders a DOCX
  (`app/documents/docx.py`). Wired to `generate-resume`.
- **Milestone 7**: `LibreOfficePdfConverter` (`app/documents/libreoffice.py`), DOCX -> PDF
  via headless LibreOffice; a missing/failed conversion degrades to DOCX-only rather than
  failing `generate-resume`.
- **Milestone 8**: the review/apply/outcome state machine (`app/applications/
  state_machine.py`, an explicit allowed-transition map over `JobStatus`) and the artifact
  bundle writer (`app/applications/bundle.py`) that organizes `data\applications\...` and
  keeps one `ApplicationArtifactSet` row per job. Wired to `approve`/`skip`/
  `open-application`/`mark-applied`/etc.
- **Milestone 9**: the FastAPI + Jinja2 + HTMX dashboard (`app/ui/`, `app/services/jobs.py`,
  a minimal JSON API at `app/api/routes.py` backed by the same service layer). Wired to
  `serve`.
- **Milestone 10**: desktop notifications — Windows toast (`app/notifications/windows_toast.py`)
  or `notify-send` on Linux (`app/notifications/linux_notify.py`) —
  gated by `notifications.minimum_score`, wired into the automated `run`/`serve` pipeline.
- **Milestone 11**: the scheduler (`app/scheduler/run.py`, `app/scheduler/background.py`,
  APScheduler-driven), a file-based run-lock (`app/scheduler/lock.py`), and
  `SchedulerRun`/`SourceRunResult` history. Wired to `run`/`serve`.
- **Milestone 12**: Greenhouse (`app/sources/greenhouse.py`) and Lever
  (`app/sources/lever.py`) adapters against their public, documented, unauthenticated
  job-board APIs, configured per search profile.
- **Milestone 13**: exponential-backoff retry (`app/utils/retry.py`) for AI calls and
  source-adapter HTTP requests, per-source failure isolation in the scheduler (one
  failing source never hides or aborts the others), and the AI cost-tracking dashboard
  page (`/costs`, `app/services/costs.py`).
