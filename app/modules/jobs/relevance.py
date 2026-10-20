"""How well a posting matches what was typed in the search box, so results can
be filtered and ordered by relevance rather than only by date.

- Whole words only: "hr" matches "HR Generalist", never "Threat Response".
  Common endings still count ("recruit" -> "Recruiter", "engineer" -> "Engineers"),
  except on words of three letters or fewer, where they'd make new words.
- Abbreviations and their full names are the same search ("HR" = "Human
  Resources" = "People Operations", "QA" = "Quality Assurance", ...), in either
  direction and inside longer titles ("HR Manager" = "Human Resources Manager").
- Pay rates aren't words: "$45/hr" in a title doesn't match "hr".
- Tiers: the phrase in the title (4) > all its words in the title (3) > the
  company's name (2) > the phrase in the description (1) > none of these (0).
  The company tier needs the search to *be* the company's name ("Shopify" for
  "Shopify Inc."); a word inside one doesn't count, because a staffing agency
  called "X HR Solutions" lists all kinds of jobs.
- Only RELEVANT (company tier) and above is shown: a job that just mentions
  the term in its description ("send your resume to our HR team") is left out.

Pure functions; text is compared after `normalize_term` (case, accents and
punctuation ignored).
"""
from __future__ import annotations

import re
from functools import lru_cache

from app.shared.terms import normalize_term

TITLE_PHRASE, TITLE_WORDS, COMPANY, DESCRIPTION, NONE = 4, 3, 2, 1, 0
RELEVANT = COMPANY  # the lowest tier search results show

# Each group is one meaning, written in normalized form (lowercase, "&" and
# punctuation dropped). Keep groups to unambiguous names: "PM" is left out
# because it means both product and project manager.
SYNONYMS: tuple[tuple[str, ...], ...] = (
    ("hr", "human resources", "human resource", "people and culture", "people culture", "people operations",
     "people ops", "rh", "ressources humaines"),
    ("hrbp", "hr business partner", "human resources business partner", "human resource business partner",
     "people partner"),
    ("hris", "hr information systems", "human resources information systems"),
    ("ta", "talent acquisition"),
    ("qa", "quality assurance"),
    ("ux", "user experience"),
    ("ui", "user interface"),
    ("sre", "site reliability engineer", "site reliability engineering"),
    ("swe", "software engineer", "software developer"),
    ("ml", "machine learning"),
    ("ai", "artificial intelligence"),
    ("bi", "business intelligence"),
    ("it", "information technology"),
    ("devops", "dev ops"),
    ("fullstack", "full stack"),
    ("frontend", "front end"),
    ("backend", "back end"),
    ("csm", "customer success manager"),
    ("ae", "account executive"),
    ("sdr", "sales development representative"),
    ("bdr", "business development representative"),
    ("vp", "vice president"),
    ("cto", "chief technology officer"),
    ("cfo", "chief financial officer"),
    ("coo", "chief operating officer"),
    ("ceo", "chief executive officer"),
)

_ENDINGS = "(?:s|es|er|ers|ing|ed)?"
_PAY_RATE = re.compile(r"(?<=[\d$])\s*(?:/|per\b)\s*(?:hr|hrs|hour|h)\b", re.IGNORECASE)  # "$45/hr", "20$/hr"
_MAX_VARIANTS = 12
# Left off a company's name before comparing it with a search ("Shopify Inc." = "Shopify").
_COMPANY_WORDS = frozenset(
    "inc incorporated ltd ltee llc llp corp corporation co company limited group technologies canada".split())


def _word(word: str) -> str:
    return re.escape(word) + (_ENDINGS if len(word) > 3 else "")


def _bounded(pattern: str) -> re.Pattern:
    return re.compile(r"(?<!\S)" + pattern + r"(?!\S)")


@lru_cache(maxsize=512)
def variants(term: str) -> tuple[tuple[str, ...], ...]:
    """The term and its synonym rewrites, each as a tuple of normalized words."""
    found = [normalize_term(term)]
    if not found[0]:
        return ()
    for group in SYNONYMS:
        for current in list(found):
            for phrase in group:
                pattern = _bounded(re.escape(phrase))
                if not pattern.search(current):
                    continue
                for other in group:
                    rewritten = " ".join(pattern.sub(other, current).split())
                    if rewritten not in found and len(found) < _MAX_VARIANTS:
                        found.append(rewritten)
                break
    return tuple(tuple(v.split()) for v in found)


@lru_cache(maxsize=1024)
def _phrase_pattern(words: tuple[str, ...]) -> re.Pattern:
    return _bounded(" ".join(_word(w) for w in words))


@lru_cache(maxsize=1024)
def _word_patterns(words: tuple[str, ...]) -> tuple[re.Pattern, ...]:
    return tuple(_bounded(_word(w)) for w in words)


def tier(terms: list[str] | tuple[str, ...], title: str | None, company: str | None = None,
         description: str | None = None) -> int:
    """The best tier any search term reaches on this posting."""
    title_n = normalize_term(_PAY_RATE.sub(" ", title or ""))
    company_n = " ".join(w for w in normalize_term(company or "").split() if w not in _COMPANY_WORDS)
    description_n = None  # normalized only when needed: descriptions are long
    best = NONE
    for term in terms:
        for words in variants(term):
            phrase = _phrase_pattern(words)
            if phrase.search(title_n):
                return TITLE_PHRASE
            if best < TITLE_WORDS and all(p.search(title_n) for p in _word_patterns(words)):
                best = TITLE_WORDS
            if best < COMPANY and company_n and phrase.fullmatch(company_n):
                best = COMPANY
            if best < DESCRIPTION and description:
                if description_n is None:
                    description_n = normalize_term(description)
                if phrase.search(description_n):
                    best = DESCRIPTION
    return best


def mentions(terms: list[str] | tuple[str, ...], *texts: str | None) -> bool:
    """Whether any term (or a synonym) appears as whole words in any of the texts."""
    normalized = [normalize_term(t or "") for t in texts]
    return any(_phrase_pattern(words).search(text)
               for term in terms for words in variants(term) for text in normalized)
