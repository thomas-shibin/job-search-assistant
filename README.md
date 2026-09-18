# Local AI Job Application Assistant

A local, human-in-the-loop tool that discovers jobs, scores them against your resume,
generates a truthful tailored resume for strong matches, and lets you review and approve
before anything is submitted. See `docs/ARCHITECTURE.md` for the full design and
milestone roadmap. This README covers Milestones 1-3 (project scaffold, base resume
ingestion, manual job import/dedup) — most CLI commands still print "not yet implemented"
until their milestone lands.

## Requirements

- Windows with PowerShell
- Python 3.11+ (the project targets 3.12+ per the long-term design, but runs on 3.11)
- [Git for Windows](https://git-scm.com/download/win) (not required to run the app, but
  needed to commit — it wasn't detected on this machine during scaffolding)
- [LibreOffice](https://www.libreoffice.org/download/download/) (for DOCX -> PDF
  conversion, added in Milestone 7 — not needed yet)

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

## Importing your base resume (Milestone 2)

`import-resume` parses a `.docx` resume deterministically (no AI calls) into structured
data: employment history with accomplishment bullets, skills, education, and
certifications. This becomes the ground-truth `CandidateProfile` that every later
AI-tailored resume must trace back to — see "Truthfulness guardrail" in
`docs/ARCHITECTURE.md`.

It expects reasonably standard resume conventions to get full structured extraction:
- Section headings (Summary/Experience/Skills/Education/Certifications) as their own
  paragraph, ideally using a Word "Heading" style, or a short bold/all-caps line.
- Each job entry as its own line containing a date range (e.g. `Jan 2021 - Present`,
  `2015-2018`), with accomplishment bullets as separate bullet-list paragraphs beneath it.
- Skills as a comma/pipe/bullet-separated list.

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

## Running tests

```powershell
.\.venv\Scripts\Activate.ps1
pytest
```

## CLI

```powershell
python cli.py init              # create data dirs + starter config.yaml
python cli.py validate-config   # load and validate config.yaml
python cli.py import-resume PATH [--force]  # parse base resume into CandidateProfile
python cli.py discover --description-file job.txt [--url ...] [--company ...] [--title ...] [--location ...]
python cli.py analyze           # Milestone 5
python cli.py generate-resume   # Milestone 6
python cli.py run               # Milestone 11
python cli.py serve             # Milestone 11
```

Once installed via `pip install -e .`, the same commands are also available as
`jobassistant <command>` (e.g. `jobassistant validate-config`).

## What data leaves your machine

Per the project's security principles, only the minimum needed is ever sent to the
configured AI provider (OpenAI), and only for two operations:

- **Job requirement extraction**: the raw text of one job description.
- **Resume tailoring**: the job description, its extracted requirements, and the
  structured fields of your candidate profile relevant to that job (summary, matched
  skills, employment history with accomplishment bullets) — never the raw `.docx` file
  itself, and never other jobs' data.

Nothing else (API keys, other candidates' data, application history) is transmitted.
API keys live only in your local `.env` file, which is git-ignored and never logged.

## Project status

Milestones 1-3 are done:
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

Everything else — automated job-source adapters, hard filters/scoring, resume tailoring,
the dashboard, notifications, and scheduling — is represented as typed interfaces in
`app/*/base.py` (and `app/matching/scorer.py`, `app/resume/validator.py`) with
`NotImplementedError` bodies, to be filled in milestone-by-milestone per
`docs/ARCHITECTURE.md`.
