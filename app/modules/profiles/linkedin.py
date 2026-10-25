"""Reading a member's own LinkedIn profile into their Hirlumo profile.

LinkedIn's sign-in shares only name, email, photo and locale. Work history,
education and skills aren't available to apps: the full-profile API is for
LinkedIn partners, and the data-portability API only serves members in the
EU/EEA and Switzerland. So the member brings their own copy, either:

- the profile PDF (their profile > More > Save to PDF): instant, but it lists
  only their top skills; or
- the data export (Settings > Data privacy > Get a copy of your data): a ZIP
  with Profile.csv, Positions.csv, Education.csv and Skills.csv. It's complete,
  but LinkedIn takes from minutes to a day to prepare it. The CSV files on
  their own work too.

Files are read in memory and never stored. Nothing here talks to LinkedIn.
"""
from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import PurePosixPath
from statistics import median

MAX_UPLOAD_BYTES = 30_000_000  # all files together; a "particular data" export is a few KB
MAX_PDF_PAGES = 20
_MAX_CSV_BYTES = 5_000_000  # one file inside the ZIP, after decompressing
_MAX_ZIP_ENTRIES = 5_000
_MAX_ROWS = 300


class LinkedInImportError(ValueError):
    """The upload can't be read; the message is safe to show the member."""


@dataclass
class Position:
    title: str
    company: str
    location: str | None = None
    start: date | None = None
    end: date | None = None
    is_current: bool = False
    description: str | None = None


@dataclass
class School:
    institution: str
    degree: str | None = None
    field_of_study: str | None = None
    start: date | None = None
    end: date | None = None


@dataclass
class LinkedInProfile:
    first_name: str | None = None
    last_name: str | None = None
    headline: str | None = None
    summary: str | None = None
    location: str | None = None
    skills: list[str] = field(default_factory=list)
    positions: list[Position] = field(default_factory=list)
    schools: list[School] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.headline or self.location or self.skills or self.positions or self.schools)

    def merge(self, other: LinkedInProfile) -> None:
        """Fill what's still missing from `other` (a second file of the same export)."""
        for name in ("first_name", "last_name", "headline", "summary", "location"):
            if not getattr(self, name) and getattr(other, name):
                setattr(self, name, getattr(other, name))
        for name in ("skills", "positions", "schools"):
            if not getattr(self, name):
                setattr(self, name, getattr(other, name))


def read_files(files: list[tuple[str, bytes]]) -> LinkedInProfile:
    """Read uploaded LinkedIn files: a profile PDF, the export ZIP, or its CSVs.

    Raises LinkedInImportError with a message for the member when nothing can be read.
    """
    if not files:
        raise LinkedInImportError("Choose your LinkedIn profile PDF or data export file first.")
    if sum(len(data) for _, data in files) > MAX_UPLOAD_BYTES:
        raise LinkedInImportError(
            f"That's over {MAX_UPLOAD_BYTES // 1_000_000} MB. When you request your LinkedIn data, choose "
            "\"Want something in particular?\" and tick only Profile, Positions, Education and Skills.")
    # The export first: it's the complete record; a PDF only fills gaps.
    ordered = sorted(files, key=lambda f: f[1][:4] == b"%PDF")
    result = LinkedInProfile()
    problems: list[str] = []
    for name, data in ordered:
        try:
            result.merge(_read_one(name, data))
        except LinkedInImportError as exc:
            problems.append(str(exc))
    if result.empty:
        raise LinkedInImportError(problems[0] if problems else
                                  "We couldn't find any profile details in that file.")
    return result


def _read_one(name: str, data: bytes) -> LinkedInProfile:
    if data[:4] == b"PK\x03\x04":
        return _read_zip(data)
    if data[:4] == b"%PDF":
        return _read_pdf(data)
    if name.lower().endswith(".csv"):
        return _read_csvs({PurePosixPath(name).name.lower(): data})
    raise LinkedInImportError("Upload the PDF of your LinkedIn profile, or the ZIP (or CSV files) "
                              "from LinkedIn's data export.")


# ---- dates ----------------------------------------------------------------------------

_MONTHS = {m: i for i, names in enumerate((
    ("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"), ("may",), ("jun", "june"),
    ("jul", "july"), ("aug", "august"), ("sep", "sept", "september"), ("oct", "october"),
    ("nov", "november"), ("dec", "december")), start=1) for m in names}
_MONTH = r"(?:" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\.?"
_DATE = rf"(?:{_MONTH}\s+)?\d{{4}}"


def parse_date(text: str | None) -> date | None:
    """'Jan 2020', 'January 2020', '2020', '01/2020', '2020-01' -> the 1st of that
    month (January when only a year is given). Blank, 'Present' or nonsense -> None."""
    text = (text or "").strip().lower().rstrip(".")
    if not text or text == "present":
        return None
    year = month = None
    if m := re.fullmatch(rf"({_MONTH})\s+(\d{{4}})", text):
        month, year = _MONTHS[m.group(1).rstrip(".")], int(m.group(2))
    elif m := re.fullmatch(r"(\d{4})", text):
        month, year = 1, int(m.group(1))
    elif m := re.fullmatch(r"(\d{1,2})/(?:\d{1,2}/)?(\d{4})", text):
        month, year = int(m.group(1)), int(m.group(2))
    elif m := re.fullmatch(r"(\d{4})-(\d{1,2})(?:-\d{1,2})?", text):
        year, month = int(m.group(1)), int(m.group(2))
    if year is None or not 1 <= month <= 12 or not 1940 <= year <= date.today().year + 10:
        return None
    return date(year, month, 1)


def _span(start: date | None, end: date | None) -> tuple[date | None, date | None]:
    """An end before the start is a typo on the profile: keep the start only."""
    if start and end and end < start:
        return start, None
    return start, end


# ---- the data export (ZIP / CSV) --------------------------------------------------------

_WANTED = ("profile.csv", "positions.csv", "education.csv", "skills.csv")


def _read_zip(data: bytes) -> LinkedInProfile:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise LinkedInImportError("That ZIP file is damaged. Download it from LinkedIn again.") from exc
    with archive:
        entries = archive.infolist()
        if len(entries) > _MAX_ZIP_ENTRIES:
            raise LinkedInImportError("That ZIP has too many files to be a LinkedIn data export.")
        found: dict[str, bytes] = {}
        for entry in entries:
            base = PurePosixPath(entry.filename.replace("\\", "/")).name.lower()
            if base not in _WANTED or base in found or entry.is_dir():
                continue
            try:
                with archive.open(entry) as handle:
                    content = handle.read(_MAX_CSV_BYTES + 1)  # never trust the size the header claims
            except RuntimeError as exc:  # encrypted
                raise LinkedInImportError("That ZIP is password-protected. Upload LinkedIn's file as it "
                                          "was downloaded.") from exc
            except (zipfile.BadZipFile, OSError, EOFError) as exc:
                raise LinkedInImportError("That ZIP file is damaged. Download it from LinkedIn again.") from exc
            if len(content) > _MAX_CSV_BYTES:
                raise LinkedInImportError(f"{entry.filename} is too large to be a LinkedIn profile file.")
            found[base] = content
    if not found:
        raise LinkedInImportError("That ZIP doesn't contain LinkedIn profile files (Profile.csv, Positions.csv, "
                                  "Education.csv or Skills.csv). Request your data again and tick them.")
    return _read_csvs(found)


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def _key(name: str) -> str:
    return " ".join(name.replace("﻿", "").split()).lower()


def _rows(data: bytes, required: tuple[str, ...]) -> list[dict[str, str]]:
    """The CSV's rows, keyed by lower-case column name. LinkedIn sometimes puts
    a few 'Notes:' lines above the header, so the header is found, not assumed."""
    try:
        table = list(csv.reader(io.StringIO(_decode(data))))
    except csv.Error:
        return []
    for index, row in enumerate(table[:20]):
        keys = [_key(cell) for cell in row]
        if any(name in keys for name in required):
            return [{keys[i]: (cell or "").strip() for i, cell in enumerate(values) if i < len(keys)}
                    for values in table[index + 1:index + 1 + _MAX_ROWS] if any((c or "").strip() for c in values)]
    return []


def _pick(row: dict[str, str], *names: str) -> str:
    for name in names:
        if row.get(name):
            return row[name]
    return ""


def _clip(value: str | None, limit: int) -> str | None:
    value = " ".join((value or "").split())
    return value[:limit] or None


def _read_csvs(found: dict[str, bytes]) -> LinkedInProfile:
    out = LinkedInProfile()
    if "profile.csv" in found:
        for row in _rows(found["profile.csv"], ("first name", "headline"))[:1]:
            out.first_name = _clip(_pick(row, "first name"), 100)
            out.last_name = _clip(_pick(row, "last name"), 100)
            out.headline = _clip(_pick(row, "headline"), 200)
            out.summary = (_pick(row, "summary") or "").strip()[:5000] or None
            out.location = _clip(_pick(row, "geo location", "location"), 200)
    if "positions.csv" in found:
        for row in _rows(found["positions.csv"], ("company name", "title")):
            title, company = _clip(_pick(row, "title", "position"), 200), _clip(_pick(row, "company name", "company"), 200)
            if not (title or company):
                continue
            finished = _pick(row, "finished on", "end date", "ended on")
            start, end = _span(parse_date(_pick(row, "started on", "start date")), parse_date(finished))
            out.positions.append(Position(
                title=title or "Role", company=company or "Company", location=_clip(_pick(row, "location"), 200),
                start=start, end=end, is_current=not finished.strip() or finished.strip().lower() == "present",
                description=(_pick(row, "description") or "").strip()[:5000] or None))
    if "education.csv" in found:
        for row in _rows(found["education.csv"], ("school name", "degree name")):
            school = _clip(_pick(row, "school name", "school"), 200)
            if not school:
                continue
            start, end = _span(parse_date(_pick(row, "start date", "started on")),
                               parse_date(_pick(row, "end date", "finished on")))
            degree, field_of_study = _split_degree(_pick(row, "degree name", "degree"))
            field_of_study = field_of_study or _clip(_pick(row, "field of study"), 200)
            out.schools.append(School(institution=school, degree=degree, field_of_study=field_of_study,
                                      start=start, end=end))
    if "skills.csv" in found:
        out.skills = [s for row in _rows(found["skills.csv"], ("name", "skill"))
                      if (s := _clip(_pick(row, "name", "skill"), 80))]
    return out


def _split_degree(text: str) -> tuple[str | None, str | None]:
    """'Bachelor of Science - BS, Computer Science' -> ('Bachelor of Science - BS', 'Computer Science')."""
    text = " ".join((text or "").split())
    if not text:
        return None, None
    degree, _, field_of_study = text.partition(", ")
    return degree[:200] or None, field_of_study[:200] or None


# ---- the profile PDF ("Save to PDF") --------------------------------------------------

@dataclass
class _Line:
    text: str
    size: float


_PAGE_FOOTER = re.compile(r"^Page \d+ of \d+$", re.I)
_DATE_RANGE = re.compile(rf"^(?P<start>{_DATE})\s*[-–—]\s*(?P<end>{_DATE}|Present)(?:\s*\(.*\))?$", re.I)
_DURATION = re.compile(r"^(?:\d+ years?(?: \d+ months?)?|\d+ months?|less than a year)$", re.I)
_EDU_DETAIL = re.compile(r"^(?P<degree>.*?)\s*·?\s*\((?P<start>[^()\-–—]*?)\s*[-–—]\s*(?P<end>[^()]*?)\)\s*$")
_MAIN_HEADINGS = {"summary", "experience", "education", "volunteer experience", "licenses & certifications",
                  "honors & awards", "publications", "projects", "courses", "patents", "organizations",
                  "recommendations", "test scores", "languages", "causes", "skills"}
_SIDE_HEADINGS = {"contact", "top skills", "skills", "languages", "certifications", "honors-awards",
                  "publications", "patents", "summary"}


def _read_pdf(data: bytes) -> LinkedInProfile:
    import pdfplumber

    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            if len(pdf.pages) > MAX_PDF_PAGES:
                raise LinkedInImportError("That PDF is longer than a LinkedIn profile. On LinkedIn, open your "
                                          "profile and choose More > Save to PDF.")
            side: list[_Line] = []
            main: list[_Line] = []
            for page in pdf.pages:
                page_side, page_main = _page_columns(page)
                side += page_side
                main += page_main
    except LinkedInImportError:
        raise
    except Exception as exc:  # damaged or encrypted PDF
        raise LinkedInImportError("We couldn't open that PDF. Save it from LinkedIn again and retry.") from exc
    everything = " ".join(line.text for line in side + main).lower()
    if "linkedin.com/in/" not in everything and not any(line.text.lower() == "experience" for line in main):
        raise LinkedInImportError("That PDF doesn't look like a LinkedIn profile. On LinkedIn, open your "
                                  "profile and choose More > Save to PDF.")
    out = _parse_main(main)
    sections = _sections(side, _SIDE_HEADINGS)
    out.skills = [s for line in sections.get("top skills", []) + sections.get("skills", [])
                  if (s := _clip(line.text, 80))]
    return out


def _page_columns(page) -> tuple[list[_Line], list[_Line]]:
    """Split a page into LinkedIn's sidebar and main column. The main column's
    left edge is where most lines start, right of the first fifth of the page."""
    words = page.extract_words(extra_attrs=["size"])
    if not words:
        return [], []
    starts = [round(w["x0"]) for w in words if w["x0"] > page.width * 0.2]
    split = 0.0
    if starts:
        edge = max(set(starts), key=starts.count)
        if sum(1 for w in words if w["x1"] < edge - 2) > 0:
            split = edge - 2
    columns: tuple[list[_Line], list[_Line]] = ([], [])
    for column, words_in in enumerate(([w for w in words if w["x1"] <= split],
                                       [w for w in words if w["x1"] > split])):
        rows: dict[int, list[dict]] = {}
        for word in words_in:
            rows.setdefault(round(word["top"] / 3), []).append(word)
        for key in sorted(rows):
            row = sorted(rows[key], key=lambda w: w["x0"])
            text = " ".join(w["text"] for w in row).strip()
            if text and not _PAGE_FOOTER.match(text):
                columns[column].append(_Line(text, round(max(w["size"] for w in row), 1)))
    return columns


def _sections(lines: list[_Line], headings: set[str]) -> dict[str, list[_Line]]:
    """{heading: its lines}; lines before the first heading go under ''."""
    out: dict[str, list[_Line]] = {"": []}
    current = ""
    body = median([line.size for line in lines]) if lines else 0
    for line in lines:
        name = line.text.strip().lower()
        if name in headings and (line.size >= body or len(lines) < 3):
            current = name
            out.setdefault(current, [])
        else:
            out[current].append(line)
    return out


def _parse_main(lines: list[_Line]) -> LinkedInProfile:
    out = LinkedInProfile()
    sections = _sections(lines, _MAIN_HEADINGS)
    header = sections.get("", [])
    if header:
        name = max(header[:3], key=lambda line: line.size)
        first, _, last = name.text.partition(" ")
        out.first_name, out.last_name = _clip(first, 100), _clip(last, 100)
        rest = header[header.index(name) + 1:]
        if rest and _looks_like_place(rest[-1].text):
            out.location = _clip(rest[-1].text, 200)
            rest = rest[:-1]
        out.headline = _clip(" ".join(line.text for line in rest), 200)
    if "summary" in sections:
        out.summary = _join(sections["summary"])[:5000] or None
    out.positions = _parse_experience(sections.get("experience", []))
    out.schools = _parse_education(sections.get("education", []))
    return out


def _looks_like_place(text: str) -> bool:
    return len(text) <= 80 and ("," in text or text.lower().endswith(("area", "canada", "united states")))


def _join(lines: list[_Line]) -> str:
    """Wrapped PDF lines back into paragraphs; bullets start new lines."""
    out = ""
    for line in lines:
        text = line.text.strip()
        if out and text[:1] in "•-–*▪◦":
            out += "\n" + text
        else:
            out += (" " if out else "") + text
    return out.strip()


def _parse_experience(lines: list[_Line]) -> list[Position]:
    dates = [i for i, line in enumerate(lines) if _DATE_RANGE.match(line.text)]
    roles: list[tuple[int, int, Position]] = []  # (header start, content start, position)
    group: str | None = None  # the company several roles sit under
    last_meta = -1
    for d in dates:
        title_at = d - 1
        if title_at <= last_meta:
            continue  # two date lines in a row: not a role
        title_line = lines[title_at]
        c = title_at - 1
        header_start = title_at
        if c > last_meta and _DURATION.match(lines[c].text) and c - 1 > last_meta:
            group, company, header_start = lines[c - 1].text, lines[c - 1].text, c - 1
        elif c > last_meta and (lines[c].size > title_line.size + 0.2 or not roles or group is None):
            group, company, header_start = None, lines[c].text, c
        else:
            company = group or (roles[-1][2].company if roles else "Company")
        match = _DATE_RANGE.match(lines[d].text)
        start, end = _span(parse_date(match.group("start")), parse_date(match.group("end")))
        position = Position(title=_clip(title_line.text, 200) or "Role", company=_clip(company, 200) or "Company",
                            start=start, end=end, is_current=match.group("end").lower() == "present")
        content = d + 1
        if content < len(lines) and content not in dates and _looks_like_place(lines[content].text) \
                and lines[content].size <= title_line.size:
            position.location = _clip(lines[content].text, 200)
            content += 1
        last_meta = content - 1
        roles.append((header_start, content, position))
    for index, (_, content, position) in enumerate(roles):
        stop = roles[index + 1][0] if index + 1 < len(roles) else len(lines)
        position.description = _join(lines[content:stop])[:5000] or None
    return [position for _, _, position in roles]


def _parse_education(lines: list[_Line]) -> list[School]:
    schools: list[School] = []
    sizes = {line.size for line in lines}
    for line in lines:
        detail = _EDU_DETAIL.match(line.text)
        if detail and schools:
            school = schools[-1]
            school.degree, school.field_of_study = _split_degree(detail.group("degree").strip(" ·"))
            school.start, school.end = _span(parse_date(detail.group("start")), parse_date(detail.group("end")))
        elif not detail and (len(sizes) == 1 or line.size == max(sizes)):
            schools.append(School(institution=_clip(line.text, 200) or "School"))
        elif not detail and schools and not schools[-1].degree:
            # A degree line without dates, under its school.
            schools[-1].degree, schools[-1].field_of_study = _split_degree(line.text.strip(" ·"))
    return schools
