from __future__ import annotations

import shutil
from pathlib import Path

import typer

from app.config.loader import ConfigError, validate_config
from app.utils.logging import configure_logging

app = typer.Typer(help="Local AI Job Application Assistant CLI")

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"
EXAMPLE_CONFIG_PATH = PROJECT_ROOT / "config" / "config.example.yaml"

_CONFIG_HELP = "config.yaml to import if the database has no settings yet (settings live in the DB)"


def _print_config_errors(errors: list[str]) -> None:
    typer.echo("Configuration is INVALID:", err=True)
    for err in errors:
        typer.echo(f"  - {err}", err=True)


def _load_settings(config_path: Path):
    """Effective settings from the database (importing `config_path` on first use)."""
    from app.config.store import load_app_config
    from app.database.session import SessionLocal

    session = SessionLocal()
    try:
        return load_app_config(session, config_path)
    except ConfigError as exc:
        _print_config_errors(exc.errors)
        raise typer.Exit(code=1)
    finally:
        session.close()


@app.command()
def init() -> None:
    """Create local data directories and a starter config.yaml."""
    configure_logging()

    for sub in ("resumes", "applications"):
        (PROJECT_ROOT / "data" / sub).mkdir(parents=True, exist_ok=True)

    if not DEFAULT_CONFIG_PATH.exists():
        if not EXAMPLE_CONFIG_PATH.exists():
            typer.echo("config.example.yaml not found; cannot create config.yaml", err=True)
            raise typer.Exit(code=1)
        shutil.copy(EXAMPLE_CONFIG_PATH, DEFAULT_CONFIG_PATH)
        typer.echo(f"Created {DEFAULT_CONFIG_PATH} from config.example.yaml")
    else:
        typer.echo(f"{DEFAULT_CONFIG_PATH} already exists, leaving it as-is")

    typer.echo("\nNext steps:")
    typer.echo("  1. Run 'alembic upgrade head' to create the local database")
    typer.echo("  2. Run 'python cli.py serve' and open http://127.0.0.1:8000")
    typer.echo("  3. Upload your resume on the 'My resume' page; adjust everything else under Settings")


@app.command("validate-config")
def validate_config_cmd(
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help=_CONFIG_HELP),
) -> None:
    """Validate the settings stored in the database, reporting any errors."""
    configure_logging()

    config = _load_settings(config_path)

    errors = validate_config(config, base_dir=PROJECT_ROOT)
    if errors:
        _print_config_errors(errors)
        raise typer.Exit(code=1)

    typer.echo("Configuration is OK.")


@app.command("import-config")
def import_config_cmd(
    config_path: Path = typer.Argument(DEFAULT_CONFIG_PATH, help="YAML file to import"),
    force: bool = typer.Option(False, "--force", help="Overwrite settings already in the database"),
    keep_watches: bool = typer.Option(False, "--keep-watches", help="Leave existing watchlist entries alone"),
) -> None:
    """Import a config.yaml into the database. Settings are edited in the dashboard
    afterwards; this happens automatically on first start, so it's only needed to
    deliberately reset settings from a file (--force).
    """
    configure_logging()

    from app.config.store import has_settings, import_yaml_config
    from app.database.session import SessionLocal

    session = SessionLocal()
    try:
        if has_settings(session) and not force:
            typer.echo("Settings already exist in the database. Pass --force to overwrite them from the file.", err=True)
            raise typer.Exit(code=1)
        import_yaml_config(session, config_path, replace_watches=not keep_watches)
    except ConfigError as exc:
        _print_config_errors(exc.errors)
        raise typer.Exit(code=1)
    finally:
        session.close()
    typer.echo(f"Imported {config_path} into the database.")


def _not_implemented(command: str, milestone: int) -> None:
    typer.echo(f"'{command}' is not yet implemented (planned for Milestone {milestone}). See docs/ARCHITECTURE.md.")


@app.command("import-resume")
def import_resume_cmd(
    resume_path: Path,
    force: bool = typer.Option(False, "--force", help="Re-import even if content is unchanged"),
) -> None:
    """Parse a base resume (.docx or .pdf) into the structured candidate profile."""
    configure_logging()

    if not resume_path.exists():
        typer.echo(f"Resume file not found: {resume_path}", err=True)
        raise typer.Exit(code=1)

    from app.database.session import SessionLocal
    from app.resume.parser import import_resume as run_import_resume

    session = SessionLocal()
    try:
        result = run_import_resume(session, resume_path, force=force)
    finally:
        session.close()

    if not result.created:
        typer.echo(
            f"No changes detected since version {result.profile.version}; skipping import. "
            "Use --force to re-import anyway."
        )
        return

    typer.echo(f"Imported resume as CandidateProfile version {result.profile.version} (id={result.profile.id})")
    typer.echo(f"  Employment entries:   {result.counts['employments']}")
    typer.echo(f"  Accomplishment bullets: {result.counts['bullets']}")
    typer.echo(f"  Skills:               {result.counts['skills']}")
    typer.echo(f"  Education entries:    {result.counts['education']}")
    typer.echo(f"  Certifications:       {result.counts['certifications']}")
    typer.echo(f"  Projects:             {result.counts['projects']}")

    if result.profile.raw_unparsed_sections:
        typer.echo(
            "\nWARNING: some resume content could not be confidently classified into "
            "structured fields and was preserved as raw text for your review "
            "(CandidateProfile.raw_unparsed_sections). Review it to confirm nothing "
            "important was missed, and consider adjusting the resume's section headings "
            "or layout if you want it captured in structured form."
        )


@app.command()
def discover(
    url: str = typer.Option(None, "--url", help="Job posting URL (optional)"),
    description: str = typer.Option(None, "--description", help="Raw job description text"),
    description_file: Path = typer.Option(
        None, "--description-file", help="Path to a text file containing the job description"
    ),
    company: str = typer.Option(None, "--company", help="Override/supply the company name"),
    title: str = typer.Option(None, "--title", help="Override/supply the job title"),
    location: str = typer.Option(None, "--location", help="Override/supply the job location"),
) -> None:
    """Manually import one job posting (paste a URL and/or description).

    Automated discovery across configured search profiles (Greenhouse/Lever
    adapters, driven by config.yaml's search_profiles) is planned for a later
    milestone -- see docs/ARCHITECTURE.md.
    """
    configure_logging()

    if description_file is not None:
        if not description_file.exists():
            typer.echo(f"Description file not found: {description_file}", err=True)
            raise typer.Exit(code=1)
        description = description_file.read_text(encoding="utf-8-sig")

    if not description or not description.strip():
        typer.echo("Provide --description or --description-file with the job posting text.", err=True)
        raise typer.Exit(code=1)

    from app.database.session import SessionLocal
    from app.ingestion.pipeline import ingest_raw_job
    from app.sources.manual import import_manual_job

    raw_job = import_manual_job(
        description=description, url=url, company=company, title=title, location=location
    )

    session = SessionLocal()
    try:
        result = ingest_raw_job(session, raw_job)
    finally:
        session.close()

    if result.created:
        typer.echo(f"Discovered new job (id={result.job.id}): {result.job.title} @ {result.job.company}")
        if result.job.company == "Unknown Company" or result.job.title == "Unknown Title":
            typer.echo(
                "  Note: company/title could not be confidently guessed from the description. "
                "Re-run with --company/--title to set them explicitly."
            )
    else:
        typer.echo(
            f"Duplicate of existing job (id={result.job.id}): "
            f"{result.job.title} @ {result.job.company} -- not re-imported"
        )


def _print_breakdown(result) -> None:
    from app.matching.pipeline import AnalyzeResult

    result: AnalyzeResult = result
    job = result.job
    if not result.filter_result.passed:
        typer.echo(f"[{job.id}] {job.title} @ {job.company} -- FILTERED")
        for reason in result.filter_result.reasons:
            typer.echo(f"    - {reason}")
        return

    b = result.breakdown
    status_label = "QUALIFIED" if result.qualified else "analyzed (below threshold)"
    typer.echo(f"[{job.id}] {job.title} @ {job.company} -- {b.overall_score}/100 ({status_label})")
    typer.echo(
        f"    technical={b.technical_score} experience={b.experience_score} title={b.title_score} "
        f"seniority={b.seniority_score} platform={b.platform_score} location={b.location_score} "
        f"domain={b.domain_score} education={b.education_score}"
    )
    if b.strong_matches:
        typer.echo(f"    Strong: {', '.join(b.strong_matches)}")
    if b.partial_matches:
        typer.echo(f"    Partial: {', '.join(b.partial_matches)}")
    if b.critical_missing_requirements:
        typer.echo(f"    Missing (required): {', '.join(b.critical_missing_requirements)}")
    typer.echo(f"    {b.explanation}")


@app.command()
def analyze(
    job_id: int = typer.Argument(None, help="Job id to analyze. Omit and use --all to analyze every DISCOVERED job."),
    all_jobs: bool = typer.Option(False, "--all", help="Analyze every job currently in DISCOVERED status"),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help=_CONFIG_HELP),
) -> None:
    """Run hard filters and deterministic scoring on discovered jobs.

    If config.yaml's `ai.enabled` is true, AI-based requirement extraction
    (Milestone 5) runs first and extends what the deterministic scorer can
    recognize beyond your configured skill lists -- disabled by default so
    this never makes a billed call unless you opt in. See "Analyzing jobs" in
    the README.
    """
    configure_logging()

    if job_id is None and not all_jobs:
        typer.echo("Provide a JOB_ID or pass --all.", err=True)
        raise typer.Exit(code=1)

    config = _load_settings(config_path)

    from app.ai.factory import AIProviderConfigError, build_ai_provider
    from app.ai.settings import AISettings
    from app.database.session import SessionLocal
    from app.matching.pipeline import NoActiveCandidateProfileError, analyze_job
    from app.models.job import Job, JobStatus

    ai_provider = None
    if config.ai.enabled or config.ai.generate_match_explanations:
        try:
            ai_provider = build_ai_provider(config.ai, AISettings())
        except AIProviderConfigError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1)

    session = SessionLocal()
    try:
        if all_jobs:
            jobs = session.query(Job).filter_by(status=JobStatus.DISCOVERED).all()
            if not jobs:
                typer.echo("No jobs in DISCOVERED status to analyze.")
                return
        else:
            job = session.get(Job, job_id)
            if job is None:
                typer.echo(f"No job found with id={job_id}", err=True)
                raise typer.Exit(code=1)
            jobs = [job]

        qualified_count = 0
        filtered_count = 0
        for job in jobs:
            try:
                result = analyze_job(session, job, config, ai_provider=ai_provider)
            except NoActiveCandidateProfileError as exc:
                typer.echo(str(exc), err=True)
                raise typer.Exit(code=1)
            _print_breakdown(result)
            if not result.filter_result.passed:
                filtered_count += 1
            elif result.qualified:
                qualified_count += 1

        if len(jobs) > 1:
            typer.echo(
                f"\nAnalyzed {len(jobs)} job(s): {qualified_count} qualified, "
                f"{filtered_count} filtered, {len(jobs) - qualified_count - filtered_count} below threshold"
            )
    finally:
        session.close()


@app.command("generate-resume")
def generate_resume(
    job_id: int,
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help=_CONFIG_HELP),
) -> None:
    """Generate a tailored, truthfulness-validated resume (DOCX, and PDF if LibreOffice
    is available) for a job, and organize its artifact bundle under data/applications/.
    """
    configure_logging()

    config = _load_settings(config_path)

    from app.ai.factory import AIProviderConfigError
    from app.database.session import SessionLocal
    from app.models.job import Job
    from app.resume.pipeline import generate_resume_bundle
    from app.resume.tailor import NoActiveCandidateProfileError

    session = SessionLocal()
    try:
        job = session.get(Job, job_id)
        if job is None:
            typer.echo(f"No job found with id={job_id}", err=True)
            raise typer.Exit(code=1)

        try:
            result = generate_resume_bundle(session, job, config)
        except NoActiveCandidateProfileError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1)
        except AIProviderConfigError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1)

        typer.echo(f"Generated tailored resume for job {job_id}: {result.tailor_result.docx_path}")
        typer.echo(f"  Validation status: {result.tailor_result.tailored_resume.validation_status}")
        if result.pdf_path:
            typer.echo(f"  PDF: {result.pdf_path}")
        else:
            typer.echo(f"  PDF conversion skipped: {result.pdf_error}", err=True)
        typer.echo(f"  Artifact bundle: {result.artifact_set.folder_path}")
        typer.echo(f"  Job {job_id} is now READY_FOR_REVIEW.")
    finally:
        session.close()


def _job_action_command(action_name: str, help_text: str):
    """Builds a `jobassistant <action> JOB_ID` CLI command around one
    `app.applications.state_machine` transition function -- these all share
    the same shape (load job, apply transition, report old/new status).
    """

    def command(job_id: int) -> None:
        configure_logging()

        from app.applications import state_machine
        from app.database.session import SessionLocal
        from app.models.job import Job

        transition_fn = getattr(state_machine, action_name)

        session = SessionLocal()
        try:
            job = session.get(Job, job_id)
            if job is None:
                typer.echo(f"No job found with id={job_id}", err=True)
                raise typer.Exit(code=1)

            from_status = job.status
            try:
                transition_fn(session, job)
            except state_machine.InvalidTransitionError as exc:
                typer.echo(str(exc), err=True)
                raise typer.Exit(code=1)

            typer.echo(f"Job {job_id}: {from_status.value} -> {job.status.value}")
        finally:
            session.close()

    command.__doc__ = help_text
    return command


app.command("approve")(_job_action_command("approve", "Approve a reviewed job's tailored resume."))
app.command("skip")(_job_action_command("skip", "Skip a job (stop pursuing it)."))
app.command("mark-applied")(_job_action_command("mark_applied", "Mark a job as applied to."))
app.command("mark-interview")(_job_action_command("mark_interview", "Mark a job as in the interview stage."))
app.command("mark-offer")(_job_action_command("mark_offer", "Mark a job as having received an offer."))
app.command("mark-rejected")(_job_action_command("mark_rejected", "Mark a job as rejected."))
app.command("archive")(_job_action_command("archive", "Archive a job (fully done, no further action)."))


@app.command("open-application")
def open_application(job_id: int) -> None:
    """Open the job's source URL in your browser and mark it APPLICATION_OPENED.

    This is the one step that touches the source platform (LinkedIn, a careers
    page, etc.) -- and it opens a normal browser tab for you to apply manually,
    exactly the same as clicking the link yourself. Nothing is scripted there.
    """
    configure_logging()

    import webbrowser

    from app.applications import state_machine
    from app.database.session import SessionLocal
    from app.models.job import Job

    session = SessionLocal()
    try:
        job = session.get(Job, job_id)
        if job is None:
            typer.echo(f"No job found with id={job_id}", err=True)
            raise typer.Exit(code=1)

        try:
            state_machine.mark_application_opened(session, job)
        except state_machine.InvalidTransitionError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1)

        webbrowser.open(job.source_url)
        typer.echo(f"Opened {job.source_url} -- job {job_id} is now APPLICATION_OPENED.")
    finally:
        session.close()


@app.command()
def run(
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help=_CONFIG_HELP),
) -> None:
    """Run the full discover -> analyze -> tailor pipeline once (Milestone 11)."""
    configure_logging()

    config = _load_settings(config_path)

    from app.database.session import SessionLocal
    from app.scheduler.run import run_once

    session = SessionLocal()
    try:
        summary = run_once(session, config)
    finally:
        session.close()

    typer.echo(
        f"Run complete: {summary.jobs_discovered} discovered, {summary.jobs_new} new, "
        f"{summary.jobs_qualified} qualified, {summary.jobs_tailored} tailored, "
        f"{summary.source_failures} source failure(s)."
    )
    if summary.source_failures:
        raise typer.Exit(code=1)


@app.command()
def serve(
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help=_CONFIG_HELP),
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port"),
) -> None:
    """Start the dashboard web server plus the background scheduler (Milestone
    11). Scans run while scheduler.enabled is on in Settings.
    """
    configure_logging()

    config = _load_settings(config_path)

    import uvicorn

    from app.scheduler.background import start_background_scheduler

    # Always started: scans can be switched on/off and re-timed from the dashboard.
    background_scheduler = start_background_scheduler(config, config_path)
    state = "on" if config.scheduler.enabled else "paused (enable it on the Settings page)"
    typer.echo(f"Background scheduler {state}, every {config.scheduler.interval_minutes} minutes.")

    try:
        uvicorn.run("main:app", host=host, port=port)
    finally:
        background_scheduler.shutdown(wait=False)



@app.command("telegram-setup")
def telegram_setup(
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help=_CONFIG_HELP),
) -> None:
    """Detect and save your Telegram chat id (same as the Watchlist page's
    "Detect chat ID" button): send your bot any message first. Needs
    TELEGRAM_BOT_TOKEN in .env.
    """
    from app.database.session import SessionLocal
    from app.notifications.telegram import TelegramError
    from app.services.telegram import detect_and_save_chat_id

    config = _load_settings(config_path)
    session = SessionLocal()
    try:
        chat = detect_and_save_chat_id(session, config)
    except TelegramError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1)
    finally:
        session.close()
    typer.echo(f"Saved Telegram chat id {chat['id']}. Run `python cli.py telegram-test` to check it.")


@app.command("telegram-test")
def telegram_test(
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help=_CONFIG_HELP),
) -> None:
    """Send a test message to your Telegram chat."""
    from app.notifications.telegram import TelegramError
    from app.services.telegram import send_test_message

    try:
        send_test_message(_load_settings(config_path))
    except TelegramError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1)
    typer.echo("Test message sent.")


@app.command("rebuild-resumes")
def rebuild_resumes(
    job_id: int = typer.Option(None, "--job", help="Only this job (default: every job with a tailored resume)"),
    pdf: bool = typer.Option(True, help="Also regenerate the PDF (needs LibreOffice)"),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help=_CONFIG_HELP),
) -> None:
    """Re-create tailored resume files in your base resume's current design,
    from the wording already saved for each job -- no AI calls. Use after
    changing the look of your base resume.
    """
    from sqlalchemy import func

    from app.database.session import SessionLocal
    from app.documents.libreoffice import PdfConversionError, convert_to_pdf
    from app.models.resume import TailoredResume
    from app.resume.tailor import rebuild_tailored_resume

    config = _load_settings(config_path)
    rebuilt = skipped = pdf_failed = 0
    with SessionLocal() as session:
        latest = session.query(func.max(TailoredResume.id)).group_by(TailoredResume.job_id)
        if job_id is not None:
            latest = latest.filter(TailoredResume.job_id == job_id)
        rows = session.query(TailoredResume).filter(TailoredResume.id.in_(latest)).order_by(TailoredResume.job_id).all()
        for tailored in rows:
            path = rebuild_tailored_resume(session, tailored, config)
            if path is None:
                skipped += 1
                continue
            rebuilt += 1
            if pdf:
                try:
                    tailored.pdf_path = str(convert_to_pdf(path))
                    session.commit()
                except PdfConversionError as exc:
                    pdf_failed += 1
                    typer.echo(f"  job {tailored.job_id}: PDF failed ({exc})", err=True)
            typer.echo(f"  job {tailored.job_id}: {path}")
    typer.echo(f"Rebuilt {rebuilt} resumes" + (f", skipped {skipped} (source resume gone)" if skipped else "")
               + (f", {pdf_failed} PDFs failed" if pdf_failed else "") + ".")


@app.command("cleanup-jobs")
def cleanup_jobs(
    days: int = typer.Option(None, help="Override Settings -> Retention for this run (0 = delete nothing)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Only list what would be deleted"),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help=_CONFIG_HELP),
) -> None:
    """Delete stored jobs older than the retention period (default 7 days), with
    their scores, analyses and tailored resumes. Jobs you've acted on are kept.
    Runs automatically after every scheduled scan."""
    from app.database.session import SessionLocal
    from app.services.retention import purge_old_jobs

    config = _load_settings(config_path)
    if days is not None:
        config.retention.delete_jobs_after_days = days
    with SessionLocal() as session:
        report = purge_old_jobs(session, config, dry_run=dry_run)
    if config.retention.delete_jobs_after_days <= 0:
        typer.echo("Retention is off (delete_jobs_after_days = 0): nothing deleted.")
        return
    verb = "Would delete" if dry_run else "Deleted"
    typer.echo(f"{verb} {report.deleted} jobs older than {config.retention.delete_jobs_after_days} days"
               + (f"; kept {report.kept_acted_on} you've acted on" if report.kept_acted_on else "") + ".")
    for title in report.deleted_titles[:50]:
        typer.echo(f"  - {title}")


@app.command("catalog-scan")
def catalog_scan(
    country: str = typer.Option(None, help="Adzuna country code (default: Settings -> Job search country)"),
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help=_CONFIG_HELP),
) -> None:
    """Fill the search-term catalog (the job title/keyword/location/company
    suggestions) from a quick scan of the whole job market -- about 50 Adzuna
    requests. Needs ADZUNA_APP_ID/ADZUNA_APP_KEY in .env.
    """
    from app.catalog.scan import plural, scan_market
    from app.catalog.store import counts, seed_catalog
    from app.database.session import SessionLocal
    from app.sources.adzuna import AdzunaSource
    from app.sources.settings import AdzunaSettings

    settings = AdzunaSettings()
    if not (settings.app_id and settings.app_key):
        typer.echo("Set ADZUNA_APP_ID and ADZUNA_APP_KEY in .env first.", err=True)
        raise typer.Exit(code=1)
    country = (country or _load_settings(config_path).search.country).lower()
    adzuna = AdzunaSource(app_id=settings.app_id, app_key=settings.app_key, country=country)
    with SessionLocal() as session:
        seed_catalog(session)
        report = scan_market(session, adzuna, country=country, progress=lambda m: typer.echo(f"  {m}"))
        totals = counts(session)
    typer.echo(f"Scanned {report.postings} postings with {report.requests} requests.")
    typer.echo("New: " + ", ".join(plural(n, kind) for kind, n in report.new_terms.items())
               + f"; dropped {report.removed} no longer seen")
    typer.echo("Catalog now: " + ", ".join(plural(n, kind) for kind, n in sorted(totals.items())))
    for error in report.errors:
        typer.echo(f"  failed: {error}", err=True)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
