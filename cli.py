from __future__ import annotations

import shutil
from pathlib import Path

import typer

from app.core.config.loader import ConfigError, validate_config
from app.core.logging import configure_logging

app = typer.Typer(help="Local AI Job Application Assistant CLI")

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"
EXAMPLE_CONFIG_PATH = PROJECT_ROOT / "config" / "config.example.yaml"

_CONFIG_HELP = "config.yaml to import if the database has no settings yet (settings live in the DB)"


def _print_config_errors(errors: list[str]) -> None:
    typer.echo("Configuration is INVALID:", err=True)
    for err in errors:
        typer.echo(f"  - {err}", err=True)


_acting_user_email: str | None = None


@app.callback()
def _global_options(
    user: str = typer.Option(None, "--user", envvar="JSA_USER", metavar="EMAIL",
                             help="Account to act as, for commands that work on one user's data."),
) -> None:
    global _acting_user_email
    _acting_user_email = user


def _session():
    """A database session -- bound to `--user`'s account when given, so a
    command only ever sees and changes that user's data."""
    from app.core.database.session import SessionLocal
    from app.modules.auth.models import Role, User
    from app.core.tenancy import bind_tenant

    session = SessionLocal()
    if _acting_user_email:
        user = session.query(User).filter(
            User.email == _acting_user_email.strip().lower(), User.role != Role.GUEST.value).one_or_none()
        if user is None:
            session.close()
            typer.echo(f"No account with email {_acting_user_email}.", err=True)
            raise typer.Exit(code=1)
        bind_tenant(session, user.id)
    return session


def _load_settings(config_path: Path):
    """Effective settings from the database (importing `config_path` on first use)."""
    from app.core.config.store import load_app_config

    session = _session()
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

    from app.core.config.store import has_settings, import_yaml_config

    session = _session()
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

    from app.modules.resumes.parser import import_resume as run_import_resume

    session = _session()
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

    from app.modules.jobs.ingestion.pipeline import ingest_raw_job
    from app.modules.jobs.ingestion.manual import import_manual_job

    raw_job = import_manual_job(
        description=description, url=url, company=company, title=title, location=location
    )

    session = _session()
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
    from app.modules.matching.pipeline import AnalyzeResult

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

    from app.integrations.ai.factory import AIProviderConfigError, build_ai_provider
    from app.integrations.ai.settings import AISettings
    from app.modules.matching.pipeline import NoActiveCandidateProfileError, analyze_job
    from app.modules.jobs.models import Job, JobStatus

    ai_provider = None
    if config.ai.enabled or config.ai.generate_match_explanations:
        try:
            ai_provider = build_ai_provider(config.ai, AISettings())
        except AIProviderConfigError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1)

    session = _session()
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

    from app.integrations.ai.factory import AIProviderConfigError
    from app.modules.jobs.models import Job
    from app.modules.resumes.pipeline import generate_resume_bundle
    from app.modules.resumes.tailor import NoActiveCandidateProfileError

    session = _session()
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
    """Builds a `hirlumo <action> JOB_ID` CLI command around one
    `app.modules.applications.state_machine` transition function -- these all share
    the same shape (load job, apply transition, report old/new status).
    """

    def command(job_id: int) -> None:
        configure_logging()

        from app.modules.applications import state_machine
        from app.modules.jobs.models import Job

        transition_fn = getattr(state_machine, action_name)

        session = _session()
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

    from app.modules.applications import state_machine
    from app.modules.jobs.models import Job

    session = _session()
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
    """Run the full discover -> analyze -> tailor pipeline once: for every
    member, or with --user for just that account."""
    configure_logging()

    from app.core.database.session import SessionLocal
    from app.workers.run import RunSummary, run_once, scan_members

    if not _acting_user_email:
        results = scan_members(SessionLocal, config_path)
        summary = RunSummary()
        for one in results.values():
            for field in ("jobs_discovered", "jobs_new", "jobs_qualified", "jobs_tailored", "source_failures"):
                setattr(summary, field, getattr(summary, field) + getattr(one, field))
        typer.echo(f"Scanned {len(results)} member(s).")
    else:
        config = _load_settings(config_path)
        session = _session()
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

    from app.modules.alerts.telegram_commands import start_telegram_command_listener
    from app.workers.background import start_background_scheduler

    # Always started: scans can be switched on/off and re-timed from the dashboard.
    background_scheduler = start_background_scheduler(config, config_path)
    state = "on" if config.scheduler.enabled else "paused (enable it on the Settings page)"
    typer.echo(f"Background scheduler {state}, every {config.scheduler.interval_minutes} minutes.")
    # Answers /health in Telegram once a bot token and chat id are set (idles until then).
    telegram_listener = start_telegram_command_listener(config_path)

    try:
        uvicorn.run("main:app", host=host, port=port)
    finally:
        telegram_listener.stop()
        background_scheduler.shutdown(wait=False)



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

    from app.integrations.libreoffice import PdfConversionError, convert_to_pdf
    from app.modules.resumes.tailoring_models import TailoredResume
    from app.modules.resumes.tailor import rebuild_tailored_resume

    config = _load_settings(config_path)
    rebuilt = skipped = pdf_failed = 0
    with _session() as session:
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
    from app.modules.applications.retention import purge_old_jobs

    config = _load_settings(config_path)
    if days is not None:
        config.retention.delete_jobs_after_days = days
    with _session() as session:
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
    from app.modules.jobs.catalog.scan import plural, scan_market
    from app.modules.jobs.catalog.store import counts, seed_catalog
    from app.integrations.job_boards.adzuna import AdzunaSource
    from app.integrations.job_boards.settings import AdzunaSettings

    settings = AdzunaSettings()
    if not (settings.app_id and settings.app_key):
        typer.echo("Set ADZUNA_APP_ID and ADZUNA_APP_KEY in .env first.", err=True)
        raise typer.Exit(code=1)
    country = (country or _load_settings(config_path).search.country).lower()
    adzuna = AdzunaSource(app_id=settings.app_id, app_key=settings.app_key, country=country)
    with _session() as session:
        seed_catalog(session)
        report = scan_market(session, adzuna, country=country, progress=lambda m: typer.echo(f"  {m}"))
        totals = counts(session)
    typer.echo(f"Scanned {report.postings} postings with {report.requests} requests.")
    typer.echo("New: " + ", ".join(plural(n, kind) for kind, n in report.new_terms.items())
               + f"; dropped {report.removed} no longer seen")
    typer.echo("Catalog now: " + ", ".join(plural(n, kind) for kind, n in sorted(totals.items())))
    for error in report.errors:
        typer.echo(f"  failed: {error}", err=True)


@app.command("claim-legacy-data")
def claim_legacy_data(email: str = typer.Option(..., "--email", help="The account that should own the data")) -> None:
    """Give the data from before multi-user accounts (the placeholder owner the
    upgrade created) to EMAIL: an existing account gets everything moved to it;
    otherwise the placeholder becomes EMAIL (confirmed), so you can sign in with
    Google/Facebook on that address or use "Forgot password"."""
    configure_logging()
    from app.modules.auth.service import _tenant_models, normalize_email
    from app.modules.auth.tokens import utcnow
    from app.core.database.session import SessionLocal
    from app.modules.auth.models import Role, User
    from app.core.tenancy import system_session

    address = normalize_email(email)
    placeholder_email = "owner@legacy.invalid"
    with system_session(SessionLocal) as session:
        legacy = session.query(User).filter(User.email == placeholder_email).one_or_none()
        if legacy is None:
            typer.echo("Nothing to claim: there's no placeholder owner (the data already has a real owner).")
            return
        target = session.query(User).filter(User.email == address, User.role != Role.GUEST.value).one_or_none()
        if target is None:
            legacy.email, legacy.email_verified_at = address, utcnow()
            session.commit()
            typer.echo(f"The existing data now belongs to {address} (an admin). Sign in with Google/Facebook on "
                       "that address, or use \"Forgot password\" on the sign-in page to set a password.")
            return
        from app.core.config.models import UserSetting

        if session.query(UserSetting).filter(UserSetting.user_id == target.id).first():
            session.query(UserSetting).filter(UserSetting.user_id == legacy.id).delete()  # keep the target's own
        for model in _tenant_models():
            session.query(model).filter(model.user_id == legacy.id).update({model.user_id: target.id})
        session.delete(legacy)
        session.commit()
    typer.echo(f"Moved the existing data to {address}.")


@app.command("set-password")
def set_password(email: str = typer.Option(..., "--email")) -> None:
    """Set an account's password from the server (e.g. your own admin account
    before outgoing email is set up). Confirms the address too."""
    from app.modules.auth.passwords import hash_password, password_problems, PASSWORD_RULES
    from app.modules.auth.service import normalize_email
    from app.modules.auth.tokens import utcnow
    from app.core.database.session import SessionLocal
    from app.modules.auth.models import AuthSession, Role, User
    from app.core.tenancy import system_session

    with system_session(SessionLocal) as session:
        user = session.query(User).filter(User.email == normalize_email(email), User.role != Role.GUEST.value).one_or_none()
        if user is None:
            typer.echo(f"No account with email {email}.", err=True)
            raise typer.Exit(code=1)
        typer.echo(PASSWORD_RULES)
        password = typer.prompt("New password", hide_input=True, confirmation_prompt=True)
        problems = password_problems(password, email=user.email, first_name=user.first_name, last_name=user.last_name)
        if problems:
            typer.echo("Choose a stronger password: " + "; ".join(problems), err=True)
            raise typer.Exit(code=1)
        user.password_hash = hash_password(password)
        user.email_verified_at = user.email_verified_at or utcnow()
        user.failed_logins, user.locked_until = 0, None
        session.query(AuthSession).filter(AuthSession.user_id == user.id).delete()
        session.commit()
    typer.echo(f"Password set for {email}. Sign in at the app's /login page.")


@app.command("grant-role")
def grant_role_cmd(email: str = typer.Option(..., "--email"),
                   role: str = typer.Option(..., "--role", help="e.g. SUPER_ADMIN, ADMIN, JOB_SEEKER"),
                   revoke: bool = typer.Option(False, "--revoke", help="Take the role away instead")) -> None:
    """Give (or take) a role from the server -- the trusted way to create the
    first Super Admin or recover access. Recorded in the audit log."""
    from app.modules.access.service import audit
    from app.modules.access import rbac
    from app.modules.auth.service import normalize_email
    from app.core.database.session import SessionLocal
    from app.modules.access.models import UserRole
    from app.modules.auth.models import Role, User
    from app.core.tenancy import system_session

    with system_session(SessionLocal) as session:
        rbac.sync_catalog(session.connection())
        user = session.query(User).filter(User.email == normalize_email(email), User.role != Role.GUEST.value).one_or_none()
        role_id = rbac.role_id(session, role.upper())
        if user is None or role_id is None:
            typer.echo(f"No account with email {email}." if user is None else f"No role named {role}.", err=True)
            raise typer.Exit(code=1)
        before = sorted(rbac.roles_of(user, session))
        if revoke:
            if role.upper() == rbac.SUPER_ADMIN and rbac.is_last_super_admin(session, user.id):
                typer.echo("That is the last active Super Admin; grant the role to someone else first.", err=True)
                raise typer.Exit(code=1)
            session.query(UserRole).filter(UserRole.user_id == user.id, UserRole.role_id == role_id).delete()
        else:
            rbac.grant_role(session, user, role.upper())
        rbac.forget(user)
        after = sorted(rbac.roles_of(user, session))
        if after != before:
            audit(session, None, "user.roles.update", "user", user.id, {"roles": before}, {"roles": after})
        session.commit()
    typer.echo(f"{email} now has: {', '.join(after) or 'no roles'}.")


@app.command("backup")
def backup_cmd() -> None:
    """Back up the database and members' files now (docs/BACKUPS.md). The site
    also does this every night by itself."""
    configure_logging()
    from app.modules.backups import service as backups

    try:
        result = backups.create()
    except backups.BackupError as exc:
        typer.echo(f"Backup failed: {exc}", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"Saved data/backups/{result['file']} ({result['size'] // 1_000_000} MB"
               f"{', encrypted' if result['encrypted'] else ''}).")
    if result["uploaded"]:
        typer.echo("Copied off the server.")
    elif result["upload_error"]:
        typer.echo(f"Not copied off the server: {result['upload_error']}", err=True)


@app.command("backup-list")
def backup_list_cmd() -> None:
    """The backups on this server and in the off-server bucket."""
    from app.integrations.object_storage import ObjectStorage, StorageSettings
    from app.modules.backups import service as backups

    typer.echo("On this server (data/backups):")
    for name, size in backups.local_backups() or [("(none)", 0)]:
        typer.echo(f"  {name}  {size // 1_000_000} MB")
    settings = StorageSettings()
    if settings.configured:
        typer.echo(f"In the bucket {settings.bucket}:")
        names = sorted(ObjectStorage(settings).names(), reverse=True)
        for name, size in names or [("(none)", 0)]:
            typer.echo(f"  {name}  {size // 1_000_000} MB")


@app.command("backup-restore")
def backup_restore_cmd(
    name: str = typer.Argument(..., help="A backup's file name, from backup-list"),
    to: Path = typer.Option(..., "--to", help="A new folder to unpack it into"),
) -> None:
    """Unpack a backup into a new folder. The running site is never touched:
    docs/BACKUPS.md says how to swap the restored files in."""
    from app.integrations.object_storage import ObjectStorage, StorageSettings
    from app.modules.backups import service as backups

    local = backups.default_places().backups / name
    if not local.exists():
        settings = StorageSettings()
        if not settings.configured:
            typer.echo(f"No backup called {name} on this server.", err=True)
            raise typer.Exit(code=1)
        typer.echo(f"Downloading {name}...")
        local = to.parent / name
        ObjectStorage(settings).download(name, local)
    try:
        backups.extract(local, to, backups.BackupSettings().encryption_key)
    except (backups.BackupError, ValueError) as exc:
        typer.echo(f"Couldn't restore: {exc}", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"Unpacked into {to}: the database ({backups.default_places().database.name}), resumes/, "
               "applications/ and config/.")


@app.command("backup-key")
def backup_key_cmd() -> None:
    """Print a new random BACKUP_ENCRYPTION_KEY to put in .env (and in a password manager)."""
    from app.modules.backups.crypto import new_key

    typer.echo(new_key())


def main() -> None:
    from app.core.tenancy import TenantContextError

    try:
        app()
    except TenantContextError:
        typer.echo("This command works on one account's data -- pass --user EMAIL (or set JSA_USER).", err=True)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
