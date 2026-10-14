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

from app.integrations.ai.base import ExtractedJobRequirements, FitAssessmentResult, TailoredResumeDraft
from app.integrations.ai.usage import AIUsage
from app.matching.scorer import MatchScoreBreakdown
from app.shared.retry import with_retry

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

Tailoring means REWRITING the wording so a recruiter for THIS job immediately sees the match: lead each bullet
with the part most relevant to the posting, use the posting's own terminology for things the candidate really
did (e.g. if the posting says "event streaming" and the bullet describes Kafka pipelines, say so), and make the
summary speak directly to this role. EVERY bullet must be rewritten -- new sentence structure, the job's
vocabulary (see job_keywords), the most relevant outcome first. Returning a bullet's text unchanged, or with
only a word or two swapped, is a failed answer. It does NOT mean shortening the resume or deciding what the
candidate may show: every bullet keeps all of its facts.

Truthfulness rules (a rewrite that breaks one is discarded):
- Never add a number, percentage or metric that isn't in that exact source bullet (rephrasing "12+ years" as
  "over 12 years" is fine; "15 years" is not).
- Only mention technologies, tools, employers and domains that appear in that source bullet, the candidate's
  summary or candidate_skills. Never claim experience the source doesn't state.
- Never add an outcome, benefit or result the source doesn't state -- no "improving efficiency", "enhancing
  delivery", "driving growth" tacked on. Rephrase what is there; don't embellish it.
- Keep each bullet no longer than its source (shorter is fine) and the summary no longer than the candidate's
  summary -- the resume's page layout must not change. Rewrites more than 25% longer are discarded.

Respond with ONLY a JSON object (no markdown, no commentary) with these keys:
- summary: the candidate's summary rewritten for this job
- skills_order: an array of strings containing EVERY entry from candidate_skills exactly once, reordered so the
  most relevant to this job come first -- copied exactly from candidate_skills; never invent a skill
- bullets: an array of objects {"source_bullet_id": <int>, "text": <string>}, with EXACTLY ONE entry for EVERY
  id present in candidate_bullets -- never omit a bullet or an employer's bullets, never invent a bullet;
  source_bullet_id MUST be one of the ids given in candidate_bullets."""


_REWRITE_SYSTEM_PROMPT = """You tailor a candidate's resume to a specific job posting. You will receive JSON with
the job posting, its key terms (job_keywords) and passages: the sentences of the candidate's resume, each with an
id, the section or project heading it sits under, and its text.

Rewrite EVERY passage so a recruiter for THIS job immediately sees the match: lead with the part most relevant to
the posting, use the posting's own terminology for things the candidate really did, and give it a new sentence
structure. Returning a passage unchanged, or with only a word or two swapped, is a failed answer.

Truthfulness rules (a rewrite that breaks one is discarded and the original kept):
- Never add a number, percentage or metric that isn't in that passage ("12+ years" -> "over 12 years" is fine).
- Only mention technologies, tools, employers, job titles and domains that appear in that passage or in
  candidate_skills. Never give the candidate the posting's job title or a title they don't have.
- Never add an outcome or benefit the passage doesn't state -- rephrase, don't embellish.
- Keep each rewrite no longer than its passage (shorter is fine); rewrites over 25% longer are discarded.
- Keep the passage's form: a first-person sentence stays first person; a "Label: description" passage keeps
  its label (you may rephrase the description); a fragment stays a fragment.

Relevance: also rate how much each passage matters to a recruiter for THIS job -- "high" (directly matches what
the job asks for), "medium" (related or transferable), or "low" (work this job plainly doesn't involve, e.g. UI
or CSS work for a pure backend role). For every "low" passage give a short "reason" (max 15 words). Summary or
profile lines are never "low". Be honest: the candidate uses "low" to decide what to cut, and they decide --
still rewrite every passage, whatever its relevance.

Respond with ONLY a JSON object: {"passages": [{"id": <int>, "text": <string>, "relevance": "high"|"medium"|"low",
"reason": <string, for low only>}, ...]} with exactly one entry for every passage id given -- never omit, merge or
invent a passage."""


_FIT_SYSTEM_PROMPT = """You are an experienced technical recruiter. You will receive JSON with a job posting and a
candidate's resume (summary, skills, employment history with accomplishments, education, certifications, projects,
and other_resume_text: resume sections that weren't parsed into fields -- treat it as part of the resume).
Judge honestly how well the candidate fits THIS job's actual requirements -- consider required vs nice-to-have
skills, years and depth of relevant experience, seniority, domain, and location/remote constraints stated in the
posting. Only credit the candidate for what the resume actually shows; never assume unstated experience.

Respond with ONLY a JSON object (no markdown, no commentary) with these keys:
- fit_score: integer 0-100 (85+ strong fit, 70-84 good, 50-69 partial, below 50 weak)
- verdict: one of "strong", "good", "partial", "weak"
- summary: 2-3 sentences explaining the overall fit, written to the candidate ("you")
- matched: array of short strings, the job requirements the resume clearly meets (most important first, max 8)
- gaps: array of short strings, requirements the resume doesn't show or only partly meets (most important first, max 8)
- recommendation: one short sentence -- whether to apply, and what to emphasize or address if so"""


_PASSAGES_PER_CALL = 40  # long project resumes go in a few calls, keeping each answer complete


class OpenAIProvider:
    """Structurally satisfies `app.integrations.ai.base.AIProvider`."""

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
        job_keywords = []
        if requirements:
            job_keywords = list(dict.fromkeys([*requirements.required_skills, *requirements.preferred_skills]))
        payload = {
            "job_title": job.title,
            "job_company": job.company,
            "job_description": job.description,
            "job_keywords": job_keywords,
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
            temperature=0.5,  # low temperatures tend to echo the source bullets back unchanged
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

    def rewrite_passages(self, passages, job, requirements, candidate_skills) -> dict[int, dict]:
        job_keywords = []
        if requirements:
            job_keywords = list(dict.fromkeys([*requirements.required_skills, *requirements.preferred_skills]))
        rewrites: dict[int, dict] = {}
        tokens_in = tokens_out = 0
        cost = 0.0
        for start in range(0, len(passages), _PASSAGES_PER_CALL):
            payload = {
                "job_title": job.title, "job_company": job.company, "job_description": job.description,
                "job_keywords": job_keywords, "candidate_skills": candidate_skills,
                "passages": passages[start:start + _PASSAGES_PER_CALL],
            }
            response = self._create_completion(
                model=self._model,
                temperature=0.5,  # low temperatures tend to echo the passages back unchanged
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": _REWRITE_SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps(payload)},
                ],
            )
            self._record_usage(response)
            if self.last_usage:
                tokens_in += self.last_usage.tokens_in or 0
                tokens_out += self.last_usage.tokens_out or 0
                cost += self.last_usage.estimated_cost or 0
            data = json.loads(response.choices[0].message.content or "{}")
            for item in data.get("passages", []):
                if isinstance(item, dict) and isinstance(item.get("id"), int) and isinstance(item.get("text"), str):
                    low = str(item.get("relevance", "")).lower() == "low"
                    reason = str(item.get("reason") or "").strip()
                    rewrites[item["id"]] = {"text": item["text"].strip(),
                                            "drop_reason": (reason or "Not relevant to this job") if low else None}
        self.last_usage = AIUsage(tokens_in=tokens_in, tokens_out=tokens_out, estimated_cost=round(cost, 6))
        return rewrites

    def assess_fit(self, candidate: dict, job: Any) -> FitAssessmentResult:
        payload = {
            "job": {
                "title": job.title, "company": job.company, "location": job.location, "description": job.description,
            },
            "candidate": candidate,
        }
        response = self._create_completion(
            model=self._model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _FIT_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(payload, default=str)},
            ],
        )
        self._record_usage(response)
        data = json.loads(response.choices[0].message.content or "{}")
        data["fit_score"] = max(0, min(100, int(data.get("fit_score") or 0)))
        return FitAssessmentResult.model_validate(data)

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
