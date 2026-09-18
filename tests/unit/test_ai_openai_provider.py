"""Tests for `OpenAIProvider` -- the real OpenAI-backed `AIProvider`
implementation (Milestone 5). No network call happens in these tests: the
OpenAI client is replaced with a stub whose `.chat.completions.create`
returns a canned response, so only this module's own request-building and
response-parsing logic is under test.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.ai.base import ExtractedJobRequirements
from app.ai.openai_provider import OpenAIProvider
from app.matching.scorer import MatchScoreBreakdown


class _StubCompletions:
    def __init__(self, response) -> None:
        self._response = response
        self.last_call_kwargs: dict | None = None

    def create(self, **kwargs):
        self.last_call_kwargs = kwargs
        return self._response


class _StubChat:
    def __init__(self, response) -> None:
        self.completions = _StubCompletions(response)


class _StubClient:
    def __init__(self, response) -> None:
        self.chat = _StubChat(response)


def _chat_response(content: str, prompt_tokens: int = 100, completion_tokens: int = 20):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens),
    )


def _provider(response) -> tuple[OpenAIProvider, _StubClient]:
    client = _StubClient(response)
    provider = OpenAIProvider(api_key="unused", model="gpt-4o-mini", client=client)
    return provider, client


def test_extract_job_requirements_parses_json_response():
    payload = {
        "required_skills": ["Java", "Spring Boot"],
        "preferred_skills": ["AWS"],
        "seniority_level": "senior",
        "min_years_experience": 5,
        "remote_type_detected": "remote",
    }
    provider, _ = _provider(_chat_response(json.dumps(payload)))

    result = provider.extract_job_requirements("Senior Backend Engineer posting text")

    assert isinstance(result, ExtractedJobRequirements)
    assert result.required_skills == ["Java", "Spring Boot"]
    assert result.preferred_skills == ["AWS"]
    assert result.seniority_level == "senior"
    assert result.min_years_experience == 5
    assert result.remote_type_detected == "remote"


def test_extract_job_requirements_sends_job_description_and_json_response_format():
    provider, client = _provider(_chat_response("{}"))

    provider.extract_job_requirements("some posting text")

    kwargs = client.chat.completions.last_call_kwargs
    assert kwargs["response_format"] == {"type": "json_object"}
    assert kwargs["messages"][-1]["content"] == "some posting text"


def test_extract_job_requirements_records_usage_and_estimated_cost():
    provider, _ = _provider(_chat_response("{}", prompt_tokens=1000, completion_tokens=500))

    provider.extract_job_requirements("some posting text")

    assert provider.last_usage.tokens_in == 1000
    assert provider.last_usage.tokens_out == 500
    assert provider.last_usage.estimated_cost is not None
    assert provider.last_usage.estimated_cost > 0


def test_generate_match_explanation_returns_stripped_content():
    breakdown = MatchScoreBreakdown(
        overall_score=80, technical_score=80, experience_score=100, title_score=80,
        seniority_score=100, platform_score=100, location_score=100, domain_score=50,
        education_score=100, explanation="Strong match on Java.",
    )
    provider, _ = _provider(_chat_response("  You're a strong match on Java.  "))

    result = provider.generate_match_explanation(breakdown)

    assert result == "You're a strong match on Java."


def test_extract_job_requirements_retries_on_transient_connection_error():
    import httpx
    from openai import APIConnectionError

    call_count = {"n": 0}
    payload = {"required_skills": ["Java"]}
    fake_request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")

    class _FlakyCompletions:
        def create(self, **kwargs):
            call_count["n"] += 1
            if call_count["n"] < 2:
                raise APIConnectionError(request=fake_request)
            return _chat_response(json.dumps(payload))

    class _FlakyChat:
        completions = _FlakyCompletions()

    class _FlakyClient:
        chat = _FlakyChat()

    provider = OpenAIProvider(api_key="unused", model="gpt-4o-mini", client=_FlakyClient())

    result = provider.extract_job_requirements("posting text")

    assert result.required_skills == ["Java"]
    assert call_count["n"] == 2


def test_tailor_resume_parses_json_response_and_sends_candidate_bullets():
    from app.models.candidate import AccomplishmentBullet, CandidateProfile, Employment, Skill
    from app.models.job import Job, JobStatus

    candidate = CandidateProfile(raw_resume_path="x.docx", source_hash="h", summary="Backend engineer.")
    candidate.employments = [
        Employment(id=1, company="Acme", title="Engineer", bullets=[AccomplishmentBullet(id=7, text="Did X", order_index=0)])
    ]
    candidate.skills = [Skill(name="Java")]
    job = Job(
        id=1, source="manual", source_url="https://example.com", company="Acme", title="Backend Engineer",
        description="posting text", status=JobStatus.DISCOVERED, fingerprint="fp", description_hash="dh",
    )

    payload = {
        "summary": "Tailored summary.",
        "skills_order": ["Java"],
        "bullets": [{"source_bullet_id": 7, "text": "Did X, tailored"}],
    }
    provider, client = _provider(_chat_response(json.dumps(payload)))

    result = provider.tailor_resume(candidate, job, None, None)

    assert result.summary == "Tailored summary."
    assert result.skills_order == ["Java"]
    assert result.bullets[0].source_bullet_id == 7

    sent_content = client.chat.completions.last_call_kwargs["messages"][-1]["content"]
    sent_payload = json.loads(sent_content)
    assert sent_payload["candidate_bullets"] == [{"id": 7, "employment": "Engineer @ Acme", "text": "Did X"}]
