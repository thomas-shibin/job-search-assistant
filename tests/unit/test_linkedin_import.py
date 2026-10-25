"""Reading a member's own LinkedIn files: the profile PDF and the data export
(ZIP or its CSVs). The PDF fixture is a made-up person in LinkedIn's layout."""
from __future__ import annotations

import io
import zipfile
from datetime import date
from pathlib import Path

import pytest

from app.modules.profiles import linkedin
from app.modules.profiles.linkedin import LinkedInImportError, parse_date, read_files

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "linkedin_profile_sample.pdf"

PROFILE = (b"First Name,Last Name,Maiden Name,Address,Birth Date,Headline,Summary,Industry,Zip Code,Geo Location\n"
           b'Maya,Okafor,,,,"Data Analyst | Clear decisions","Six years in retail.",IT,,"Vancouver, British Columbia, Canada"\n')
POSITIONS = (b"Company Name,Title,Description,Location,Started On,Finished On\n"
             b'Northwind Logistics,Senior Data Analyst,"Built dashboards.\nCut prep time.","Vancouver, BC",Mar 2022,\n'
             b"Maple Grocers,Junior Analyst,,Burnaby,Jun 2017,Aug 2019\n")
EDUCATION = (b"School Name,Start Date,End Date,Notes,Degree Name,Activities\n"
             b"University of British Columbia,2013,2017,,\"Bachelor of Science - BS, Statistics\",\n")
SKILLS = b"Name\nSQL\nPower BI\nsql\n"


def _zip(files: dict[str, bytes], folder: str = "") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in files.items():
            archive.writestr(folder + name, data)
    return buffer.getvalue()


@pytest.mark.parametrize("text, expected", [
    ("Jan 2020", date(2020, 1, 1)), ("January 2020", date(2020, 1, 1)), ("Sept. 2015", date(2015, 9, 1)),
    ("2018", date(2018, 1, 1)), ("03/2021", date(2021, 3, 1)), ("2021-11", date(2021, 11, 1)),
    ("6/15/2020", date(2020, 6, 1)), ("", None), ("Present", None), ("13/2020", None), ("1820", None), ("soon", None),
])
def test_dates_in_every_format_linkedin_uses(text, expected):
    assert parse_date(text) == expected


def test_data_export_zip_is_read_completely():
    data = read_files([("Basic_LinkedInDataExport.zip", _zip({
        "Profile.csv": PROFILE, "Positions.csv": POSITIONS, "Education.csv": EDUCATION, "Skills.csv": SKILLS,
        "messages.csv": b"From,To\nx,y\n"}))])
    assert (data.first_name, data.headline, data.location) == (
        "Maya", "Data Analyst | Clear decisions", "Vancouver, British Columbia, Canada")
    senior, junior = data.positions
    assert senior.is_current and senior.end is None and senior.start == date(2022, 3, 1)
    assert senior.description == "Built dashboards.\nCut prep time."  # line breaks inside a quoted cell survive
    assert not junior.is_current and junior.end == date(2019, 8, 1)
    school, = data.schools
    assert (school.degree, school.field_of_study, school.end) == ("Bachelor of Science - BS", "Statistics", date(2017, 1, 1))
    assert data.skills == ["SQL", "Power BI", "sql"]  # de-duplicated when saved, not here


def test_export_quirks_bom_notes_lines_nested_folder_and_windows_encoding():
    notes = "Notes:\nThis file lists your positions\n\n".encode()
    positions = notes + "﻿Company Name,Title,Started On,Finished On\nCafé Québec,Barista,2016,2017\n".encode("cp1252",
                                                                                                                    errors="ignore")
    data = read_files([("export.zip", _zip({"Positions.csv": positions}, folder="Complete_LinkedInDataExport/"))])
    assert data.positions[0].company == "Café Québec" and data.positions[0].start == date(2016, 1, 1)


def test_csv_files_uploaded_on_their_own():
    data = read_files([("Positions.csv", POSITIONS), ("Skills.csv", SKILLS)])
    assert len(data.positions) == 2 and data.skills[:2] == ["SQL", "Power BI"]


def test_end_date_before_start_keeps_the_start_only():
    data = read_files([("Positions.csv", b"Company Name,Title,Started On,Finished On\nAcme,Clerk,May 2020,Jan 2019\n")])
    assert data.positions[0].start == date(2020, 5, 1) and data.positions[0].end is None
    assert not data.positions[0].is_current


def test_profile_pdf_in_linkedins_layout():
    data = read_files([("Profile.pdf", FIXTURE.read_bytes())])
    assert (data.first_name, data.last_name) == ("Maya", "Okafor")
    assert data.headline == "Data Analyst | Turning messy data into clear decisions"
    assert data.location == "Vancouver, British Columbia, Canada"
    assert data.skills == ["SQL", "Power BI", "Stakeholder Management"]
    senior, analyst, junior = data.positions
    # Two roles at one company: the second keeps the company, not a description line.
    assert (senior.company, analyst.company, junior.company) == ("Northwind Logistics",) * 2 + ("Maple Grocers",)
    assert senior.is_current and senior.location == "Vancouver, British Columbia, Canada"
    assert senior.description.startswith("• Built the weekly") and "\n• Cut report" in senior.description
    assert (analyst.start, analyst.end, analyst.location) == (date(2019, 9, 1), date(2022, 2, 1), None)
    assert junior.description == "Weekly sales reporting in Excel and SQL."
    ubc, bcit = data.schools
    assert (ubc.institution, ubc.degree, ubc.field_of_study) == (
        "University of British Columbia", "Bachelor of Science - BS", "Statistics")
    assert (bcit.institution, bcit.start, bcit.end) == ("BCIT", date(2018, 1, 1), date(2019, 1, 1))


def test_pdf_plus_export_prefers_the_export():
    data = read_files([("Profile.pdf", FIXTURE.read_bytes()), ("Skills.csv", SKILLS)])
    assert data.skills == ["SQL", "Power BI", "sql"]  # the export's full list, not the PDF's top 3
    assert len(data.positions) == 3  # the export had none, so the PDF's fill in


@pytest.mark.parametrize("name, data, message", [
    ("resume.docx", b"PK\x03\x04garbage", "damaged"),
    ("notes.txt", b"hello", "Upload the PDF of your LinkedIn profile"),
    ("photos.zip", None, "doesn't contain LinkedIn profile files"),
    ("other.pdf", None, "doesn't look like a LinkedIn profile"),
    ("Positions.csv", b"Company Name,Title\n", "couldn't find any profile details"),
])
def test_files_that_arent_linkedin_are_explained(name, data, message):
    if name == "photos.zip":
        data = _zip({"photo.jpg": b"\xff\xd8"})
    if name == "other.pdf":
        data = _pdf_with_text("Quarterly report")
    with pytest.raises(LinkedInImportError, match=message):
        read_files([(name, data)])


def test_nothing_uploaded_and_too_large():
    with pytest.raises(LinkedInImportError, match="Choose your LinkedIn"):
        read_files([])
    with pytest.raises(LinkedInImportError, match="over 30 MB"):
        read_files([("big.zip", b"PK\x03\x04" + b"0" * linkedin.MAX_UPLOAD_BYTES)])


def test_zip_bombs_and_oversized_members_are_refused(monkeypatch):
    monkeypatch.setattr(linkedin, "_MAX_CSV_BYTES", 1000)
    huge = _zip({"Positions.csv": b"Company Name,Title\n" + b"Acme,Clerk\n" * 500})  # compresses to almost nothing
    with pytest.raises(LinkedInImportError, match="too large"):
        read_files([("export.zip", huge)])
    monkeypatch.setattr(linkedin, "_MAX_ZIP_ENTRIES", 3)
    with pytest.raises(LinkedInImportError, match="too many files"):
        read_files([("export.zip", _zip({f"f{i}.csv": b"x" for i in range(5)}))])


def _pdf_with_text(text: str) -> bytes:
    """A one-page PDF that isn't a LinkedIn profile."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
               b"/Resources << /Font << /F1 5 0 R >> >> >>",
               b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = b"%PDF-1.4\n", []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    return out + b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
