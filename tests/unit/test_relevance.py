"""Search relevance: whole words, synonyms, and the order results are shown in."""
from __future__ import annotations

from datetime import datetime

import pytest

from app.integrations.job_boards.base import RawJob
from app.modules.jobs import relevance
from app.modules.jobs.service import _by_relevance


@pytest.mark.parametrize("term, title, expected", [
    ("hr", "Senior Security Engineer, Threat Response", relevance.NONE),  # the reported case
    ("hr", "HR Generalist", relevance.TITLE_PHRASE),
    ("hr", "Human Resources Coordinator", relevance.TITLE_PHRASE),
    ("hr", "Director, People Operations & Compliance", relevance.TITLE_PHRASE),
    ("human resources", "HR Business Partner", relevance.TITLE_PHRASE),
    ("hr", "Conseiller(ère) en ressources humaines", relevance.TITLE_PHRASE),
    ("HR Manager", "Human Resources Manager", relevance.TITLE_PHRASE),
    ("ai", "Retail Maintenance Lead", relevance.NONE),
    ("java", "JavaScript Developer", relevance.NONE),
    ("java", "Senior Java Developer", relevance.TITLE_PHRASE),
    ("data", "Database Administrator", relevance.NONE),
    ("recruit", "Technical Recruiter", relevance.TITLE_PHRASE),
    ("engineer", "Senior Engineers", relevance.TITLE_PHRASE),
    ("software engineer", "Software Developer", relevance.TITLE_PHRASE),
    ("software engineer", "Engineer, Software (Backend)", relevance.TITLE_WORDS),
    ("c++", "C++ Developer", relevance.TITLE_PHRASE),
    ("Développeur", "Developpeur Python", relevance.TITLE_PHRASE),
    ("r", "Red Team Lead", relevance.NONE),  # no endings on short words
    ("hr", "CNC Programmer - $40-$45/hr", relevance.NONE),  # a pay rate
    ("hr", "Plongeur - 20$/hr", relevance.NONE),
    ("hr", "Payroll / HR Administrator", relevance.TITLE_PHRASE),
])
def test_title_tiers(term, title, expected):
    assert relevance.tier([term], title) == expected


def test_company_and_description_tiers():
    assert relevance.tier(["shopify"], "Backend Developer", "Shopify Inc.") == relevance.COMPANY
    assert relevance.tier(["hr"], "CNC Machinist", "Fuze HR Solutions") < relevance.tier(["hr"], "HR Lead", "Acme")
    assert relevance.tier(["hr"], "Software Engineer", "Acme", "You'll partner with our HR team.") == relevance.DESCRIPTION
    assert relevance.tier(["hr", "recruiter"], "Recruiter") == relevance.TITLE_PHRASE  # the best of several terms


def test_keywords_need_whole_words():
    assert relevance.mentions(["java"], "Backend Developer", "We use Java and Kafka.")
    assert not relevance.mentions(["java"], "Frontend Developer", "We use JavaScript.")


def test_results_are_ordered_by_relevance_then_newest():
    def job(title, day, description=""):
        return RawJob(external_id=title, source="adzuna", source_url="https://x", company="Acme", title=title,
                      location="Toronto", description=description, date_posted=datetime(2026, 10, day))
    newest_first = [
        job("Security Engineer", 9, "Report to HR on incidents."),  # mentions it in passing
        job("Payroll Specialist", 8),                               # no mention at all
        job("HR Generalist", 7),
        job("Human Resources Manager", 6),
    ]
    ordered = [j.title for j in _by_relevance(newest_first, ["hr"])]
    assert ordered == ["HR Generalist", "Human Resources Manager", "Security Engineer", "Payroll Specialist"]
    assert _by_relevance(newest_first, []) == newest_first  # no title typed: newest first, unchanged
