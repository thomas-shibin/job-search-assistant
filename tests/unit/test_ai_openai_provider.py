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

from app.integrations.ai.base import ExtractedJobRequirements
from app.integrations.ai.openai_provider import OpenAIProvider
from app.modules.matching.scorer import MatchScoreBreakdown


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
    from app.modules.resumes.models import AccomplishmentBullet, CandidateProfile, Employment, Skill
    from app.modules.jobs.models import Job, JobStatus

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


def test_assess_fit_sends_job_and_resume_and_parses_result():
    payload = {
        "fit_score": 130, "verdict": "strong", "summary": "You fit well.",
        "matched": ["Java", "Spring Boot"], "gaps": ["Kubernetes"], "recommendation": "Apply.",
    }
    provider, client = _provider(_chat_response(json.dumps(payload)))
    job = SimpleNamespace(title="Backend Engineer", company="Acme", location="Remote", description="Java, K8s")

    result = provider.assess_fit({"skills": ["Java"], "employment": []}, job)

    assert result.fit_score == 100  # clamped into 0-100
    assert result.verdict == "strong" and result.gaps == ["Kubernetes"]
    sent = json.loads(client.chat.completions.last_call_kwargs["messages"][1]["content"])
    assert sent["job"]["title"] == "Backend Engineer"
    assert sent["candidate"]["skills"] == ["Java"]
    assert provider.last_usage.tokens_in == 100


class _SequenceCompletions:
    """Returns the given responses in order, recording every call."""

    def __init__(self, responses) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


def _sequence_provider(*contents):
    completions = _SequenceCompletions([_chat_response(json.dumps(c)) for c in contents])
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return OpenAIProvider(api_key="unused", model="gpt-4o-mini", client=client), completions


def _job():
    from app.modules.jobs.models import Job, JobStatus
    return Job(id=1, source="manual", source_url="https://example.com", company="Acme", title="Backend Engineer",
               description="posting text", status=JobStatus.DISCOVERED, fingerprint="fp", description_hash="dh")


def test_rewrite_passages_sends_each_passage_its_length_limit():
    passages = [{"id": 1, "heading": "Experience", "text": "Fixed bugs and did code reviews for a team of 6"}]
    provider, calls = _sequence_provider({"passages": [{"id": 1, "text": "Reviewed code and fixed bugs for a team of 6",
                                                        "relevance": "high"}]})

    rewrites = provider.rewrite_passages(passages, _job(), None, ["Java"])

    sent = json.loads(calls.calls[0]["messages"][-1]["content"])["passages"][0]
    assert sent["max_chars"] == len(passages[0]["text"]) + 30 - 4  # the validator's limit (+30 for a short line), less a margin
    assert rewrites[1]["text"] == "Reviewed code and fixed bugs for a team of 6"
    assert len(calls.calls) == 1  # nothing to redo


def test_rewrite_passages_redoes_a_rewrite_that_is_too_long_or_adds_an_outcome():
    source = "Helped move 3 services to AWS using Docker"
    passages = [{"id": 1, "heading": "Experience", "text": source},
                {"id": 2, "heading": "Experience", "text": "Built the order API in Node.js"}]
    first = {"passages": [{"id": 1, "text": "Helped move 3 services to AWS using Docker and GitHub Actions, " * 3, "relevance": "high"},
                          {"id": 2, "text": "Built the order API in Node.js, enhancing performance", "relevance": "high"}]}
    fixed = {"passages": [{"id": 1, "text": "Helped migrate 3 services to AWS with Docker"},
                          {"id": 2, "text": "Built the order REST API in Node.js"}]}
    provider, calls = _sequence_provider(first, fixed)

    rewrites = provider.rewrite_passages(passages, _job(), None, [])

    assert rewrites[1]["text"] == "Helped migrate 3 services to AWS with Docker"
    assert rewrites[2]["text"] == "Built the order REST API in Node.js"
    assert len(calls.calls) == 2
    redo = {p["id"]: p for p in json.loads(calls.calls[1]["messages"][-1]["content"])["passages"]}
    assert redo[1]["facts_from"] == source and redo[1]["remove"] == []
    assert redo[2]["remove"] == ["enhancing"]


def test_rewrite_passages_keeps_outcome_words_the_original_already_uses():
    passages = [{"id": 1, "heading": "Experience", "text": "Improved page load time by 40%"}]
    provider, calls = _sequence_provider({"passages": [{"id": 1, "text": "Improved page load times by 40%", "relevance": "high"}]})

    assert provider.rewrite_passages(passages, _job(), None, [])[1]["text"] == "Improved page load times by 40%"
    assert len(calls.calls) == 1
