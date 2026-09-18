from __future__ import annotations

import shutil
from pathlib import Path

import typer

from app.config.loader import ConfigError, load_config, validate_config
from app.utils.logging import configure_logging

app = typer.Typer(help="Local AI Job Application Assistant CLI")

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"
EXAMPLE_CONFIG_PATH = PROJECT_ROOT / "config" / "config.example.yaml"


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
    typer.echo(f"  1. Edit {DEFAULT_CONFIG_PATH} with your real candidate info and search profiles")
    typer.echo("  2. Place your base resume at the path referenced by candidate.base_resume")
    typer.echo("  3. Run 'alembic upgrade head' to create the local database")
    typer.echo("  4. Run 'jobassistant validate-config' to confirm everything is wired up")


@app.command("validate-config")
def validate_config_cmd(
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="Path to config.yaml"),
) -> None:
    """Load and validate config.yaml, reporting any errors."""
    configure_logging()

    if not config_path.exists():
        typer.echo(f"Config file not found: {config_path}", err=True)
        raise typer.Exit(code=1)

    try:
        config = load_config(config_path)
    except ConfigError as exc:
        typer.echo("Configuration is INVALID:", err=True)
        for err in exc.errors:
            typer.echo(f"  - {err}", err=True)
        raise typer.Exit(code=1)

    errors = validate_config(config, base_dir=PROJECT_ROOT)
    if errors:
        typer.echo("Configuration is INVALID:", err=True)
        for err in errors:
            typer.echo(f"  - {err}", err=True)
        raise typer.Exit(code=1)

    typer.echo("Configuration is OK.")


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
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="Path to config.yaml"),
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

    if not config_path.exists():
        typer.echo(f"Config file not found: {config_path}", err=True)
        raise typer.Exit(code=1)

    try:
        config = load_config(config_path)
    except ConfigError as exc:
        typer.echo("Configuration is INVALID:", err=True)
        for err in exc.errors:
            typer.echo(f"  - {err}", err=True)
        raise typer.Exit(code=1)

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
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="Path to config.yaml"),
) -> None:
    """Generate a tailored, truthfulness-validated resume (DOCX, and PDF if LibreOffice
    is available) for a job, and organize its artifact bundle under data/applications/.
    """
    configure_logging()

    if not config_path.exists():
        typer.echo(f"Config file not found: {config_path}", err=True)
        raise typer.Exit(code=1)

    try:
        config = load_config(config_path)
    except ConfigError as exc:
        typer.echo("Configuration is INVALID:", err=True)
        for err in exc.errors:
            typer.echo(f"  - {err}", err=True)
        raise typer.Exit(code=1)

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
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="Path to config.yaml"),
) -> None:
    """Run the full discover -> analyze -> tailor pipeline once (Milestone 11)."""
    configure_logging()

    if not config_path.exists():
        typer.echo(f"Config file not found: {config_path}", err=True)
        raise typer.Exit(code=1)

    try:
        config = load_config(config_path)
    except ConfigError as exc:
        typer.echo("Configuration is INVALID:", err=True)
        for err in exc.errors:
            typer.echo(f"  - {err}", err=True)
        raise typer.Exit(code=1)

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
    config_path: Path = typer.Option(DEFAULT_CONFIG_PATH, "--config", help="Path to config.yaml"),
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port"),
) -> None:
    """Start the dashboard web server, plus the background scheduler if
    config.yaml's scheduler.enabled is true (Milestone 11).
    """
    configure_logging()

    if not config_path.exists():
        typer.echo(f"Config file not found: {config_path}", err=True)
        raise typer.Exit(code=1)

    try:
        config = load_config(config_path)
    except ConfigError as exc:
        typer.echo("Configuration is INVALID:", err=True)
        for err in exc.errors:
            typer.echo(f"  - {err}", err=True)
        raise typer.Exit(code=1)

    import uvicorn

    from app.scheduler.background import start_background_scheduler

    background_scheduler = None
    if config.scheduler.enabled:
        background_scheduler = start_background_scheduler(config, config_path)
        typer.echo(f"Background scheduler started (every {config.scheduler.interval_minutes} minutes).")

    try:
        uvicorn.run("main:app", host=host, port=port)
    finally:
        if background_scheduler is not None:
            background_scheduler.shutdown(wait=False)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
