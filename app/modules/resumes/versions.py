"""Resume versions. Every change -- an upload, an edit, a Canadian conversion --
is saved as a new version and the old ones are kept, so a member can compare
any two and go back to any of them.

Going back makes that version the one in use again; nothing is deleted and no
version is renumbered. Analyses and tailored resumes keep pointing at the
version they were made from. A new version is always numbered after the
highest one, so going back to version 2 and then editing makes version 6, not
a second version 3.
"""
from __future__ import annotations

import difflib
import tempfile
from dataclasses import dataclass
from pathlib import Path

from markupsafe import Markup, escape
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.modules.resumes import canadian, uploads
from app.modules.resumes.diff import side_by_side_diff
from app.modules.resumes.models import CandidateProfile

CONTEXT = 1  # unchanged lines shown around each change when only changes are shown
SIMILAR = 0.5  # a replaced line this similar to the old one is shown as changed, word by word


def next_number(session: Session) -> int:
    return (session.query(func.max(CandidateProfile.version)).scalar() or 0) + 1


def all_versions(session: Session) -> list[CandidateProfile]:
    return session.query(CandidateProfile).order_by(CandidateProfile.version.desc()).all()


def by_number(session: Session, number: int) -> CandidateProfile | None:
    return (session.query(CandidateProfile).filter(CandidateProfile.version == number)
            .order_by(CandidateProfile.id.desc()).first())


def previous(session: Session, profile: CandidateProfile) -> CandidateProfile | None:
    return (session.query(CandidateProfile).filter(CandidateProfile.version < profile.version)
            .order_by(CandidateProfile.version.desc()).first())


def use(session: Session, profile: CandidateProfile) -> None:
    """Make `profile` the version in use."""
    session.query(CandidateProfile).filter(CandidateProfile.id != profile.id,
                                           CandidateProfile.is_active.is_(True)).update({"is_active": False})
    profile.is_active = True
    session.commit()


def file_bytes(profile: CandidateProfile) -> tuple[bytes, str] | None:
    """The version's file (from disk, else the copy kept in the database) and its suffix."""
    path = uploads.resume_file(profile)
    if path is not None:
        return path.read_bytes(), path.suffix.lower()
    if profile.source_file:
        return profile.source_file, Path(profile.raw_resume_path).suffix.lower() or ".pdf"
    return None


def lines(profile: CandidateProfile) -> list[str] | None:
    """The version's text, line by line, as the checks read it; None when its file is gone."""
    found = file_bytes(profile)
    if found is None:
        return None
    data, suffix = found
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / f"resume{suffix}"
        path.write_bytes(data)
        try:
            return [line.text for line in canadian.read(path).lines]
        except Exception:
            return None


# ---- comparing two versions ---------------------------------------------------------------------

@dataclass
class Row:
    kind: str  # same | changed | added | removed | moved_from | moved_to | skipped
    before: Markup = Markup("")
    after: Markup = Markup("")
    skipped: int = 0  # for "skipped": how many unchanged lines aren't shown
    old: str | None = None  # the plain text, for pairing lines up
    new: str | None = None


@dataclass
class Comparison:
    rows: list[Row]

    def count(self, *kinds: str) -> int:
        return sum(1 for row in self.rows if row.kind in kinds)

    @property
    def changed(self) -> int:
        return self.count("changed")

    @property
    def added(self) -> int:
        return self.count("added")

    @property
    def removed(self) -> int:
        return self.count("removed")

    @property
    def moved(self) -> int:
        return self.count("moved_to")

    @property
    def same(self) -> bool:
        return not any(row.kind not in ("same", "skipped") for row in self.rows)


def _text(value: str) -> Markup:
    return Markup(escape(value))


MAX_PAIRING = 40_000  # removed x added lines compared for rewording; beyond it they stay added and removed


def compare(old: list[str], new: list[str], *, only_changes: bool = True) -> Comparison:
    """Line by line. A line taken out in one place and put back unchanged in
    another is shown as moved; one put back reworded, as changed, word by word."""
    rows: list[Row] = []
    matcher = difflib.SequenceMatcher(a=old, b=new, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            rows += [Row("same", _text(line), _text(line)) for line in old[i1:i2]]
            continue
        rows += [Row("removed", before=_text(line), old=line) for line in old[i1:i2]]
        rows += [Row("added", after=_text(line), new=line) for line in new[j1:j2]]
    _pair(rows)
    rows = [row for row in rows if row.kind != "paired"]
    return Comparison(_collapse(rows) if only_changes else rows)


def _pair(rows: list[Row]) -> None:
    removed = [row for row in rows if row.kind == "removed"]
    added = [row for row in rows if row.kind == "added"]
    if not removed or not added:
        return
    # The same line elsewhere: moved.
    waiting: dict[str, list[Row]] = {}
    for row in removed:
        waiting.setdefault(row.old.strip(), []).append(row)
    for row in added:
        if waiting.get(row.new.strip()):
            row.kind = "moved_to"
            waiting[row.new.strip()].pop(0).kind = "moved_from"
    # A reworded line: changed, shown where the new version has it.
    removed = [row for row in removed if row.kind == "removed"]
    added = [row for row in added if row.kind == "added"]
    if len(removed) * len(added) > MAX_PAIRING:
        return
    scored = []
    for r in removed:
        for a in added:
            matcher = difflib.SequenceMatcher(a=r.old, b=a.new, autojunk=False)
            if matcher.real_quick_ratio() >= SIMILAR and matcher.quick_ratio() >= SIMILAR:
                ratio = matcher.ratio()
                if ratio >= SIMILAR:
                    scored.append((ratio, id(r), id(a), r, a))
    for _, _, _, r, a in sorted(scored, key=lambda item: item[0], reverse=True):
        if r.kind == "removed" and a.kind == "added":
            a.kind = "changed"
            a.before, a.after = side_by_side_diff(r.old, a.new)
            r.kind = "paired"


def _collapse(rows: list[Row]) -> list[Row]:
    """Unchanged lines more than CONTEXT away from a change become one "skipped" row."""
    near = set()
    for i, row in enumerate(rows):
        if row.kind != "same":
            near.update(range(i - CONTEXT, i + CONTEXT + 1))
    out: list[Row] = []
    for i, row in enumerate(rows):
        if row.kind != "same" or i in near:
            out.append(row)
        elif out and out[-1].kind == "skipped":
            out[-1].skipped += 1
        else:
            out.append(Row("skipped", skipped=1))
    return out
