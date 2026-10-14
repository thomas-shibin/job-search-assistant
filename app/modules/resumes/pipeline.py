"""Orchestrates the full resume-generation flow (Milestones 6-8) used by both
the CLI's `generate-resume` command and the scheduler's automated run
(Milestone 11): tailor -> PDF conversion (best-effort) -> artifact bundle ->
READY_FOR_REVIEW. Centralized here so both callers can't drift out of sync.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.integrations.ai.base import AIProvider
from app.modules.applications.bundle import build_artifact_set
from app.modules.applications.state_machine import mark_ready_for_review
from app.core.config.schema import AppConfig
from app.integrations.libreoffice import PdfConversionError, convert_to_pdf
from app.modules.applications.models import ApplicationArtifactSet
from app.modules.jobs.models import Job
from app.modules.resumes.tailor import TailorResult, tailor_resume


@dataclass
class GenerateResumeResult:
    tailor_result: TailorResult
    pdf_path: str | None
    pdf_error: str | None
    artifact_set: ApplicationArtifactSet


def generate_resume_bundle(
    session: Session, job: Job, config: AppConfig, ai_provider: AIProvider | None = None
) -> GenerateResumeResult:
    tailor_result = tailor_resume(session, job, config, ai_provider=ai_provider)

    pdf_path: str | None = None
    pdf_error: str | None = None
    try:
        pdf_path = str(convert_to_pdf(tailor_result.docx_path))
        tailor_result.tailored_resume.pdf_path = pdf_path
        session.commit()
    except PdfConversionError as exc:
        # A DOCX-only tailored resume is still a usable result -- PDF
        # generation is a nice-to-have, not a hard requirement.
        pdf_error = str(exc)

    # Transition before snapshotting so job.json reflects the job's final status
    # rather than the RESUME_GENERATED status it was in mid-pipeline.
    mark_ready_for_review(session, job)
    artifact_set = build_artifact_set(session, job, tailor_result.tailored_resume)

    return GenerateResumeResult(
        tailor_result=tailor_result, pdf_path=pdf_path, pdf_error=pdf_error, artifact_set=artifact_set
    )
