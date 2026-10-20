"""Tailors a resume by editing a copy of the candidate's own .docx in place:
every paragraph, table, font, colour, spacing, bullet style, header and page
setup stays exactly as designed -- only the wording of the paragraphs being
tailored (summary, experience bullets) changes, plus the location line when a
job calls for a different address.

Formatting inside a rewritten paragraph is kept word by word: the new text is
aligned against the old (difflib over words), and each new word takes the
formatting of the old word it lines up with -- so a bolded "Kafka" that
survives the rewrite stays bold, and inserted words borrow their neighbour's
formatting.
"""
from __future__ import annotations

import copy
import difflib
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from docx import Document
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph
from docx.text.run import Run

_BULLET_PREFIX = re.compile(r"^[\s•●○◦▪▫■□‣⁃∙·\-–—*]+")
_WORD = re.compile(r"\S+\s*|\s+")
_MC_FALLBACK = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"
_MAX_SPAN = 8  # a source text (e.g. a summary) may span up to this many consecutive paragraphs


@dataclass
class EditReport:
    replaced: int = 0
    unchanged: int = 0
    not_found: list[str] = field(default_factory=list)
    address_replaced: bool = False
    removed: int = 0


def _key(text: str) -> str:
    """Comparison key: bullet glyphs, case and spacing don't matter."""
    return " ".join(_BULLET_PREFIX.sub("", text).split()).casefold()


def _parts(doc) -> Iterator[tuple[object, bool]]:
    """The body, then each page header/footer (flagged True)."""
    yield doc.element.body, False
    for section in doc.sections:
        for part in (section.header, section.footer, section.first_page_header, section.first_page_footer):
            if not part.is_linked_to_previous:
                yield part._element, True


def iter_paragraphs(doc) -> Iterator[tuple[Paragraph, bool]]:
    """Every paragraph in reading order (body, tables, text boxes, then headers/
    footers), flagged True when it sits in a page header or footer."""
    for part, in_header in _parts(doc):
        for p in part.iter(qn("w:p")):
            # A text box is stored twice (modern + legacy fallback copy); edit the one Word shows.
            if not any(ancestor.tag == _MC_FALLBACK for ancestor in p.iterancestors()):
                yield Paragraph(p, None), in_header


def _all_paragraphs(doc) -> Iterator[Paragraph]:
    return (paragraph for paragraph, _ in iter_paragraphs(doc))


_EMU_PER_INCH = 914_400
PHOTO_MIN_INCHES = 0.6  # smaller pictures are icons (phone, email, LinkedIn), not a photo


def _photo_runs(doc, min_inches: float) -> list:
    """The runs holding a picture at least `min_inches` on both sides: a photo
    of the candidate (or a large logo), never the small contact icons."""
    found = []
    for part, _ in _parts(doc):
        for drawing in part.iter(qn("w:drawing")):
            extent = next(drawing.iter(qn("wp:extent")), None)
            if extent is None or not any(el.tag.endswith("}pic") for el in drawing.iter()):
                continue
            size = min(int(extent.get("cx", 0)), int(extent.get("cy", 0))) / _EMU_PER_INCH
            run = next((a for a in drawing.iterancestors() if a.tag == qn("w:r")), None)
            if size >= min_inches and run is not None and run not in found:
                found.append(run)
    return found


def count_photos(path: Path, min_inches: float = PHOTO_MIN_INCHES) -> int:
    return len(_photo_runs(Document(str(path)), min_inches))


def remove_photos(path: Path, min_inches: float = PHOTO_MIN_INCHES) -> int:
    """Take any photo out of the Word file at `path` (in place); returns how many."""
    doc = Document(str(path))
    runs = _photo_runs(doc, min_inches)
    for run in runs:
        run.getparent().remove(run)
    if runs:
        doc.save(str(path))
    return len(runs)


def _text_runs(paragraph: Paragraph) -> list:
    return [run for run in paragraph.runs if run.text]


def set_paragraph_text(paragraph: Paragraph, new_text: str) -> None:
    """Replace a paragraph's wording, keeping its paragraph formatting and, word
    by word, the character formatting of the text it replaces.
    """
    # Hyperlinked words would otherwise survive next to the new text: keep their
    # runs (and formatting) as plain runs; the link itself goes with the old wording.
    for link in paragraph._p.findall(qn("w:hyperlink")):
        for r in link.findall(qn("w:r")):
            link.addprevious(r)
        link.getparent().remove(link)
    runs = _text_runs(paragraph)
    if not runs:
        paragraph.add_run(new_text)
        return
    old_text = "".join(run.text for run in runs)
    prefix = _BULLET_PREFIX.match(old_text)
    if prefix and not _BULLET_PREFIX.match(new_text):
        new_text = prefix.group(0) + new_text  # keep a typed-in "• " bullet

    # Which run owns each old word.
    owner: list[int] = []
    old_words: list[str] = []
    position, run_starts = 0, []
    for run in runs:
        run_starts.append(position)
        position += len(run.text)
    for match in _WORD.finditer(old_text):
        old_words.append(match.group(0))
        owner.append(max(i for i, start in enumerate(run_starts) if start <= match.start()))

    new_words = _WORD.findall(new_text)
    assigned: list[int] = []
    matcher = difflib.SequenceMatcher(a=[w.strip().casefold() for w in old_words],
                                      b=[w.strip().casefold() for w in new_words], autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "delete":
            continue
        for offset in range(j2 - j1):
            if tag == "equal" or (tag == "replace" and i1 + offset < i2):
                assigned.append(owner[i1 + offset])
            else:  # inserted words: format like the word before them
                assigned.append(assigned[-1] if assigned else owner[min(i1, len(owner) - 1)])

    # Group consecutive words that share a source run into one new run each.
    groups: list[tuple[int, str]] = []
    for word, run_index in zip(new_words, assigned):
        if groups and groups[-1][0] == run_index:
            groups[-1] = (run_index, groups[-1][1] + word)
        else:
            groups.append((run_index, word))

    anchor = runs[0]._r
    for run_index, text in groups:
        new_r = copy.deepcopy(runs[run_index]._r)
        for child in list(new_r):
            if child.tag != qn("w:rPr"):
                new_r.remove(child)
        anchor.addprevious(new_r)
        Run(new_r, paragraph).text = text
    for run in runs:
        run._r.getparent().remove(run._r)


def _remove_paragraph(paragraph: Paragraph) -> None:
    element = paragraph._p
    element.getparent().remove(element)


def tailor_docx(
    base_path: Path, out_path: Path, replacements: list[tuple[str, str]], *,
    location_line: str | None = None, full_address: bool = False, remove: list[str] = (),
) -> EditReport:
    """Copy `base_path` to `out_path`, then for each (original text, new text)
    rewrite the paragraph(s) holding the original text. Text found in several
    consecutive paragraphs (a multi-paragraph summary) is split back across the
    same paragraphs at sentence breaks -- paragraphs are never removed or merged. `location_line` replaces the address line in
    the resume's header: a complete street address (`full_address`) as given,
    a "City, Province, Country" one in the shape the resume already uses.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(base_path, out_path)
    doc = Document(str(out_path))
    paragraphs = [p for p in _all_paragraphs(doc) if p.text.strip()]
    keys = [_key(p.text) for p in paragraphs]
    used: set[int] = set()
    report = EditReport()

    # Paragraphs the user chose to take out (never done without their approval).
    to_remove = []
    for original in remove:
        span = _find_span(keys, _key(original), used)
        if span is None:
            report.not_found.append(original)
            continue
        used.update(range(*span))
        to_remove.extend(paragraphs[span[0]:span[1]])

    for original, new in replacements:
        target = _key(original)
        if not target:
            continue
        span = _find_span(keys, target, used)
        if span is None:
            report.not_found.append(original)
            continue
        start, end = span
        used.update(range(start, end))
        if _key(new) == target and end - start == 1:
            report.unchanged += 1
            continue
        pieces = _split_across(new, [paragraphs[k].text for k in range(start, end)])
        if pieces is None:  # can't keep every paragraph: leave the passage as written
            report.not_found.append(original)
            continue
        for paragraph, piece in zip(paragraphs[start:end], pieces):
            set_paragraph_text(paragraph, piece)
        report.replaced += 1

    if location_line:
        report.address_replaced = _replace_location_line(paragraphs, location_line, full_address=full_address)
    for paragraph in to_remove:
        cell_paragraphs = paragraph._p.getparent().findall(qn("w:p"))
        if paragraph._p.getparent().tag == qn("w:tc") and len(cell_paragraphs) == 1:
            set_paragraph_text(paragraph, "")  # a table cell must keep one paragraph
        else:
            _remove_paragraph(paragraph)
        report.removed += 1

    doc.save(str(out_path))
    return report


_SENTENCE_END = re.compile(r"(?<=[.;!?])\s+")


def _split_across(new: str, originals: list[str]) -> list[str] | None:
    """Fit `new` into as many paragraphs as the original passage used, so no
    paragraph (bullet, line, date) is ever removed or merged. Breaks only between
    sentences, balancing lengths like the originals; None if `new` has too few
    sentences to fill every paragraph.
    """
    if len(originals) == 1:
        return [new]
    sentences = [x for x in _SENTENCE_END.split(new.strip()) if x]
    if len(sentences) < len(originals):
        return None
    total = sum(len(t) for t in originals) or 1
    pieces, index = [], 0
    for n, original in enumerate(originals):
        remaining_slots = len(originals) - n - 1
        piece = [sentences[index]]
        index += 1
        goal = len(" ".join(sentences)) * len(original) / total
        while index < len(sentences) - remaining_slots and len(" ".join(piece)) < goal * 0.75:
            piece.append(sentences[index])
            index += 1
        if remaining_slots == 0:
            piece.extend(sentences[index:])
        pieces.append(" ".join(piece))
    return pieces


def _find_span(keys: list[str], target: str, used: set[int]) -> tuple[int, int] | None:
    for i, key in enumerate(keys):
        if i in used or not key:
            continue
        if key == target:
            return i, i + 1
        if not target.startswith(key):
            continue
        joined = key
        for j in range(i + 1, min(i + _MAX_SPAN, len(keys))):
            if j in used:
                break
            joined = f"{joined} {keys[j]}"
            if joined == target:
                return i, j + 1
            if not target.startswith(joined):
                break
    return None


_PLACE = re.compile(r"^[A-Za-zÀ-ÿ.'\- ]{2,40}(,\s*[A-Za-zÀ-ÿ.'\- ]{2,40}){1,2}$")
_COUNTRIES = {"canada", "usa", "united states", "india", "uk", "united kingdom"}
_NOT_ADDRESS = re.compile(r"@|https?:|www\.|linkedin|github|\.com\b", re.I)
_PHONE = re.compile(r"^[\s+()\-.\d]{7,}$")
_STREET = re.compile(r"\d+[A-Za-z]?\s+[A-Za-zÀ-ÿ]")  # "1234 Main", "12B Elm"
_POSTAL = re.compile(r"\b[A-Z]\d[A-Z]\s?\d[A-Z]\d\b|\b\d{5}(?:-\d{4})?\b", re.I)  # Canadian / US


def _is_address_line(text: str) -> bool:
    """A header line holding the candidate's address: "Victoria, Canada" or a full
    "1234 Main St, Unit 5, Victoria, BC V8W 1A1" -- never the email/phone/links."""
    if not text or len(text) > 140 or "," not in text or _NOT_ADDRESS.search(text) or _PHONE.match(text):
        return False
    return bool(_PLACE.match(text) or _STREET.search(text) or _POSTAL.search(text))


def _replace_location_line(paragraphs: list[Paragraph], location_line: str, *, full_address: bool = False) -> bool:
    """Swap the address line in the header (the first lines, before any section) for `location_line`."""
    for paragraph in paragraphs[:12]:
        text = paragraph.text.strip()
        if not _is_address_line(text):
            continue
        parts = [p.strip() for p in text.split(",")]
        new_parts = [p.strip() for p in location_line.split(",")]
        if full_address or not _PLACE.match(text):
            replacement = location_line  # a complete address, or the resume already shows one
        elif len(parts) == 2 and parts[1].casefold() in _COUNTRIES and len(new_parts) >= 3:
            replacement = f"{new_parts[0]}, {new_parts[-1]}"  # "Victoria, Canada" -> "Toronto, Canada"
        elif len(parts) == 2 and len(new_parts) >= 2:
            replacement = f"{new_parts[0]}, {new_parts[1]}"
        else:
            replacement = location_line
        if _key(replacement) != _key(text):
            set_paragraph_text(paragraph, replacement)
        return True
    return False


# --- Layout-independent tailoring -------------------------------------------
# Which paragraphs of any Word resume are prose worth rewriting for a job: the
# summary lines, bullets and responsibilities (also inside tables), never names,
# headings, dates, contact details, table labels or bare skill lists.

_MIN_PROSE_WORDS = 6
_MIN_PROSE_CHARS = 35
_MONTH = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
# A date anywhere marks a role/education/period line, not prose to rewrite.
_HAS_DATE = re.compile(
    rf"\b\d{{1,2}}/\d{{2,4}}\b|\b\d{{1,2}}[.-](19|20)\d{{2}}\b|\b{_MONTH}\s*'?\d{{2,4}}\b|\b(19|20)\d{{2}}\s*[-–—to]+\s*((19|20)\d{{2}}|present|current|now|till|date)"
    r"|\b(till|to) date\b|\b(present|current)\s*$", re.I)


_LABEL = re.compile(r"^[^:.]{2,50}:\s+")
_CONTACT = re.compile(r"\S+@\S+\.\w|https?://|www\.|\b(linkedin|github)\.com\b|\+?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}", re.I)


@dataclass
class Passage:
    text: str             # the paragraph's text, without any typed bullet glyph
    section: str | None   # the heading/label above it, as context for the rewrite


def is_prose(text: str) -> bool:
    text = _BULLET_PREFIX.sub("", text).strip()
    words = text.split()
    if len(words) < _MIN_PROSE_WORDS or len(text) < _MIN_PROSE_CHARS:
        return False
    if _CONTACT.search(text) or _HAS_DATE.search(text) or text.isupper():
        return False
    body = _LABEL.sub("", text)  # "Cloud Computing: Skilled in ..." -- judge the part after the label
    body_words = body.split() or words
    separators = body.count(",") + body.count("|") + body.count("/") + body.count("•")
    lowercase_words = sum(1 for w in body_words if w[:1].islower())
    if separators >= len(body_words) / 3 and lowercase_words < len(body_words) / 3:
        return False  # a list of skills/tools, not a sentence
    # Title Case (ignoring acronyms like AWS): a job title, degree or certification, not a sentence.
    alphabetic = [w for w in body_words if w[:1].isalpha() and not (w.isupper() and len(w) > 1)]
    if alphabetic and sum(1 for w in alphabetic if w[:1].isupper()) / len(alphabetic) > 0.6:
        return False
    return True


def rewritable_passages(path: Path) -> list[Passage]:
    """The prose paragraphs of the resume body, in reading order, each with the
    nearest heading or label above it."""
    doc = Document(str(path))
    passages, section = [], None
    for p in doc.element.body.iter(qn("w:p")):
        if any(ancestor.tag == _MC_FALLBACK for ancestor in p.iterancestors()):
            continue
        text = Paragraph(p, None).text.strip()
        if not text:
            continue
        if is_prose(text):
            passages.append(Passage(text=_BULLET_PREFIX.sub("", text).strip(), section=section))
        elif len(text) <= 80:
            section = text
    return passages
