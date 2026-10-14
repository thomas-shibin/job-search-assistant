"""Which Canadian province a free-text job location is in -- "Toronto, Ontario",
"Mississauga, Peel region", "Vancouver, BC", "Kelowna" -- using the province
names/codes themselves, then the location catalog's exact paths (a market scan
records e.g. Mississauga -> Canada / Ontario / Peel region / Mississauga).
"""
from __future__ import annotations

import re

from sqlalchemy.orm import Session

from app.catalog.seed_data import CANADIAN_PROVINCES
from app.models.catalog import SearchTerm, TermKind
from app.shared.terms import normalize_term

_CODE_BY_NAME = {normalize_term(name): code for name, code in CANADIAN_PROVINCES}
_CODES = {code for _, code in CANADIAN_PROVINCES}


def province_code(value: str | None) -> str | None:
    """"Ontario" / "ON" / "on" -> "ON"; anything else -> None."""
    if not value:
        return None
    if value.strip().upper() in _CODES:
        return value.strip().upper()
    return _CODE_BY_NAME.get(normalize_term(value))


def province_of(session: Session | None, location: str | None) -> str | None:
    """Province code for a job location, or None if it can't be told (e.g. "Remote")."""
    if not location:
        return None
    parts = [part.strip() for part in location.split(",") if part.strip()]
    for part in reversed(parts):  # "City, Province" -- the province is usually last
        if (code := province_code(part)):
            return code
    if session is None:
        return None

    # "Mississauga, Peel region": find a cataloged place named like one of the parts.
    wanted = [normalize_term(part) for part in parts]
    rows = session.query(SearchTerm.label, SearchTerm.geo_path).filter(
        SearchTerm.kind == TermKind.LOCATION.value, SearchTerm.geo_path.is_not(None)
    )
    by_place: dict[str, set[str]] = {}
    for label, geo_path in rows:
        if geo_path and len(geo_path) >= 2 and (code := province_code(geo_path[1])):
            by_place.setdefault(normalize_term(label.split(",")[0]), set()).add(code)
    for name in wanted:
        codes = by_place.get(name, set())
        if len(codes) == 1:  # ambiguous names (a "Richmond" in two provinces) don't count
            return next(iter(codes))
    return None


_STATED_LOCATION = re.compile(r"\b(?:location|based in|work location|office location)\s*[:\-]\s*([^\n;|]{2,80})", re.I)
_WORD = re.compile(r"[A-Za-zÀ-ÿ'.\-]+")


def _place(text: str, code: str) -> tuple[str | None, str]:
    """(city, province code) for a location text that resolved to `code`."""
    first = text.split(",")[0].strip()
    first = re.sub(r"\s*\(.*$", "", first).strip()  # "Toronto (hybrid)" -> "Toronto"
    if not first or province_code(first) or normalize_term(first) in {"remote", "hybrid", "canada"}:
        return None, code
    return first, code


def stated_place(session: Session | None, description: str | None) -> tuple[str | None, str] | None:
    """(city, province code) of a location the posting states itself ("Location:
    Vancouver folks its Hybrid..." -> ("Vancouver", "BC")). Tries the stated text's
    first words, longest first, so trailing prose doesn't hide the place name.
    """
    for match in _STATED_LOCATION.finditer(description or ""):
        text = match.group(1)
        if (code := province_of(session, text)):
            return _place(text, code)
        words = _WORD.findall(text)[:4]
        for n in range(len(words), 0, -1):
            candidate = " ".join(words[:n])
            if (code := province_of(session, candidate)):
                return _place(candidate, code)
    return None


def stated_province(session: Session | None, description: str | None) -> str | None:
    place = stated_place(session, description)
    return place[1] if place else None


def job_place(session: Session | None, description: str | None, listed: str | None) -> tuple[str | None, str] | None:
    """Where a job is: the location its description states, else its listed one."""
    if (place := stated_place(session, description)):
        return place
    if (code := province_of(session, listed)):
        return _place(listed or "", code)
    return None
