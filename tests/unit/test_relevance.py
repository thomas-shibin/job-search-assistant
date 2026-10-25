"""Search relevance: whole words, synonyms, and the order results are shown in."""
from __future__ import annotations

from datetime import datetime

import pytest

from app.integrations.job_boards.base import RawJob
from app.modules.jobs import relevance
from app.modules.jobs.service import relevant_only


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


@pytest.mark.parametrize("title", ["Jr Human Resource Generalist", "*People & Culture Generalist",
                                   "Human Resources Generalist-Corporate", "Intermediate Generalist, Human Resources"])
def test_other_names_for_hr_count(title):
    assert relevance.tier(["hr generalist"], title) >= relevance.TITLE_WORDS


@pytest.mark.parametrize("title", ["HR Manager", "Plumber", "Corporate Recruiter", "Intern, Disability Management"])
def test_other_jobs_are_not_hr_generalist(title):
    assert relevance.tier(["hr generalist"], title) < relevance.RELEVANT


@pytest.mark.parametrize("title", ["Administrative Assistant", "Office Administrator", "Admin Coordinator",
                                   "Reception and Administrative Support", "Payroll / HR Administrator"])
def test_admin_administrative_and_administrator_are_one_search(title):
    # A member searching "administrator" saw only IT jobs: "Administrative Assistant" didn't count.
    for term in ("administrator", "admin", "administrative"):
        assert relevance.tier([term], title) == relevance.TITLE_PHRASE


@pytest.mark.parametrize("title", ["Receptionist", "Office Manager", "Executive Assistant", "Front Desk Agent"])
def test_office_admin_searches_find_the_usual_titles_a_tier_below(title):
    assert relevance.tier(["administrator"], title) == relevance.TITLE_WORDS
    assert relevance.tier(["administrative assistant"], title) < relevance.RELEVANT  # only a one-word search


@pytest.mark.parametrize("title", ["Systems Administrator", "Senior Linux System Administrator", "Salesforce Admin",
                                   "Microsoft 365 Administrator", "IT Administrator", "Database Administrator",
                                   "Jira and Confluence Administrator", "Program Manager - Jira Administration"])
def test_it_administrators_are_not_office_admin_work(title):
    assert relevance.tier(["administrator"], title) < relevance.RELEVANT
    assert relevance.tier(["admin assistant"], title) < relevance.RELEVANT


def test_naming_the_system_still_finds_it_administrators():
    assert relevance.tier(["systems administrator"], "Systems Administrator") == relevance.TITLE_PHRASE
    assert relevance.tier(["salesforce admin"], "Salesforce Administrator") == relevance.TITLE_PHRASE
    assert relevance.tier(["it administrator"], "IT Administrator") == relevance.TITLE_PHRASE


def test_company_and_description_tiers():
    assert relevance.tier(["shopify"], "Backend Developer", "Shopify Inc.") == relevance.COMPANY
    assert relevance.tier(["hr"], "CNC Machinist", "Fuze HR Solutions") < relevance.RELEVANT  # a word in a name
    assert relevance.tier(["hr generalist"], "Plumber", "Acme", "Apply to our HR generalist.") < relevance.RELEVANT
    assert relevance.tier(["hr"], "Software Engineer", "Acme", "You'll partner with our HR team.") == relevance.DESCRIPTION
    assert relevance.tier(["hr", "recruiter"], "Recruiter") == relevance.TITLE_PHRASE  # the best of several terms


def test_keywords_need_whole_words():
    assert relevance.mentions(["java"], "Backend Developer", "We use Java and Kafka.")
    assert not relevance.mentions(["java"], "Frontend Developer", "We use JavaScript.")


def test_only_relevant_results_are_shown_best_first_then_newest():
    def job(title, day, description=""):
        return RawJob(external_id=title, source="adzuna", source_url="https://x", company="Acme", title=title,
                      location="Toronto", description=description, date_posted=datetime(2026, 10, day))
    newest_first = [
        job("Security Engineer", 9, "Report to HR on incidents."),  # mentions it in passing
        job("Payroll Specialist", 8),                               # no mention at all
        job("HR Generalist", 7),
        job("Human Resources Manager", 6),
    ]
    kept, hidden = relevant_only(newest_first, ["hr"])
    assert [j.title for j in kept] == ["HR Generalist", "Human Resources Manager"]
    assert hidden == 2  # the passing mention and the posting without one
    assert relevant_only(newest_first, []) == (newest_first, 0)  # no title typed: everything, newest first


@pytest.mark.parametrize("typed, titles", [
    ("HR and Admin", ["HR", "Admin"]),
    ("HR & Admin", ["HR", "Admin"]),
    ("HR&Admin", ["HR", "Admin"]),
    ("HR / Admin", ["HR", "Admin"]),
    ("Sales and Marketing Manager", ["Sales Manager", "Marketing Manager"]),  # the job word is shared
    ("UX/UI Designer", ["UX Designer", "UI Designer"]),
    ("Health and Safety Officer", ["Health and Safety Officer"]),  # one field
    ("Research & Development", ["Research & Development"]),
    ("R&D Engineer", ["R&D Engineer"]),  # initials
    ("24/7 support", ["24/7 support"]),
    ("Data Analyst", ["Data Analyst"]),
])
def test_two_jobs_in_one_entry_are_two_searches(typed, titles):
    assert relevance.split_titles(typed) == titles


def test_hr_and_admin_finds_both_kinds_of_job():
    titles = relevance.split_titles("HR and Admin")
    for title in ("HR Generalist", "Human Resources Advisor", "Administrative Assistant", "Office Administrator",
                  "Receptionist"):
        assert relevance.tier(titles, title) >= relevance.RELEVANT, title
    assert relevance.tier(titles, "Systems Administrator") < relevance.RELEVANT
    assert relevance.tier(["HR and Admin"], "HR Generalist") < relevance.RELEVANT  # why one entry found nothing
