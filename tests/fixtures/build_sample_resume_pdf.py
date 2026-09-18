"""Generates tests/fixtures/sample_resume.pdf.

Mirrors the structure of build_sample_resume.py (the .docx fixture) so PDF
parsing can be tested against a comparable, realistic layout. PDF text has no
bold/paragraph-style metadata, so section headers here are written in ALL CAPS
(the heading heuristic PDF parsing falls back to) and bullets use a leading
"- " character instead of Word's "List Bullet" style.

Run directly to (re)build the fixture: python tests/fixtures/build_resume_resume_pdf.py
"""
from __future__ import annotations

from pathlib import Path

from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import LETTER

OUTPUT_PATH = Path(__file__).resolve().parent / "sample_resume.pdf"

LINES = [
    "Jordan Casey",
    "jordan.casey@example.com | (555) 123-4567 | Victoria, BC",
    "",
    "PROFESSIONAL SUMMARY",
    "Senior backend engineer with 10+ years of experience building distributed",
    "systems in Java and Python, specializing in scalable REST APIs and",
    "cloud-native architectures.",
    "",
    "EXPERIENCE",
    "Senior Software Engineer, Acme Cloud Systems | Jan 2021 - Present",
    "- Led design and implementation of a microservices platform handling 50M+ requests/day",
    "- Reduced p95 API latency by 40% through caching and query optimization",
    "- Mentored a team of 5 engineers and led architecture review sessions",
    "Software Engineer, Beta Systems Inc. | Jun 2018 - Dec 2020",
    "- Built and maintained REST APIs serving the company's core billing platform",
    "- Migrated a monolithic Java application to a set of containerized Spring Boot services",
    "- Introduced automated integration tests, cutting regression bugs by half",
    "Junior Developer — Gamma Software (2015-2018)",
    "- Developed internal tooling for the QA team using Python",
    "- Fixed production defects reported through the support queue",
    "- Wrote unit tests for legacy modules with no prior coverage",
    "",
    "SKILLS",
    "Java, Python, Spring Boot, REST APIs, Distributed Systems, AWS, Kubernetes,",
    "Docker, PostgreSQL, MongoDB, Kafka, OAuth2, Keycloak, Git, CI/CD,",
    "Microservices, Terraform, Linux",
    "",
    "EDUCATION",
    "Bachelor of Science in Computer Science, University of Victoria (2011 - 2015)",
    "",
    "CERTIFICATIONS",
    "AWS Certified Solutions Architect - Associate, Amazon Web Services, 2022",
    "Certified Kubernetes Administrator (CKA), Cloud Native Computing Foundation, 2023",
    "",
    "VOLUNTEER EXPERIENCE",
    "Mentor, Local Coding Bootcamp (2019-Present) - helped junior developers with career transitions.",
]


def build(path: Path) -> None:
    c = canvas.Canvas(str(path), pagesize=LETTER)
    c.setFont("Helvetica", 10)
    width, height = LETTER
    y = height - 50
    for line in LINES:
        if line:
            c.drawString(50, y, line)
        y -= 14
        if y < 50:
            c.showPage()
            c.setFont("Helvetica", 10)
            y = height - 50
    c.save()


if __name__ == "__main__":
    build(OUTPUT_PATH)
    print(f"Wrote {OUTPUT_PATH}")
