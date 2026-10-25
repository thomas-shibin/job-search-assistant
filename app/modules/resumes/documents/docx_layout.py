"""Layout fixes for ATS software, made inside the member's Word file (the
problems are found by app/modules/resumes/ats.py):

- contact details only in the page header or footer are moved to the top of
  the page itself;
- text in text boxes and shapes is put in the page, after the paragraph the
  box is anchored to;
- tables become plain paragraphs, row by row: a short row of two cells
  ("Acme Ltd | 2019 - 2022") becomes one line with the second part at a right
  tab, a short row of more cells becomes one line joined with " | ", anything
  else becomes the cells' paragraphs one after another;
- columns become one column (column breaks are removed with them, as one
  column turns them into page breaks).

Paragraphs are moved, not retyped, so their wording, fonts and bullets stay.
What the table or the columns did for the look -- side-by-side cells, borders,
shading -- doesn't; that's the trade for being read correctly.
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from pathlib import Path

from docx import Document
from docx.enum.text import WD_TAB_ALIGNMENT
from docx.oxml.ns import qn
from docx.shared import Twips
from docx.text.paragraph import Paragraph

_MC_FALLBACK = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"
_R_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"(?:\+?\d[\s().-]*){10,15}")
_NAME = re.compile(r"^[^\W\d_][\w.'’-]*(?:\s+[^\W\d_][\w.'’-]*){0,4}$")  # "Jordan Lee", "Mary-Jane O'Neil"
SHORT_ROW_WORDS = 15  # a row this short reads as one line ("Title | Company | 2019 - 2022")
_CONTENT = (qn("w:p"), qn("w:tbl"), qn("w:sdt"))


def _text(element) -> str:
    return " ".join(t.text or "" for t in element.iter(qn("w:t"))).strip()


def _has_content(element) -> bool:
    return bool(_text(element)) or any(True for _ in element.iter(qn("w:drawing"))) or \
        any(True for _ in element.iter(qn("w:pict")))


def _in_fallback(element) -> bool:
    return any(a.tag == _MC_FALLBACK for a in element.iterancestors())


def _nearest(element, tag: str):
    return next((a for a in element.iterancestors() if a.tag == tag), None)


def fix(path: Path, chosen: set[str], only: dict[str, set[int]] | None = None) -> dict[str, int]:
    """Make the chosen fixes ("ats_header", "ats_text_boxes", "ats_tables",
    "ats_columns") in the Word file at `path`, in place. `only` limits tables
    and text boxes to some of them ({key: indexes as `preview` numbers them}).
    Returns how many things each one changed; nothing is saved when nothing changed."""
    doc = Document(str(path))
    only = only or {}
    # Found before anything moves, so the numbers match what the member was shown.
    tables, boxes = _tables(doc), _boxes(doc)
    pick = lambda key, items: [x for n, x in enumerate(items) if key not in only or n in only[key]]  # noqa: E731
    done = {}
    if "ats_header" in chosen:
        done["ats_header"] = _contact_into_body(doc)
    if "ats_text_boxes" in chosen:
        done["ats_text_boxes"] = _text_boxes_into_body(pick("ats_text_boxes", boxes))
    if "ats_tables" in chosen:
        done["ats_tables"] = _unwrap_tables(doc, pick("ats_tables", tables))
    if "ats_columns" in chosen:
        done["ats_columns"] = _one_column(doc)
    done = {key: n for key, n in done.items() if n}
    if done:
        doc.save(str(path))
    return done


@dataclass
class Item:
    """One thing a fix would change, shown before it's made."""
    key: str
    index: int
    before: list[str]  # its text now
    after: list[str]  # its text after the fix
    near: str  # the closest heading or line above it, to say where it is


def _tables(doc) -> list:
    """The tables to unwrap, in reading order (a table inside another goes with it)."""
    return [t for t in doc.element.body.iter(qn("w:tbl"))
            if not _in_fallback(t) and _nearest(t, qn("w:tbl")) is None]


def _boxes(doc) -> list:
    return [b for b in doc.element.body.iter(qn("w:txbxContent"))
            if not _in_fallback(b) and _nearest(b, qn("w:txbxContent")) is None and _text(b)]


def _above(element, is_heading) -> str:
    """The nearest heading before `element` in the page, else the nearest line."""
    nearest = ""
    node = element
    while node is not None and node.tag != qn("w:body"):
        for sibling in node.itersiblings(preceding=True):
            for p in reversed(list(sibling.iter(qn("w:p"))) if sibling.tag != qn("w:p") else [sibling]):
                text = _text(p)
                if not text:
                    continue
                if is_heading(text):
                    return text
                nearest = nearest or text
        node = node.getparent()
    return nearest


def preview(path: Path, is_heading=lambda text: False) -> dict[str, list[Item]]:
    """What each layout fix would change, item by item."""
    doc = Document(str(path))
    out: dict[str, list[Item]] = {}
    width = _text_width(doc)
    items = []
    for n, table in enumerate(_tables(doc)):
        rows = [" | ".join(_text(tc) for tc in _cells(tr) if _text(tc)) for tr in table.iter(qn("w:tr"))]
        work = copy.deepcopy(table)
        for inner in reversed([t for t in work.iter(qn("w:tbl")) if t is not work]):
            for element in _unwrap(inner, width):
                inner.addprevious(element)
            inner.getparent().remove(inner)
        after = [_text(e).replace("\t", "  ⇥  ") for e in _unwrap(work, width)]
        items.append(Item("ats_tables", n, [r for r in rows if r], [a for a in after if a], _above(table, is_heading)))
    if items:
        out["ats_tables"] = items
    boxes = [Item("ats_text_boxes", n, [_text(p) for p in box if _text(p)], [_text(p) for p in box if _text(p)],
                  _above(box, is_heading)) for n, box in enumerate(_boxes(doc))]
    if boxes:
        out["ats_text_boxes"] = boxes
    header = [_text(c) for part, element in _header_parts(doc) for c in element
              if c.tag in _CONTENT and _text(c) and not _has_field(c)]
    if header:
        out["ats_header"] = [Item("ats_header", 0, header, header, "")]
    if any(int(c.get(qn("w:num")) or 1) >= 2 for c in doc.element.body.iter(qn("w:cols"))):
        out["ats_columns"] = [Item("ats_columns", 0, ["In columns"], ["One column"], "")]
    return out


# ---- contact details out of the page header -------------------------------------------------

def _header_parts(doc):
    """(part, element) of every page header and footer with its own content."""
    seen = set()
    for section in doc.sections:
        for item in (section.header, section.first_page_header, section.even_page_header,
                     section.footer, section.first_page_footer, section.even_page_footer):
            if item.is_linked_to_previous:
                continue
            part = item.part
            if id(part) not in seen:
                seen.add(id(part))
                yield part, item._element


def _relink(element, source_part, target_part) -> None:
    """Moved content keeps its links and pictures: relationships (r:id, r:embed)
    point into the header's part, so they're re-made in the document's."""
    for node in element.iter():
        for name, value in list(node.attrib.items()):
            if not name.startswith(_R_NS) or value not in source_part.rels:
                continue
            rel = source_part.rels[value]
            if rel.is_external:
                node.set(name, target_part.relate_to(rel.target_ref, rel.reltype, is_external=True))
            else:
                node.set(name, target_part.relate_to(rel.target_part, rel.reltype))


def _has_field(element) -> bool:
    return any(True for _ in element.iter(qn("w:fldSimple"))) or any(True for _ in element.iter(qn("w:instrText")))


def _contact_into_body(doc) -> int:
    body = doc.element.body
    body_text = _text(body)
    moved = []
    for part, element in _header_parts(doc):
        text = _text(element)
        if not any(p.search(text) and not p.search(body_text) for p in (_EMAIL, _PHONE)):
            continue
        for child in [c for c in element if c.tag in _CONTENT]:
            # Page numbers and other fields stay in the header; so does anything empty.
            if not _text(child) or _has_field(child):
                continue
            element.remove(child)
            _relink(child, part, doc.part)
            moved.append(child)
        if not any(c.tag == qn("w:p") for c in element):
            element.append(element.makeelement(qn("w:p"), {}))  # a header needs a paragraph
    if not moved:
        return 0
    # Under the name when the page starts with it (and the header didn't hold the name), else at the top.
    first = next((c for c in body if c.tag in _CONTENT and _text(c)), None)
    under_name = first is not None and first.tag == qn("w:p") and _NAME.match(_text(first)) and \
        not any(_NAME.match(_text(c)) for c in moved)
    if under_name:
        place = first
        for child in moved:
            place.addnext(child)
            place = child
    else:
        top = next(iter(body), None)
        for child in moved:
            if top is not None and top.tag != qn("w:sectPr"):
                top.addprevious(child)
            else:
                body.insert(0, child)
    return len(moved)


# ---- text boxes into the page ------------------------------------------------------------------

def _text_boxes_into_body(boxes: list) -> int:
    moved = 0
    for box in boxes:
        run = _nearest(box, qn("w:r"))
        anchor = _nearest(run, qn("w:p")) if run is not None else None
        if anchor is None or anchor.getparent() is None:
            continue
        place = anchor
        for child in [c for c in box if c.tag in _CONTENT]:
            place.addnext(child)
            place = child
        run.getparent().remove(run)  # the box (and Word's legacy copy of it) goes
        moved += 1
    return moved


# ---- tables into paragraphs ------------------------------------------------------------------

def _text_width(doc) -> int | None:
    """The page's text width in twips, for a right tab at the margin."""
    section = doc.sections[0]
    if not (section.page_width and section.left_margin is not None and section.right_margin is not None):
        return None
    return int((section.page_width - section.left_margin - section.right_margin) / 635)  # EMU -> twips


def _cells(row) -> list:
    return [tc for tc in row.iter(qn("w:tc")) if _nearest(tc, qn("w:tr")) is row]


def _merge(paragraphs: list, separator: str, width: int | None):
    """One paragraph with the others' runs appended after `separator` ("\\t": at a right tab)."""
    base = paragraphs[0]
    for other in paragraphs[1:]:
        gap = base.makeelement(qn("w:r"), {})
        if separator == "\t":
            gap.append(gap.makeelement(qn("w:tab"), {}))
        else:
            t = gap.makeelement(qn("w:t"), {"{http://www.w3.org/XML/1998/namespace}space": "preserve"})
            t.text = separator
            gap.append(t)
        base.append(gap)
        for child in [c for c in other if c.tag != qn("w:pPr")]:
            base.append(child)
    if separator == "\t" and width:
        Paragraph(base, None).paragraph_format.tab_stops.add_tab_stop(Twips(width), WD_TAB_ALIGNMENT.RIGHT)
    return base


def _unwrap(table, width: int | None) -> list:
    out = []
    for row in table.findall(qn("w:tr")):
        cells = [[c for c in tc if c.tag in _CONTENT and _has_content(c)] for tc in _cells(row)]
        cells = [c for c in cells if c]
        words = sum(len(_text(e).split()) for c in cells for e in c)
        if len(cells) >= 2 and words <= SHORT_ROW_WORDS and all(len(c) == 1 and c[0].tag == qn("w:p") for c in cells):
            out.append(_merge([c[0] for c in cells], "\t" if len(cells) == 2 else " | ", width))
        else:
            out.extend(e for c in cells for e in c)
    return out


def _unwrap_tables(doc, tables: list) -> int:
    width = _text_width(doc)
    for table in tables:
        # Tables inside it first, innermost first (document order lists a table before those inside it).
        for inner in reversed([t for t in table.iter(qn("w:tbl")) if t is not table]):
            for element in _unwrap(inner, width):
                inner.addprevious(element)
            inner.getparent().remove(inner)
        for element in _unwrap(table, width):
            table.addprevious(element)
        table.getparent().remove(table)
    return len(tables)


# ---- one column ------------------------------------------------------------------------------

def _one_column(doc) -> int:
    body = doc.element.body
    changed = 0
    for cols in body.iter(qn("w:cols")):
        if int(cols.get(qn("w:num")) or 1) < 2:
            continue
        for col in cols.findall(qn("w:col")):
            cols.remove(col)
        for name in (qn("w:equalWidth"), qn("w:sep")):
            cols.attrib.pop(name, None)
        cols.set(qn("w:num"), "1")
        changed += 1
    if changed:
        for br in list(body.iter(qn("w:br"))):
            if br.get(qn("w:type")) == "column":
                br.getparent().remove(br)
    return changed
