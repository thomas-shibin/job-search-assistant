"""Real `AIProvider` implementation backed by the OpenAI Chat Completions API
(Milestone 5). Transient failures (dropped connection, rate limit) are
retried with exponential backoff (Milestone 13 hardening); anything else
(bad request, auth failure) raises immediately since retrying it would never
succeed. The caller still decides what a final failure means (the pipeline
treats extraction as required but match-explanation polish as best-effort --
see `app/matching/pipeline.py`).
"""
from __future__ import annotations

import json
from typing import Any

from openai import APIConnectionError, OpenAI, RateLimitError

from app.ai.base import ExtractedJobRequirements, TailoredResumeDraft
from app.ai.usage import AIUsage
from app.matching.scorer import MatchScoreBreakdown
from app.utils.retry import with_retry

_RETRYABLE_ERRORS = (APIConnectionError, RateLimitError)

# USD per 1K tokens, (input, output). Not billing-critical -- `estimated_cost`
# on AICallLog is a rough cost-visibility figure (spec §19), not an invoice.
_PRICING_PER_1K: dict[str, tuple[float, float]] = {
    "gpt-4o-mini": (0.00015, 0.0006),
    "gpt-4o": (0.0025, 0.01),
}
_DEFAULT_PRICING_PER_1K = (0.0005, 0.0015)

_EXTRACTION_SYSTEM_PROMPT = """You extract structured hiring requirements from a job posting.
Read the posting text and respond with ONLY a JSON object (no markdown, no commentary) with these keys:
- required_skills: array of strings, skills/technologies explicitly required
- preferred_skills: array of strings, skills/technologies listed as preferred/nice-to-have/bonus
- seniority_level: one of "intern", "junior", "mid", "senior", "staff", "lead", "principal", "director", or null
- min_years_experience: integer minimum years of experience stated, or null if not stated
- remote_type_detected: one of "remote", "hybrid", "onsite", or null if not stated
Only report what the posting actually states. Do not invent or infer skills that are not named in the text."""

_EXPLANATION_SYSTEM_PROMPT = """You polish a deterministic job-match explanation for a human job seeker.
Rewrite the given explanation as 1-2 clear, natural sentences. Do not add any claim, score, or skill
that is not already present in the input -- you may only rephrase, never invent."""

_TAILOR_SYSTEM_PROMPT = """You tailor a candidate's resume to a specific job posting. You will receive JSON with
the job posting, extracted job requirements, and the candidate's ground-truth profile (summary, skills, and a
numbered list of accomplishment bullets, each with an id).

Respond with ONLY a JSON object (no markdown, no commentary) with these keys:
- summary: a 2-4 sentence professional summary tailored to this job
- skills_order: an array of strings, a REORDERED SUBSET of candidate_skills (most relevant to this job first) --
  every entry must be copied exactly from candidate_skills; never invent a skill not in that list
- bullets: an array of objects {"source_bullet_id": <int>, "text": <string>}, one per candidate_bullets entry you
  choose to include (you may omit bullets that are irrelevant to this job, but never invent a new one).
  source_bullet_id MUST be one of the ids given in candidate_bullets. The rewritten text may rephrase, reorder, or
  emphasize -- but MUST NOT introduce any number, percentage, or metric that is not already present in that exact
  source bullet's original text. When in doubt, keep the number unchanged or omit it rather than guessing."""


class OpenAIProvider:
    """Structurally satisfies `app.ai.base.AIProvider`."""

    def __init__(self, api_key: str, model: str = "gpt-4o-mini", client: OpenAI | None = None) -> None:
        self._model = model
        self._client = client or OpenAI(api_key=api_key)
        self.last_usage: AIUsage | None = None

    def _create_completion(self, **kwargs: Any) -> Any:
        return with_retry(lambda: self._client.chat.completions.create(**kwargs), retry_on=_RETRYABLE_ERRORS)

    def _record_usage(self, response: Any) -> None:
        usage = getattr(response, "usage", None)
        if usage is None:
            self.last_usage = None
            return
        tokens_in, tokens_out = usage.prompt_tokens, usage.completion_tokens
        price_in, price_out = _PRICING_PER_1K.get(self._model, _DEFAULT_PRICING_PER_1K)
        cost = (tokens_in / 1000) * price_in + (tokens_out / 1000) * price_out
        self.last_usage = AIUsage(tokens_in=tokens_in, tokens_out=tokens_out, estimated_cost=round(cost, 6))

    def extract_job_requirements(self, job_description: str) -> ExtractedJobRequirements:
        response = self._create_completion(
            model=self._model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _EXTRACTION_SYSTEM_PROMPT},
                {"role": "user", "content": job_description},
            ],
        )
        self._record_usage(response)
        content = response.choices[0].message.content or "{}"
        data = json.loads(content)
        return ExtractedJobRequirements.model_validate(data)

    def tailor_resume(self, candidate, job, requirements, address) -> TailoredResumeDraft:
        payload = {
            "job_title": job.title,
            "job_company": job.company,
            "job_description": job.description,
            "extracted_requirements": requirements.model_dump() if requirements else None,
            "candidate_summary": candidate.summary,
            "candidate_skills": [s.name for s in candidate.skills],
            "candidate_bullets": [
                {"id": b.id, "employment": f"{emp.title} @ {emp.company}", "text": b.text}
                for emp in candidate.employments
                for b in emp.bullets
            ],
        }
        response = self._create_completion(
            model=self._model,
            temperature=0.2,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _TAILOR_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(payload)},
            ],
        )
        self._record_usage(response)
        content = response.choices[0].message.content or "{}"
        data = json.loads(content)
        return TailoredResumeDraft.model_validate(data)

    def generate_match_explanation(self, score: MatchScoreBreakdown) -> str:
        response = self._create_completion(
            model=self._model,
            temperature=0.3,
            messages=[
                {"role": "system", "content": _EXPLANATION_SYSTEM_PROMPT},
                {"role": "user", "content": score.explanation},
            ],
        )
        self._record_usage(response)
        content = response.choices[0].message.content or ""
        return content.strip()
