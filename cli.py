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
    """Parse a base resume DOCX into the structured candidate profile."""
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

    AI-based requirement extraction (Milestone 5) is not yet part of this
    pipeline -- see docs/ARCHITECTURE.md for what this milestone can and
    can't detect in a job posting today.
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

    from app.database.session import SessionLocal
    from app.matching.pipeline import NoActiveCandidateProfileError, analyze_job
    from app.models.job import Job, JobStatus

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
                result = analyze_job(session, job, config)
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
def generate_resume(job_id: int) -> None:
    """Generate a tailored resume for a qualified job."""
    _not_implemented("generate-resume", 6)


@app.command()
def run() -> None:
    """Run the full discover -> analyze -> tailor pipeline once."""
    _not_implemented("run", 11)


@app.command()
def serve() -> None:
    """Start the dashboard web server and scheduler."""
    _not_implemented("serve", 11)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
