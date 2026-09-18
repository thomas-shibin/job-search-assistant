"""Writes the per-job artifact bundle to disk under `data/applications/...`
(Milestone 8, spec "File Organization") and keeps the one `ApplicationArtifactSet`
row per job pointing at it. Called once a tailored resume exists (see
`cli.py generate-resume`); re-running it just overwrites the same files/row rather
than creating duplicates, so it's safe to call again after e.g. regenerating a resume.
"""
from __future__ import annotations

import json
from datetime import date, datetime

from sqlalchemy.orm import Session

from app.applications.folder import get_application_folder
from app.models.application import ApplicationArtifactSet
from app.models.job import Job
from app.models.matching import MatchAnalysis
from app.models.resume import TailoredResume


def _json_default(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def build_artifact_set(session: Session, job: Job, tailored_resume: TailoredResume | None) -> ApplicationArtifactSet:
    folder = get_application_folder(job)

    job_json_path = folder / "job.json"
    job_json_path.write_text(
        json.dumps(
            {
                "id": job.id,
                "source": job.source,
                "source_url": job.source_url,
                "company": job.company,
                "title": job.title,
                "location": job.location,
                "remote_type": job.remote_type,
                "status": job.status.value,
                "date_discovered": job.date_discovered,
            },
            default=_json_default,
            indent=2,
        ),
        encoding="utf-8",
    )

    job_description_path = folder / "job_description.txt"
    job_description_path.write_text(job.description, encoding="utf-8")

    match_analysis = (
        session.query(MatchAnalysis).filter_by(job_id=job.id).order_by(MatchAnalysis.scored_at.desc()).first()
    )
    match_analysis_path = None
    if match_analysis is not None:
        match_analysis_path = folder / "match_analysis.json"
        match_analysis_path.write_text(
            json.dumps(
                {
                    "overall_score": match_analysis.overall_score,
                    "technical_score": match_analysis.technical_score,
                    "experience_score": match_analysis.experience_score,
                    "title_score": match_analysis.title_score,
                    "seniority_score": match_analysis.seniority_score,
                    "platform_score": match_analysis.platform_score,
                    "location_score": match_analysis.location_score,
                    "domain_score": match_analysis.domain_score,
                    "education_score": match_analysis.education_score,
                    "strong_matches": match_analysis.strong_matches,
                    "partial_matches": match_analysis.partial_matches,
                    "critical_missing_requirements": match_analysis.critical_missing_requirements,
                    "explanation": match_analysis.explanation,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    metadata_path = folder / "metadata.json"
    metadata_path.write_text(
        json.dumps({"job_id": job.id, "generated_at": datetime.utcnow().isoformat()}, indent=2),
        encoding="utf-8",
    )

    artifact_set = session.query(ApplicationArtifactSet).filter_by(job_id=job.id).first()
    if artifact_set is None:
        artifact_set = ApplicationArtifactSet(job_id=job.id, folder_path=str(folder))
        session.add(artifact_set)

    artifact_set.folder_path = str(folder)
    artifact_set.job_json_path = str(job_json_path)
    artifact_set.job_description_path = str(job_description_path)
    artifact_set.match_analysis_path = str(match_analysis_path) if match_analysis_path else None
    artifact_set.tailored_resume_docx_path = tailored_resume.docx_path if tailored_resume else None
    artifact_set.tailored_resume_pdf_path = tailored_resume.pdf_path if tailored_resume else None
    artifact_set.metadata_path = str(metadata_path)

    session.commit()
    session.refresh(artifact_set)
    return artifact_set
