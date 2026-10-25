"""One copy of each job, from the most reliable source, and nothing too old
(app/modules/jobs/dedupe.py)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.integrations.job_boards.base import RawJob
from app.modules.jobs import dedupe


def _job(source="adzuna", company="Maple Dental Inc.", title="Office Administrator", location="Toronto, ON",
         ext="1", days_old=1):
    return RawJob(source=source, external_id=ext, source_url=f"https://x/{source}/{ext}", company=company,
                  title=title, location=location, description="d",
                  date_posted=datetime.now(timezone.utc) - timedelta(days=days_old))


def test_the_same_company_title_and_city_is_one_job_whatever_the_wording():
    assert dedupe.job_key(_job()) == dedupe.job_key(
        _job(company="MAPLE DENTAL", title="Office administrator", location="Toronto, Ontario, Canada"))
    assert dedupe.city("Remote - Canada") == ""
    assert dedupe.city("Hybrid - Vancouver, BC") == "vancouver"
    assert dedupe.job_key(_job()) != dedupe.job_key(_job(location="Ottawa, ON"))  # another city: another job


def test_the_most_reliable_copy_is_kept_in_the_first_ones_place():
    board, second, company = _job(ext="a"), _job(source="jooble", ext="j"), _job(source="greenhouse", ext="g")
    other = _job(title="Receptionist", ext="r")

    kept = dedupe.unique([second, other, board, company])

    assert [j.external_id for j in kept] == ["g", "r"]


def test_too_old_is_judged_by_the_posting_date_only():
    assert dedupe.too_old(_job(days_old=31), 30)
    assert not dedupe.too_old(_job(days_old=29), 30)
    undated = _job()
    undated.date_posted = None
    assert not dedupe.too_old(undated, 30)
    assert not dedupe.too_old(_job(days_old=400), None)


class _Board:
    def __init__(self, jobs):
        self.jobs, self.filters = jobs, []

    def search_page(self, *, what, where, page, per_page=50, area=(), what_or=None, filters=None):
        self.filters.append(dict(filters or {}))
        return self.jobs


def test_board_searches_ask_for_recent_ads_and_drop_old_and_repeated_ones():
    from app.modules.jobs.search import search_jobs

    board = _Board([_job(ext="1", days_old=2), _job(ext="2", days_old=3),  # the same ad, listed twice
                    _job(ext="3", title="Receptionist", days_old=45)])     # older than asked
    found = search_jobs(board, ["admin"], ["Toronto"], use_cache=False, max_age_days=30)

    assert [j.external_id for j in found.jobs] == ["1"]
    assert board.filters[0]["max_days_old"] == "30"
    search_jobs(board, ["admin"], ["Toronto"], use_cache=False, max_age_days=30, filters={"max_days_old": "7"})
    assert board.filters[1]["max_days_old"] == "7"  # a narrower "posted within" wins


def test_merged_results_keep_the_company_copy_and_the_same_title_elsewhere():
    from app.modules.jobs.service import _merge_results

    company = _job(source="workable", ext="w")
    merged = _merge_results([company], [_job(ext="a"), _job(ext="o", location="Ottawa, ON")])

    assert sorted(j.external_id for j in merged) == ["o", "w"]
