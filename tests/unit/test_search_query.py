"""Smart search's rules (app/modules/jobs/query.py): a typed job search read into
titles, places and the results filters, with spelling fixed."""
from __future__ import annotations

import pytest

from app.modules.jobs.query import Vocabulary, fold, read

TITLES = [
    "HR Generalist", "HR Manager", "Human Resources Business Partner", "Recruiter", "Senior Recruiter",
    "Technical Recruiter", "Software Engineer", "Senior Software Engineer", "Data Analyst", "Data Entry Clerk",
    "Marketing Coordinator", "Developer", "Backend Developer", "Accountant", "Contract Manager", "Head of Sales",
    "Sales Representative", "Class 1 Driver", "Nurse", "Mission Specialist",
]
PLACES = [
    ("Toronto, ON", 900), ("Vancouver, BC", 500), ("Victoria, BC", 0), ("Victoria region, NB", 40),
    ("Calgary region, AB", 300), ("Montréal, QC", 400), ("Mission, BC", 5), ("Ontario", 2000),
]


@pytest.fixture(scope="module")
def vocab() -> Vocabulary:
    texts = [(t, 1) for t in TITLES] + [("Recruteur", 1), ("Recruiter", 5)]
    return Vocabulary.build(texts, PLACES, job_terms=TITLES)


def test_a_sentence_becomes_titles_places_and_filters(vocab):
    r = read("remote HR jobs over 80k in Toronto posted this week", vocab)

    assert r.titles == ["HR"]
    assert r.locations == ["Toronto, ON"]
    assert r.workplaces == ["remote"]
    assert r.min_salary == 80_000
    assert r.days == 7
    assert not r.same_as("remote HR jobs over 80k in Toronto posted this week")


@pytest.mark.parametrize("text", ["HR", "Software Engineer, Data Analyst", "Head of Sales", "Contract Manager",
                                  "Data Entry Clerk", "Class 1 Driver", "Senior Recruiter"])
def test_plain_titles_are_left_alone(vocab, text):
    assert read(text, vocab).same_as(text)


def test_a_place_typed_in_the_title_box_becomes_the_location(vocab):
    r = read("Data analyst, Toronto", vocab)

    assert (r.titles, r.locations) == (["Data analyst"], ["Toronto, ON"])


@pytest.mark.parametrize("text, place", [
    ("Montreal", "Montréal, QC"),  # accents don't matter
    ("Victoria", "Victoria, BC"),  # the name as written beats "Victoria region"
    ("nurse in calgary", "Calgary region, AB"),
    ("accountant toronto, on", "Toronto, ON"),
])
def test_places(vocab, text, place):
    assert read(text, vocab).locations == [place]


def test_a_place_that_is_also_a_job_word_needs_in(vocab):
    assert read("Mission Specialist", vocab).locations == []
    assert read("nurse in Mission", vocab).locations == ["Mission, BC"]


@pytest.mark.parametrize("text, expected", [
    ("contract jobs", ["contract"]),
    ("Contract Manager", []),  # a title, not a job type
    ("part-time sales representative", ["part_time"]),
    ("marketing internship", ["internship"]),
    ("temp jobs in Toronto", ["temporary"]),
    ("full time accountant", ["full_time"]),
])
def test_job_types(vocab, text, expected):
    assert read(text, vocab).employment_types == expected


def test_junior_and_entry_level_are_the_level_filter_but_senior_stays_in_the_title(vocab):
    junior = read("entry level marketing jobs", vocab)
    senior = read("senior recruiter", vocab)

    assert (junior.titles, junior.levels) == (["marketing"], ["entry"])
    assert (senior.titles, senior.levels) == (["senior recruiter"], [])
    assert read("data entry clerk", vocab).levels == []


@pytest.mark.parametrize("text, salary", [
    ("accountant $80,000", 80_000),
    ("accountant 90k+", 90_000),
    ("accountant 70-90k", 70_000),
    ("sales representative $25/hr", 52_000),  # hourly, as a year
    ("Class 1 driver", None),  # a bare small number isn't pay
    ("accountant with 5 years", None),
])
def test_salary(vocab, text, salary):
    assert read(text, vocab).min_salary == salary


@pytest.mark.parametrize("text, days", [
    ("nurse posted today", 1), ("nurse last 3 days", 3), ("nurse past 2 weeks", 14), ("nurse this month", 30),
    ("nurse last 10 days", 14),  # rounded up to the filter's next choice
])
def test_date_posted(vocab, text, days):
    assert read(text, vocab).days == days


def test_spelling_is_fixed_towards_the_commoner_word(vocab):
    r = read("senior software enginer in vancouver", vocab)

    assert r.titles == ["senior software engineer"]
    assert r.locations == ["Vancouver, BC"]
    assert r.corrections == [("enginer", "engineer")]
    assert read("recruter", vocab).titles == ["recruiter"]  # not the rarer "recruteur"


def test_a_misspelled_place_becomes_the_location(vocab):
    r = read("Toronot accountant", vocab)

    assert (r.titles, r.locations) == (["accountant"], ["Toronto, ON"])


def test_short_words_and_abbreviations_are_never_corrected(vocab):
    r = read("hr", vocab)

    assert (r.titles, r.corrections, r.unknown) == (["hr"], [], [])
    assert read("qa", vocab).unknown == []  # a known abbreviation


def test_words_nobody_uses_are_reported_not_guessed(vocab):
    r = read("asdkjh qwe", vocab)

    assert r.unknown == ["asdkjh", "qwe"]
    assert r.corrections == []
    assert read("something with spreadsheets", vocab).unknown == ["spreadsheets"]


def test_or_splits_titles_and_framing_words_are_dropped(vocab):
    r = read("looking for marketing or sales jobs", vocab)

    assert r.titles == ["marketing", "sales"]


def test_fold_keeps_positions():
    assert fold("Montréal İstanbul") == "montreal istanbul"
    assert len(fold("Montréal İstanbul")) == len("Montréal İstanbul")


@pytest.mark.parametrize("text, km", [
    ("nurse within 60 km of Victoria", 60),
    ("nurse 25km from Toronto", 25),
    ("accountant within 30 miles of Vancouver", 48),  # miles, as km
    ("nurse within 900 km of Toronto", 200),  # capped
])
def test_a_distance_becomes_the_radius(vocab, text, km):
    r = read(text, vocab)

    assert r.radius_km == km
    assert r.titles in (["nurse"], ["accountant"])
    assert len(r.locations) == 1
    assert r.min_salary is None  # "60 km" isn't pay


def test_class_1_and_years_are_not_distances(vocab):
    assert read("Class 1 driver", vocab).radius_km is None
