"""ATS readability: whether applicant tracking systems are likely to read a
resume correctly.

This doesn't simulate any ATS. Workday, Greenhouse, Taleo, iCIMS and the rest
are closed, each employer sets its own screening, and none publishes a pass
mark -- so no tool can honestly say a resume "passes" or "fails" one. What it
does is look for what ATS makers document as breaking their resume parsing
(Greenhouse: graphics, tables, headers and footers, columns, images, files
over 2.5 MB) and what it can see directly: text that doesn't copy out of a PDF,
and no email or phone number in the text. Each finding's source is in
app/modules/resumes/check_sources.py.
"""
from __future__ import annotations

import re
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn

from app.modules.resumes.canadian import MUST, SHOULD, TIP, Finding, ResumeDocument, _examples
from app.modules.resumes.documents.docx_edit import _MC_FALLBACK, iter_paragraphs

PARSE_LIMIT_BYTES = 2_500_000  # Greenhouse: "Resumes over 2.5MB can't be parsed."
TABLE_WORDS = 20  # fewer words in a table is a small detail, not the resume's layout
GUTTER_POINTS = 12
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"(?:\+?\d[\s().-]*){10,15}")
_EXPERIENCE = re.compile(r"(?i)experience|employment|work history|career history|professional background|internship")

# What converting fixes inside a Word file (documents/docx_layout.py), and its label.
FIXES = {
    "ats_header": "Move them to the top of the page",
    "ats_text_boxes": "Put this text in the page",
    "ats_tables": "Turn the tables into plain lines (the side-by-side layout and borders go)",
    "ats_columns": "Make it one column",
}
# What the standard layout fixes when a PDF is converted: it's a plain,
# one-column Word file with no tables, text boxes or page header.
LAYOUT_FIXES = {"ats_graphics", "ats_file_size", "ats_tables", "ats_columns", "ats_text_boxes", "ats_header"}


def check(path: Path, doc: ResumeDocument, size: int) -> list[Finding]:
    """What may stop an ATS reading this resume correctly, most important first."""
    findings = _docx(path, doc) if doc.is_docx else _pdf(path, doc)
    if doc.is_docx:
        for finding in findings:
            finding.fix_label = FIXES.get(finding.key)
    if size > PARSE_LIMIT_BYTES:
        findings.append(Finding(
            "ats_file_size", SHOULD, f"Make the file smaller (it's {size / 1_000_000:.1f} MB)",
            "Some ATS stop reading resumes above 2.5 MB. Large pictures are usually the cause: remove them, or "
            "save again with \"compress pictures\"."))
    findings += _contact_and_headings(doc)
    order = {MUST: 0, SHOULD: 1, TIP: 2}
    return sorted(findings, key=lambda f: order[f.level])


def _words(element) -> int:
    return len(" ".join(t.text or "" for t in element.iter(qn("w:t"))).split())


def _docx(path: Path, doc: ResumeDocument) -> list[Finding]:
    document = Document(str(path))
    body = document.element.body
    findings = []

    tables = [t for t in body.iter(qn("w:tbl")) if not any(a.tag == qn("w:tbl") for a in t.iterancestors())]
    in_tables = sum(_words(t) for t in tables)
    if in_tables >= TABLE_WORDS:
        findings.append(Finding(
            "ats_tables", SHOULD, "Move your content out of tables",
            f"About {in_tables} words of your resume are inside tables. Lay it out with plain paragraphs, tabs and "
            "bullets instead."))

    boxes = [b for b in body.iter(qn("w:txbxContent"))
             if not any(a.tag == _MC_FALLBACK for a in b.iterancestors())]
    boxed = [" ".join(t.text or "" for t in b.iter(qn("w:t"))).strip() for b in boxes]
    boxed = [text for text in boxed if len(text.split()) >= 3]
    if boxed:
        findings.append(Finding(
            "ats_text_boxes", SHOULD, "Take text out of text boxes",
            "Put this text in the page itself rather than in a text box or shape.", _examples(boxed)))

    columns = [int(c.get(qn("w:num")) or 1) for c in body.iter(qn("w:cols"))]
    if any(n >= 2 for n in columns):
        findings.append(Finding(
            "ats_columns", SHOULD, "Use one column",
            "Your resume is set in columns. Put everything in a single column, with section headings above "
            "each part."))

    header_text, body_text = [], []
    for paragraph, in_header in iter_paragraphs(document):
        (header_text if in_header else body_text).append(paragraph.text)
    header_text, body_text = "\n".join(header_text), "\n".join(body_text)
    in_header = [what for what, pattern in (("email", _EMAIL), ("phone number", _PHONE))
                 if pattern.search(header_text) and not pattern.search(body_text)]
    if in_header:
        findings.append(Finding(
            "ats_header", SHOULD, "Move your contact details out of the page header",
            f"Your {' and '.join(in_header)} {'are' if len(in_header) > 1 else 'is'} only in the page header or "
            "footer. Put your name and contact details at the top of the page itself."))

    drawings = sum(1 for _ in body.iter(qn("w:drawing"))) + sum(1 for _ in body.iter(qn("w:pict")))
    if drawings - doc.photos > 0:
        findings.append(_graphics(drawings - doc.photos))
    return findings


def _graphics(count: int) -> Finding:
    return Finding(
        "ats_graphics", TIP, "Keep important information out of pictures",
        f"Your resume has {count} picture{'s' if count != 1 else ''}, icon{'s' if count != 1 else ''} or "
        "graphic. That's fine for decoration, but anything in them (skill levels, logos, contact icons without "
        "text) can't be read, so write it as text too.")


def _pdf(path: Path, doc: ResumeDocument) -> list[Finding]:
    import pdfplumber

    findings = []
    with pdfplumber.open(str(path)) as pdf:
        pages = pdf.pages[:3]
        chars = [c for page in pages for c in page.chars]
        broken = sum(1 for c in chars if c.get("text", "").startswith("(cid:"))
        if chars and broken / len(chars) > 0.02:
            findings.append(Finding(
                "ats_unreadable", MUST, "Your PDF's text doesn't copy out correctly",
                "Part of the text comes out as codes instead of letters, usually because of the font. Save the PDF "
                "again from Word or Google Docs with a standard font (Calibri, Arial, Garamond), or upload the "
                "Word file instead."))
        if pages and _has_columns(pages[0]):
            findings.append(Finding(
                "ats_columns", SHOULD, "Use one column",
                "Your resume looks like it's set in two columns (for example a sidebar). Put everything in a single "
                "column, with section headings above each part."))
        if any(_text_table(page) for page in pages):
            findings.append(Finding(
                "ats_tables", SHOULD, "Move your content out of tables",
                "Part of your resume is laid out in a table with borders. Lay it out with plain paragraphs, tabs "
                "and bullets instead."))
        graphics = sum(len(page.images) for page in pages) - doc.photos  # the photo has its own finding
    if graphics > 0:
        findings.append(_graphics(graphics))
    return findings


def _has_columns(page) -> bool:
    """A clear vertical gap down the middle of the page with text on both sides:
    two columns or a sidebar. A one-column resume has lines crossing the middle
    (its bullets), even when dates sit at the right. A few lines may cross the
    gap -- a name and contact line above both columns usually does."""
    words = page.extract_words()
    if len(words) < 60:
        return False
    lines: dict[int, list[tuple[float, float]]] = {}
    for w in words:
        lines.setdefault(round(float(w["top"]) / 3), []).append((float(w["x0"]), float(w["x1"])))
    width = float(page.width)
    crossing = [0] * (int(width) + 2)
    for spans in lines.values():
        covered = set()
        for x0, x1 in spans:  # widened a little, so the spaces between words count as text
            covered.update(range(max(0, int(x0) - 3), min(len(crossing), int(x1) + 4)))
        for x in covered:
            crossing[x] += 1
    allowed = max(1, len(lines) // 20)
    best = run = best_end = 0
    for x in range(int(width * 0.15), int(width * 0.8)):
        run = run + 1 if crossing[x] <= allowed else 0
        if run > best:
            best, best_end = run, x
    if best < GUTTER_POINTS:
        return False
    middle = best_end - best / 2
    left = sum(1 for w in words if w["x1"] < middle)
    return min(left, len(words) - left) >= len(words) * 0.15


def _text_table(page) -> bool:
    for table in page.find_tables():
        rows = table.extract()
        if len(rows) >= 2 and max((len(r) for r in rows), default=0) >= 2:
            if sum(1 for row in rows for cell in row if cell and cell.strip()) >= 4:
                return True
    return False


def _contact_and_headings(doc: ResumeDocument) -> list[Finding]:
    findings = []
    text = "\n".join(line.text for line in doc.lines)
    if not _EMAIL.search(text):
        findings.append(Finding(
            "ats_email", MUST, "Add your email address as text",
            "Write your email address under your name, as plain text (not only as an icon or a link on a word)."))
    if not _PHONE.search(text):
        findings.append(Finding(
            "ats_phone", SHOULD, "Add your phone number as text",
            "Write your phone number under your name, as plain text."))
    headings = [line for line in doc.lines if line.heading]
    unknown = [line.text for line in headings if line.section == "other"]
    if unknown and not any(line.section == "experience" or _EXPERIENCE.search(line.text) for line in headings):
        findings.append(Finding(
            "ats_headings", SHOULD, "Use standard section headings",
            "We couldn't find an Experience heading. Use familiar headings such as Summary, Experience, "
            "Education and Skills, so your jobs land in the right place.", _examples(unknown),
            lines=[i for i, line in enumerate(doc.lines) if line.heading and line.section == "other"]))
    return findings
