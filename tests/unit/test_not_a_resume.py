"""Only a resume is checked (canadian.evidence): the checks look for problems, so a
health card, a letter or a job posting would otherwise pass as "Canada-ready"."""
from __future__ import annotations

from pathlib import Path

import pytest
from docx import Document

from app.modules.resumes import canadian

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

NOT_RESUMES = {
    'health_card': """Ontario Health Insurance Plan
Health Card
JORDAN LEE
Health Number 1234-567-890 AB
Date of Birth 1990-05-12
Issue Date 2021-03-01
Expiry Date 2026-05-12
Personal Information
Sex M
This card is the property of the Government of Ontario. It must be shown each time you receive insured health services. If found, please return it to ServiceOntario, PO Box 48, Kingston ON. Call 1-866-532-3161 for help.""",
    'cover_letter': """Jordan Lee
jordan.lee@example.com | 416 555 0199
October 22, 2026
Hiring Manager
Acme Ltd, Toronto, ON
Dear Hiring Manager,
I am writing to apply for the Financial Analyst position advertised on your website. In my five years at Bolt Inc, from 2019 to 2024, I rebuilt monthly reporting and cut the close from ten days to six. I would welcome the chance to bring that experience to Acme.
Thank you for your time and consideration.
Sincerely,
Jordan Lee""",
    'invoice': """INVOICE
Acme Plumbing Ltd
123 King St W, Toronto ON M5H 1A1
Phone 416 555 0100  billing@acmeplumbing.ca
Invoice # 20391   Date: 2026-09-14   Due: 2026-10-14
Bill to: Jordan Lee, 45 Elm Street, Toronto
Description Qty Rate Amount
Kitchen sink repair 1 180.00 180.00
Replacement valve 2 24.50 49.00
Subtotal 229.00 HST 13% 29.77 Total 258.77
Thank you for your business. Payment by e-transfer within 30 days.""",
    'job_posting': """Financial Analyst
Acme Ltd - Toronto, ON - Full time
About the role
We are looking for a Financial Analyst to join our finance team and support month-end close and forecasting.
Responsibilities
Prepare monthly management reports and variance analysis
Build forecasting models in Excel
Requirements
3+ years in financial analysis
Qualifications
Bachelor's degree in Finance, Accounting or Economics; CPA an asset
Skills
Excel, SQL, Power BI
Benefits
Health and dental, RRSP matching, hybrid work
How to apply
Send your resume to careers@acme.example by November 30, 2026.""",
    'lease': """Residential Tenancy Agreement (Standard Form of Lease)
Parties to the Agreement
Landlord: Maple Properties Inc. Tenant: Jordan Lee
Rental Unit: 45 Elm Street, Unit 3, Toronto ON
Term of Tenancy Agreement: This tenancy starts on 2025-09-01 and is for a fixed length ending on 2026-08-31.
Rent: Rent is to be paid on the first day of each month. Base rent is $2,150.00.
Services and Utilities: The tenant is responsible for electricity. Contact: 647 555 0123.""",
    'essay': """Climate change and Canadian cities
Canadian cities face hotter summers, heavier rain and longer droughts. Toronto has expanded its green roof bylaw since 2009, while Vancouver has invested in sea walls. This essay compares how three cities plan for the next fifty years, and argues that funding, not knowledge, is the main obstacle. Between 1990 and 2020, average summer temperatures rose by more than one degree.""",
    'bank_statement': """Royal Bank Chequing Account Statement
Jordan Lee  Account 01234-5678901
Statement period Sep 1, 2026 to Sep 30, 2026
Opening balance 2,410.22
Sep 3 Payroll deposit Acme Ltd 2,850.00
Sep 5 Rent e-Transfer Maple Properties 2,150.00
Sep 12 Grocery Loblaws 143.88
Sep 20 Phone bill Rogers 75.00
Closing balance 2,891.34
Questions? Call 1-800-769-2511""",
}

RESUMES = {
    # Unusual headings, but jobs with dates and contact details.
    "own_headings": """Jordan Lee
jordan.lee@example.com | 416 555 0199
My Journey
Financial Analyst, Acme Ltd, Jan 2020 – Present
Rebuilt monthly reporting for five regional teams, cutting the close from ten days to six
Junior Analyst, Bolt Inc, 2017 - 2019
Reconciled accounts for twelve branches and automated the weekly cash report in Excel
What I Bring
Excel, SQL, Power BI, forecasting, stakeholder reporting""",
    # A student: no jobs yet, but Education and contact details.
    "student": """Sam Patel
sam.patel@example.com | 604 555 0142 | Vancouver, BC
Education
Bachelor of Computer Science, University of British Columbia, expected 2027
Relevant courses: data structures, databases, software engineering, statistics
Volunteer tutor for first-year programming students at the campus learning centre
Interests: hiking, chess, open-source software, building small web apps with friends""",
}


def _docx(tmp_path: Path, name: str, text: str) -> Path:
    doc = Document()
    for line in text.splitlines():
        doc.add_paragraph(line)
    path = tmp_path / f"{name}.docx"
    doc.save(str(path))
    return path


@pytest.mark.parametrize("name", sorted(NOT_RESUMES))
def test_other_documents_are_not_resumes(tmp_path, name):
    doc = canadian.read(_docx(tmp_path, name, NOT_RESUMES[name]))
    assert not canadian.evidence(doc).is_resume


@pytest.mark.parametrize("name", sorted(RESUMES))
def test_unusual_but_real_resumes_are(tmp_path, name):
    doc = canadian.read(_docx(tmp_path, name, RESUMES[name]))
    assert canadian.evidence(doc).is_resume


@pytest.mark.parametrize("name", ["sample_resume.pdf", "sample_resume.docx", "linkedin_profile_sample.pdf"])
def test_sample_resumes_and_a_linkedin_profile_are(name):
    assert canadian.evidence(canadian.read(FIXTURES / name)).is_resume
