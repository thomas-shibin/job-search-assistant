"""Tests for Milestone 10 notifications: the Windows toast provider (with a
fake toaster injected -- no real OS notification is shown) and the
minimum-score gate that decides whether a qualified job is worth notifying.
"""
from __future__ import annotations

from app.config.schema import AppConfig, CandidateConfig, NotificationsConfig
from app.matching.scorer import MatchScoreBreakdown
from app.models.job import Job, JobStatus
from app.notifications.service import maybe_notify_qualified_job
from app.notifications.windows_toast import WindowsToastNotificationProvider


class _FakeToaster:
    def __init__(self):
        self.calls = []

    def show_toast(self, title, message, **kwargs):
        self.calls.append((title, message))


class _FailingToaster:
    def show_toast(self, *args, **kwargs):
        raise OSError("no notification service available")


def _job() -> Job:
    return Job(
        id=1, source="manual", source_url="https://example.com", company="Acme", title="Senior Engineer",
        location="Victoria, BC", description="d", status=JobStatus.QUALIFIED, fingerprint="fp", description_hash="dh",
    )


def _breakdown(score: int) -> MatchScoreBreakdown:
    return MatchScoreBreakdown(
        overall_score=score, technical_score=score, experience_score=100, title_score=100,
        seniority_score=100, platform_score=100, location_score=100, domain_score=100, education_score=100,
        strong_matches=["Java"], explanation="x",
    )


def test_windows_toast_provider_calls_show_toast():
    toaster = _FakeToaster()
    provider = WindowsToastNotificationProvider(toaster=toaster)

    from app.notifications.base import JobNotification

    provider.notify(JobNotification(job_id=1, title="Engineer", company="Acme", overall_score=90))

    assert len(toaster.calls) == 1
    assert "Acme" in toaster.calls[0][1]


def test_windows_toast_provider_swallows_toast_failures():
    provider = WindowsToastNotificationProvider(toaster=_FailingToaster())
    from app.notifications.base import JobNotification

    provider.notify(JobNotification(job_id=1, title="Engineer", company="Acme", overall_score=90))  # must not raise


def test_notify_skipped_when_below_minimum_score():
    toaster = _FakeToaster()
    provider = WindowsToastNotificationProvider(toaster=toaster)
    config = AppConfig(candidate=CandidateConfig(name="X", base_resume="x.docx"), notifications=NotificationsConfig(minimum_score=80))

    maybe_notify_qualified_job(_job(), _breakdown(70), config, provider)

    assert toaster.calls == []


def test_notify_sent_when_at_or_above_minimum_score():
    toaster = _FakeToaster()
    provider = WindowsToastNotificationProvider(toaster=toaster)
    config = AppConfig(candidate=CandidateConfig(name="X", base_resume="x.docx"), notifications=NotificationsConfig(minimum_score=80))

    maybe_notify_qualified_job(_job(), _breakdown(85), config, provider)

    assert len(toaster.calls) == 1


def test_notify_no_provider_is_a_noop():
    config = AppConfig(candidate=CandidateConfig(name="X", base_resume="x.docx"))
    maybe_notify_qualified_job(_job(), _breakdown(95), config, None)  # must not raise
