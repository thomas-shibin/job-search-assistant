"""Comma-separated term lists ("Backend Engineer, Java Developer"), as typed into
the dashboard's search boxes and Settings.

Locations need care because they contain commas themselves: "Victoria, BC,
Vancouver, BC" must read as two locations, so `split_places` folds a bare
region code ("BC", "ON", "NY"), a Canadian province/territory name or a
country name back onto the place before it. Titles use plain `split_terms`,
where "Java Developer, QA" stays two titles.
"""
from __future__ import annotations

import re
import unicodedata

_COUNTRY_NAMES = {"canada", "usa", "united states", "uk", "united kingdom", "australia", "india", "germany"}
_PROVINCE_NAMES = {
    "british columbia", "alberta", "saskatchewan", "manitoba", "ontario", "quebec", "québec",
    "new brunswick", "nova scotia", "prince edward island", "newfoundland and labrador",
    "newfoundland", "yukon", "northwest territories", "nunavut",
}


def _is_region_suffix(token: str) -> bool:
    lowered = token.lower()
    return bool(re.fullmatch(r"[A-Z]{2,3}", token)) or lowered in _COUNTRY_NAMES or lowered in _PROVINCE_NAMES


def _tokens(text: str | None) -> list[str]:
    return [t.strip() for t in re.split(r"[,;\n]", text or "") if t.strip()]


def split_terms(text: str | None) -> list[str]:
    """Split on commas, semicolons and newlines; drop blanks and duplicates."""
    return list(dict.fromkeys(_tokens(text)))


def split_places(text: str | None) -> list[str]:
    """Like `split_terms`, but "Victoria, BC" / "Toronto, Ontario" stay one place."""
    places: list[str] = []
    for token in _tokens(text):
        if places and _is_region_suffix(token):
            places[-1] = f"{places[-1]}, {token}"
        else:
            places.append(token)
    return list(dict.fromkeys(places))


def join_terms(terms: list[str]) -> str:
    return ", ".join(terms)


def normalize_term(text: str) -> str:
    """Identity key for a term: accents, case, punctuation and spacing ignored,
    so "Québec", "quebec" and "Victoria,  BC" / "victoria bc" each match.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    ascii_only = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^0-9a-z+#]+", " ", ascii_only.casefold()).split())
