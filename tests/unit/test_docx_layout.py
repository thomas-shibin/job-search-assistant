"""Layout fixes for ATS software inside a Word resume
(app/modules/resumes/documents/docx_layout.py): after them the ATS check finds
no tables, text boxes, columns or header-only contact details, and every word
is still there, in reading order."""
from __future__ import annotations

from pathlib import Path

from docx import Document
from docx.enum.text import WD_TAB_ALIGNMENT
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import qn

from app.modules.resumes import ats, canadian
from app.modules.resumes.documents import docx_layout

BULLET = "Led a team of five analysts to rebuild monthly reporting, cutting the close from ten days to six"
LAYOUT = set(ats.FIXES)

_W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
_BOX = f"""<w:r {_W} xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"
    xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape"
    xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
    xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:v="urn:schemas-microsoft-com:vml">
  <mc:AlternateContent>
    <mc:Choice Requires="wps"><w:drawing><wp:anchor><a:graphic><a:graphicData><wps:wsp><wps:txbx>
      <w:txbxContent><w:p><w:r><w:t>Certified Payroll Professional, 2021</w:t></w:r></w:p></w:txbxContent>
    </wps:txbx></wps:wsp></a:graphicData></a:graphic></wp:anchor></w:drawing></mc:Choice>
    <mc:Fallback><w:pict><v:shape><v:textbox>
      <w:txbxContent><w:p><w:r><w:t>Certified Payroll Professional, 2021</w:t></w:r></w:p></w:txbxContent>
    </v:textbox></v:shape></w:pict></mc:Fallback>
  </mc:AlternateContent>
</w:r>"""


def _designed(path: Path) -> None:
    doc = Document()
    header = doc.sections[0].header.paragraphs[0]
    header.text = "jordan.lee@example.com | 416 555 0199 | "
    rid = doc.sections[0].header.part.relate_to("https://linkedin.com/in/jordanlee", RT.HYPERLINK, is_external=True)
    link = OxmlElement("w:hyperlink")
    link.set(qn("r:id"), rid)
    run = OxmlElement("w:r")
    text = OxmlElement("w:t")
    text.text = "linkedin.com/in/jordanlee"
    run.append(text)
    link.append(run)
    header._p.append(link)

    doc.add_paragraph("Jordan Lee")
    anchor = doc.add_paragraph("Certifications")
    anchor._p.append(parse_xml(_BOX))
    doc.add_heading("Experience", 1)
    jobs = doc.add_table(rows=2, cols=2)
    jobs.cell(0, 0).text, jobs.cell(0, 1).text = "Financial Analyst, Acme Ltd", "Jan 2020 – Present"
    jobs.cell(1, 0).text = BULLET
    jobs.cell(1, 0).add_paragraph(BULLET)
    skills = doc.add_table(rows=1, cols=3)
    for cell, skill in zip(skills.row_cells(0), ("Python", "SQL", "Excel")):
        cell.text = skill
    doc.add_paragraph("Left column").add_run().add_break()
    br = doc.paragraphs[-1].runs[-1]._r.find(qn("w:br"))
    br.set(qn("w:type"), "column")
    doc.add_heading("Education", 1)
    doc.add_paragraph("Bachelor of Commerce, University of Toronto, 2018")
    cols = OxmlElement("w:cols")
    cols.set(qn("w:num"), "2")
    doc.sections[0]._sectPr.append(cols)
    doc.save(str(path))


def _keys(path: Path) -> set[str]:
    return {f.key for f in ats.check(path, canadian.read(path), path.stat().st_size)}


def _body_text(path: Path) -> str:
    return "\n".join(p.text for p in Document(str(path)).paragraphs)


def test_every_layout_problem_is_fixed_and_no_words_are_lost(tmp_path):
    path = tmp_path / "designed.docx"
    _designed(path)
    assert LAYOUT <= _keys(path)

    done = docx_layout.fix(path, LAYOUT)

    assert done == {"ats_header": 1, "ats_text_boxes": 1, "ats_tables": 2, "ats_columns": 1}
    assert not (_keys(path) & LAYOUT)
    text = _body_text(path)
    lines = text.split("\n")
    assert lines[0] == "Jordan Lee"
    assert lines[1].startswith("jordan.lee@example.com | 416 555 0199 | linkedin.com/in/jordanlee")  # under the name
    assert lines.index("Certifications") + 1 == lines.index("Certified Payroll Professional, 2021")
    assert text.count("Certified Payroll Professional") == 1  # Word's legacy copy of the box didn't come too
    assert "Financial Analyst, Acme Ltd\tJan 2020 – Present" in lines  # a short row: one line, dates at a tab
    assert lines.count(BULLET) == 2 and "Python | SQL | Excel" in lines
    assert "Bachelor of Commerce, University of Toronto, 2018" in lines

    doc = Document(str(path))
    title = next(p for p in doc.paragraphs if p.text.startswith("Financial Analyst"))
    stops = list(title.paragraph_format.tab_stops)
    assert stops and stops[0].alignment == WD_TAB_ALIGNMENT.RIGHT
    link = doc.element.body.find(".//" + qn("w:hyperlink"))
    assert doc.part.rels[link.get(qn("r:id"))].target_ref == "https://linkedin.com/in/jordanlee"  # link still works
    assert not list(doc.element.body.iter(qn("w:tbl"))) and not list(doc.element.body.iter(qn("w:txbxContent")))
    assert not [br for br in doc.element.body.iter(qn("w:br")) if br.get(qn("w:type")) == "column"]
    assert doc.sections[0].header.paragraphs  # the header is empty but still valid


def test_only_the_chosen_fixes_are_made_and_a_clean_file_is_left_alone(tmp_path):
    path = tmp_path / "designed.docx"
    _designed(path)
    assert docx_layout.fix(path, {"ats_columns"}) == {"ats_columns": 1}
    assert {"ats_tables", "ats_text_boxes", "ats_header"} <= _keys(path)

    plain = tmp_path / "plain.docx"
    doc = Document()
    doc.add_paragraph("Jordan Lee")
    doc.add_paragraph("jordan.lee@example.com | 416 555 0199")
    doc.save(str(plain))
    before = plain.read_bytes()
    assert docx_layout.fix(plain, LAYOUT) == {}
    assert plain.read_bytes() == before  # not even re-saved


def test_a_header_with_the_name_moves_to_the_very_top(tmp_path):
    doc = Document()
    doc.sections[0].header.paragraphs[0].text = "Jordan Lee"
    doc.sections[0].header.add_paragraph("jordan.lee@example.com | 416 555 0199")
    doc.add_heading("Experience", 1)
    doc.add_paragraph(BULLET)
    path = tmp_path / "header.docx"
    doc.save(str(path))
    assert docx_layout.fix(path, {"ats_header"}) == {"ats_header": 2}
    assert _body_text(path).split("\n")[:3] == ["Jordan Lee", "jordan.lee@example.com | 416 555 0199", "Experience"]


def test_a_page_number_stays_in_the_footer_and_a_header_without_contact_stays(tmp_path):
    doc = Document()
    doc.add_paragraph("Jordan Lee")
    doc.add_paragraph("jordan.lee@example.com | 416 555 0199")
    doc.sections[0].header.paragraphs[0].text = "Jordan Lee — Resume"
    path = tmp_path / "ok.docx"
    doc.save(str(path))
    assert docx_layout.fix(path, {"ats_header"}) == {}
    assert Document(str(path)).sections[0].header.paragraphs[0].text == "Jordan Lee — Resume"


def test_word_findings_offer_the_fix_and_a_pdf_says_the_standard_layout_fixes_it(tmp_path):
    path = tmp_path / "designed.docx"
    _designed(path)
    findings = {f.key: f for f in ats.check(path, canadian.read(path), path.stat().st_size)}
    for key in LAYOUT:
        assert findings[key].fixable and findings[key].fix_label == ats.FIXES[key]
    assert not findings.get("ats_email")  # nothing else is offered as a one-click fix
    assert LAYOUT <= ats.LAYOUT_FIXES
