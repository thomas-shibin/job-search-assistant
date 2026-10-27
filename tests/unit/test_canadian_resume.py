"""The Canadian resume check's rules (app/modules/resumes/canadian.py) and the
title guard (canadian_convert.py): what's flagged, and what each fix changes."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.modules.resumes import canadian
from app.modules.resumes.canadian import Line, ResumeDocument
from app.modules.resumes.canadian_convert import _acceptable, clean_title

SAMPLE = Path(__file__).resolve().parents[1] / "fixtures" / "sample_resume.docx"


def doc(*lines: tuple[str, str] | str, pages: int = 1, photos: int = 0) -> ResumeDocument:
    """Lines as "text" (header) or (section, text); a section's first line is its heading."""
    out, seen = [], set()
    for line in lines:
        section, text = ("header", line) if isinstance(line, str) else line
        heading = section != "header" and section not in seen
        seen.add(section)
        out.append(Line(text, heading, section))
    return ResumeDocument(out, photos, pages, True)


def keys(findings) -> list[str]:
    return [f.key for f in findings]


@pytest.mark.parametrize("text, expected", [
    ("Organised the organisation's colour scheme", "Organized the organization's colour scheme"),
    ("Improved color contrast and behavior tracking", "Improved colour contrast and behaviour tracking"),
    ("Traveled to the data center; canceled 3 vendor contracts", "Travelled to the data centre; cancelled 3 vendor contracts"),
    ("• Analysed customer behaviour", "• Analyzed customer behaviour"),
    ("Volunteer at Kennedy Space Center and the World Health Organisation", None),  # names keep their spelling
    ("Specialist in analyses, emphasis on synthesis", None),  # nouns that are spelled alike
    ("Portfolio: www.colorlab.com/organise and j.gray@example.com", None),
    ("ORGANISATION CHART", None),
])
def test_respell(text, expected):
    new, changed = canadian.respell(text)
    assert new == (expected or text)
    assert bool(changed) == bool(expected)


@pytest.mark.parametrize("text, expected, unclear", [
    ("HR Executive | 03/2019 - 05/2023", "HR Executive | Mar 2019 - May 2023", []),
    ("Joined 25/03/2019", "Joined Mar 2019", []),
    ("Joined 03/25/2019", "Joined Mar 2019", []),
    ("Joined 05/04/2019", "Joined 05/04/2019", ["05/04/2019"]),  # April 5 or May 4?
    ("Diploma (2015-2018), academic year 2010-11", "Diploma (2015-2018), academic year 2010-11", []),
    ("Jan 2021 - Present", "Jan 2021 - Present", []),
])
def test_redate(text, expected, unclear):
    new, _, left = canadian.redate(text)
    assert new == expected and left == unclear


def test_redate_matches_full_month_names_when_the_resume_uses_them():
    lines = doc(("experience", "Experience"), ("experience", "Clerk, ABC | January 2019 - March 2020"),
                ("experience", "Analyst, XYZ | 04/2020 - Present")).lines
    assert canadian.redate("04/2020 - Present", canadian.month_style(lines))[0] == "April 2020 - Present"


@pytest.mark.parametrize("text, expected", [
    ("priya@example.com | DOB: 12/05/1995 | Pune, India", "priya@example.com | Pune, India"),
    ("Date of Birth: 12/05/1995", None),
    ("Marital Status: Married\tNationality: Indian", None),
    ("Civil Status: Single", None),  # the Philippines' word for it
    ("Languages: English, Hindi", "Languages: English, Hindi"),
    ("Citizenship: Canadian", "Citizenship: Canadian"),  # saying you can work here is fine
    ("Expected salary: 12 LPA", None),
])
def test_without_personal(text, expected):
    assert canadian.without_personal(text)[0] == expected


def test_a_canadian_resume_has_nothing_to_fix():
    findings = canadian.check(canadian.read(SAMPLE))
    assert [f for f in findings if f.level != canadian.TIP] == []


def test_photo_personal_details_and_declaration_are_flagged_and_planned():
    resume = doc(
        "Priya Sharma", "priya@example.com | +91 98765 43210 | Toronto, ON",
        ("experience", "Experience"), ("experience", "Executive - HR, Tata Motors | 03/2019 - 05/2023"),
        ("personal", "Personal Details"), ("personal", "Date of Birth: 12/05/1995"), ("personal", "Married"),
        ("declaration", "Declaration"),
        ("declaration", "I hereby declare that the above information is true to the best of my knowledge."),
        photos=1)
    findings = canadian.check(resume)
    assert keys(findings)[:3] == ["photo", "personal", "declaration"]
    assert all(f.level == canadian.MUST for f in findings[:2])
    assert "date of birth" in findings[1].advice and "12/05/1995" not in str(next(f for f in findings if f.key == "dates").unclear)

    plan = canadian.plan(resume, {canadian.PHOTO, canadian.PERSONAL, canadian.DECLARATION, canadian.DATES},
                         {"Executive - HR": "HR Coordinator"})
    assert plan.photos
    removed = sorted(resume.lines[i].text for i, new in plan.edits.items() if new is None)
    assert removed == sorted(["Personal Details", "Date of Birth: 12/05/1995", "Married", "Declaration",
                              "I hereby declare that the above information is true to the best of my knowledge."])
    assert plan.edits[3] == "Executive - HR (HR Coordinator), Tata Motors | Mar 2019 - May 2023"


def test_a_personal_section_heading_stays_while_something_useful_is_left():
    resume = doc("Priya Sharma", ("personal", "Personal Details"), ("personal", "Date of Birth: 12/05/1995"),
                 ("personal", "Languages: English, Hindi, French"))
    plan = canadian.plan(resume, {canadian.PERSONAL})
    assert plan.edits == {2: None}


def test_unchosen_fixes_change_nothing():
    resume = doc("Priya Sharma", ("experience", "Experience"), ("experience", "Organised hiring | 03/2019 - 05/2023"),
                 ("personal", "Personal Details"), ("personal", "Date of Birth: 12/05/1995"), photos=1)
    plan = canadian.plan(resume, {canadian.SPELLING})
    assert plan.edits == {2: "Organized hiring | 03/2019 - 05/2023"} and not plan.photos


def test_references_listed_or_on_request():
    listed = doc("Priya", ("references", "References"), ("references", "Mr. A Kumar, HR Manager, +91 99999 11111"))
    on_request = doc("Priya", ("experience", "Experience"), ("experience", "References available upon request"))
    assert next(f for f in canadian.check(listed) if f.key == "references").level == canadian.SHOULD
    assert next(f for f in canadian.check(on_request) if f.key == "references").level == canadian.TIP
    assert canadian.plan(on_request, {canadian.REFERENCES}).edits == {2: None}
    character = doc("Maria", ("references", "Character References"), ("references", "R. Dela Cruz, +63 917 555 0123"))
    assert next(f for f in canadian.check(character) if f.key == "references").level == canadian.SHOULD


def test_high_school_goes_only_when_a_degree_stays():
    with_degree = doc("Priya", ("education", "Education"),
                      ("education", "B.Tech in Mechanical Engineering, Pune University, 2018"),
                      ("education", "Higher Secondary Certificate (HSC), 2014, 85%"),
                      ("education", "ABC Junior College, Maharashtra Board"))
    alone = doc("Priya", ("education", "Education"), ("education", "Higher Secondary Certificate (HSC), 2014"))
    assert canadian.plan(with_degree, {canadian.HIGH_SCHOOL}).edits == {3: None, 4: None}
    assert "high_school" not in keys(canadian.check(alone))


def test_contact_tips_and_length():
    findings = canadian.check(doc("Priya Sharma", "12 MG Road, Pune | +91 98765 43210",
                                  ("experience", "Experience"), pages=4))
    by_key = {f.key: f for f in findings}
    assert by_key["length"].level == canadian.MUST and "about 4" in by_key["length"].title
    assert {"location", "phone"} <= set(by_key)
    assert "address" not in by_key  # Job Bank's own advice is to list your address
    assert by_key["phone"].lines == [1]
    assert not by_key["location"].fixable  # nothing to fill in: the member knows where they are


@pytest.mark.parametrize("original, suggestion, ok", [
    ("Senior Executive - HR", "HR Generalist", True),
    ("Senior Executive - HR", "Senior HR Generalist", True),
    ("Senior Executive - HR", "HR Manager", False),  # sounds more senior
    ("Associate Vice President", "Senior Manager", True),
    ("Vice President, Operations", "President", False),
    ("Deputy Manager - Accounts", "Accounting Manager", True),
    ("Accountant", "accountant", False),  # no change
])
def test_title_suggestions_never_sound_more_senior(original, suggestion, ok):
    assert _acceptable(original, clean_title(suggestion)) is ok


def test_clean_title():
    assert clean_title("  HR Generalist (Canada).  ") == "HR Generalist Canada"
    assert len(clean_title("x" * 200)) == 60


def test_dates_on_a_line_that_is_taken_out_are_not_offered_as_a_change():
    resume = doc("Maria Santos", ("experience", "Work Experience"), ("experience", "Accountant, SGV | 16/06/2015 - 15/12/2018"),
                 ("personal", "Personal Information"), ("personal", "Date of Birth: 14/08/1992"))
    offered = canadian.changes(resume)
    assert [c.line for c in offered[canadian.DATES]] == [2]
    assert [c.line for c in offered[canadian.PERSONAL]] == [3, 4]  # the heading goes with its only line
