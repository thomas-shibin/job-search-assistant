"""Reading what was typed in the job search box ("remote HR jobs in Toronto over
80k posted this week") into the search the page already runs: job titles,
places, and the results filters (workplace, job type, level, minimum salary,
date posted).

Rules only, no network: pure functions over a `Vocabulary` of known words (job
titles, companies and places from the search catalog and the job pool, built
in app/modules/jobs/interpret.py). Words the rules can't place are reported in
`Reading.unknown`; that is when interpret.py may ask the analysis provider.

- Places are known names ("Toronto", "Montreal", "Toronto, ON"), with or
  without "in". A place that is also a job word needs "in" in front of it.
- "senior", "lead" and "principal" stay in the title, which is how postings say
  it. "junior", "entry level", "new grad" and "mid level" become the Level filter.
- "contract" and "temporary" are a job type only on their own ("contract
  jobs"), so "Contract Manager" stays a title. "data entry" stays a title too.
- A distance ("within 60 km of Victoria", "25 miles from Calgary") becomes the
  search radius around the place (app/modules/jobs/places.py).
- Spelling: a word that no job title uses is corrected to the closest common
  word starting with the same letter ("enginer" -> "engineer"), when it's at
  least 5 letters long and close enough. Shorter words and abbreviations
  ("hr", "qa") are left alone.
"""
from __future__ import annotations

import difflib
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from functools import cached_property
from typing import Iterable

from app.modules.jobs.places import parse_radius
from app.modules.jobs.relevance import SYNONYMS, split_titles
from app.shared.terms import normalize_term, split_terms

DATE_STEPS = (1, 3, 7, 14, 30)  # the Date posted filter's choices (postings.DATE_RANGES)
_MIN_SALARY, _MAX_SALARY = 10_000, 1_000_000
_HOURS_A_YEAR = 2080
_CORRECT_MIN_LENGTH = 5
_CORRECT_CUTOFF = 0.84
_ABBREVIATIONS = frozenset(word for group in SYNONYMS for phrase in group for word in phrase.split())

# Words that only frame a search ("show me jobs for ..."); trimmed from both ends
# of what's left, never from inside a title ("Head of Sales").
_FILLERS = frozenset("""
    a an the any some all me i im i'm my we us want wanted need needed looking look find finding search searching
    show get give please job jobs role roles position positions opening openings vacancy vacancies career careers
    work working opportunity opportunities posting postings listing listings hiring available
    in near around at for with that which who where of to on as from and or based located close
    pay pays paying salary over above least more than per only something anything type kind new
""".split())

_PREPOSITION = r"(?:\b(?:in|near|around|at|based in|located in|close to)\s+)"
_NUMBER = r"(?:\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)"
_MONEY = re.compile(
    r"(?:\b(?:over|above|at least|minimum(?: of)?|min|more than|starting at|from|paying|pays|making|earning"
    r"|salary(?: of)?)\s+)?"
    rf"(\$\s*)?({_NUMBER})\s*(k\b)?"
    rf"(?:\s*(?:-|to)\s*\$?\s*{_NUMBER}\s*(k\b)?)?"
    r"(?:\s*(?:/\s*|per\s+|an?\s+)(year|yr|annum|hour|hr)\b|\s+(annually|hourly)\b)?\s*\+?"
)
_RADIUS = re.compile(
    r"\b(?:(?:within|inside|in\s+a|up\s+to|less\s+than|under|max(?:imum)?)\s+)?(\d{1,3})\s*"
    r"(km|kms|kilomet(?:er|re)s?|mi|miles?)\b(?:\s+(?:radius|away))?(?:\s+(?:of|from|around|near))?"
)
_KM_PER_MILE = 1.609
_DAYS: tuple[tuple[re.Pattern, object], ...] = (
    (re.compile(r"\b(?:posted\s+)?(?:in\s+the\s+)?(?:last|past)\s+(\d+)\s+(day|week|month)s?\b"),
     lambda m: int(m[1]) * {"day": 1, "week": 7, "month": 30}[m[2]]),
    (re.compile(r"\b(?:posted\s+)?(?:in\s+the\s+)?(?:last|past)\s+(?:24\s+hours|day)\b|\b(?:posted\s+)?today\b"), 1),
    (re.compile(r"\b(?:posted\s+)?(?:in\s+the\s+)?(?:last|past)\s+(?:two|couple(?:\s+of)?)\s+weeks\b"), 14),
    (re.compile(r"\b(?:posted\s+)?(?:this|last|past)\s+week\b"), 7),
    (re.compile(r"\b(?:posted\s+)?(?:this|last|past)\s+month\b"), 30),
)
_ON_ITS_OWN = r"(?=\s+(?:jobs?|roles?|positions?|work|gigs?|basis|openings?)\b|\s*(?:$|[,;|]))"
_CHOICES: tuple[tuple[str, re.Pattern, str], ...] = (
    ("workplaces", re.compile(r"\b(?:fully\s+)?(?:remote(?:ly)?|work(?:ing)?\s+from\s+home|wfh)\b"), "remote"),
    ("workplaces", re.compile(r"\bhybrid\b"), "hybrid"),
    ("workplaces", re.compile(r"\b(?:on-?site|in[- ]office|in[- ]person)\b"), "onsite"),
    ("employment_types", re.compile(r"\b(?:full[- ]?time|permanent)\b"), "full_time"),
    ("employment_types", re.compile(r"\bpart[- ]?time\b"), "part_time"),
    ("employment_types", re.compile(r"\b(?:internships?|co-?ops?|interns?)\b"), "internship"),
    ("employment_types", re.compile(r"\bfreelance\b|\b(?:on\s+)?contract(?:s|ors?)?" + _ON_ITS_OWN), "contract"),
    ("employment_types", re.compile(r"\b(?:temporary|temp)" + _ON_ITS_OWN), "temporary"),
    ("levels", re.compile(r"\b(?:junior|jr\b\.?|entry[- ]level|new\s+grads?|recent\s+grads?)"), "entry"),
    ("levels", re.compile(r"\b(?:mid[- ]level|intermediate)\b"), "mid"),
)


def fold(text: str) -> str:
    """Lowercase without accents, one character for each character of `text`,
    so a match's position in the folded text is its position in the original."""
    return "".join(unicodedata.normalize("NFKD", ch)[0].lower()[0] for ch in text)


@dataclass
class Vocabulary:
    words: dict[str, int]  # word -> how many titles, companies and search terms use it
    places: dict[str, str]  # place name -> its label, e.g. "toronto" -> "Toronto, ON"
    job_words: frozenset[str] = frozenset()  # words of catalog titles and keywords

    @classmethod
    def build(cls, texts: Iterable[tuple[str, int]], places: Iterable[tuple[str, int]],
              job_terms: Iterable[str] = ()) -> "Vocabulary":
        """`texts`: (title, company or term, weight); `places`: (location label, popularity)."""
        words: Counter[str] = Counter()
        for text, weight in texts:
            for word in normalize_term(text or "").split():
                if word.isalpha() and len(word) > 1:
                    words[word] += max(weight, 1)
        best: dict[str, tuple[int, int, str]] = {}
        for label, popularity in places:
            head = label.split(",")[0]
            # "Victoria" is "Victoria, BC" before "Victoria region, NB": a name as
            # written beats one with "region" dropped, then the more popular wins.
            keys = ((2, normalize_term(label)), (1, normalize_term(head)),
                    (0, normalize_term(re.sub(r"\s+region$", "", head, flags=re.IGNORECASE))))
            for rank, key in keys:
                if len(key) > 2 and (key not in best or (rank, popularity or 0) > best[key][:2]):
                    best[key] = (rank, popularity or 0, label)
        for key in best:  # place names are words too, for spelling ("toronot")
            for word in key.split():
                if word.isalpha() and len(word) > 1:
                    words[word] += 1
        job_words = frozenset(w for term in job_terms for w in normalize_term(term).split())
        return cls(words=dict(words), places={key: label for key, (_, _, label) in best.items()}, job_words=job_words)

    def knows(self, word: str) -> bool:
        if word in self.words or word in _ABBREVIATIONS:
            return True
        return any(word.endswith(end) and word[:-len(end)] in self.words for end in ("s", "es"))

    @cached_property
    def _by_letter(self) -> dict[str, list[str]]:
        groups: dict[str, list[str]] = {}
        for word in self.words:
            if len(word) >= 4:
                groups.setdefault(word[0], []).append(word)
        return groups

    def correct(self, word: str) -> str | None:
        """The word that was probably meant, or None (known already, or no close word)."""
        if len(word) < _CORRECT_MIN_LENGTH or not word.isalpha() or self.knows(word):
            return None
        close = difflib.get_close_matches(word, self._by_letter.get(word[0], ()), n=5, cutoff=_CORRECT_CUTOFF)
        if not close:
            return None
        scored = [(difflib.SequenceMatcher(None, word, c).ratio(), c) for c in close]
        top = max(score for score, _ in scored)
        # Among the closest few, the commonest: "recruter" -> "recruiter", not "recruteur".
        return max((c for score, c in scored if score >= top - 0.03), key=lambda c: self.words[c])

    @cached_property
    def _place_pattern(self) -> re.Pattern | None:
        if not self.places:
            return None
        names = sorted(self.places, key=len, reverse=True)
        alternation = "|".join(r"[\s,.]+".join(map(re.escape, name.split())) for name in names)
        return re.compile(rf"({_PREPOSITION})?\b({alternation})\b")

    def place(self, name: str) -> str | None:
        return self.places.get(normalize_term(name))


@dataclass
class Reading:
    """What a typed search means, in the terms the results page uses."""

    titles: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    workplaces: list[str] = field(default_factory=list)
    employment_types: list[str] = field(default_factory=list)
    levels: list[str] = field(default_factory=list)
    min_salary: int | None = None
    days: int | None = None
    radius_km: int | None = None  # the distance around the places
    corrections: list[tuple[str, str]] = field(default_factory=list)  # (typed, corrected)
    unknown: list[str] = field(default_factory=list)  # words neither known nor corrected

    @property
    def has_filters(self) -> bool:
        return bool(self.workplaces or self.employment_types or self.levels or self.min_salary or self.days)

    @property
    def is_empty(self) -> bool:
        return not (self.titles or self.locations or self.has_filters or self.radius_km)

    def same_as(self, text: str) -> bool:
        """Nothing to change: `text` is already plain titles."""
        return (self.titles == split_terms(text) and not self.locations and not self.has_filters
                and not self.radius_km and not self.corrections)

    def add(self, name: str, value: str) -> None:
        values = getattr(self, name)
        if value not in values:
            values.append(value)


def _snap_days(days: int) -> int | None:
    return next((step for step in DATE_STEPS if step >= days), None)


def _money(match: re.Match) -> int | None:
    dollar, number, unit, adverb = match[1], match[2], match[5], match[6]
    thousand = match[3] or match[4]  # "70-90k" is 70k to 90k
    value = float(number.replace(",", ""))
    hourly = unit in ("hour", "hr") or adverb == "hourly"
    if thousand:
        value *= 1000
    elif hourly and dollar and value < 500:
        value *= _HOURS_A_YEAR
    if not (dollar or thousand or "," in number or value >= _MIN_SALARY):
        return None  # a bare number: "Class 1", "5 years"
    return int(value) if _MIN_SALARY <= value <= _MAX_SALARY else None


def read(text: str, vocab: Vocabulary) -> Reading:
    """Read a typed search. Recognized phrases are taken out of the text; what's
    left, split at commas, "or", "and", "&" and "/" (relevance.split_titles),
    is job titles."""
    reading = Reading()
    folded = fold(text)
    taken = [False] * len(text)

    def free(match: re.Match) -> bool:
        return not any(taken[match.start():match.end()])

    def take(match: re.Match) -> None:
        for i in range(match.start(), match.end()):
            taken[i] = True

    for match in _RADIUS.finditer(folded):
        if free(match):
            km = int(match[1]) * (_KM_PER_MILE if match[2].startswith("mi") else 1)
            reading.radius_km = parse_radius(round(km))
            take(match)
    for pattern, days in _DAYS:
        for match in pattern.finditer(folded):
            if free(match):
                value = _snap_days(days(match) if callable(days) else days)
                if value:
                    reading.days = min(reading.days or value, value)
                take(match)
    for match in _MONEY.finditer(folded):
        if free(match) and (salary := _money(match)):
            reading.min_salary = max(reading.min_salary or 0, salary)
            take(match)
    for name, pattern, value in _CHOICES:
        for match in pattern.finditer(folded):
            if free(match):
                reading.add(name, value)
                take(match)
    if vocab._place_pattern is not None:
        for match in vocab._place_pattern.finditer(folded):
            key = normalize_term(match[2])
            if not free(match):
                continue
            if not match[1] and " " not in key and key in vocab.job_words:
                continue  # "Mission", "Victoria": a place only after "in"
            reading.add("locations", vocab.places[key])
            take(match)

    rest = "".join("|" if taken[i] else ch for i, ch in enumerate(text))
    for piece in re.split(r"[|,;]|\s+or\s+", rest, flags=re.IGNORECASE):
        words = piece.split()
        while words and _filler(words[0]):
            words.pop(0)
        while words and _filler(words[-1]):
            words.pop()
        if not words:
            continue
        kept = []
        for word in words:
            fixes = len(reading.corrections)
            checked = _checked(word, vocab, reading)
            if len(reading.corrections) > fixes and checked in vocab.places and checked not in vocab.job_words:
                reading.add("locations", vocab.places[checked])  # "toronot" -> Toronto
            else:
                kept.append(checked)
        if not kept:
            continue
        title = " ".join(kept)
        place = vocab.place(title)
        if place:  # "Data analyst, Toronto", or "toronot" corrected to a place
            reading.add("locations", place)
            continue
        for one in split_titles(title):  # "HR and Admin": two titles
            if one.lower() not in (t.lower() for t in reading.titles):
                reading.titles.append(one)
    return reading


def _core(word: str) -> str:
    return fold(word).strip(".,!?:;'\"()[]")


def _filler(word: str) -> bool:
    core = _core(word)
    return not core or core in _FILLERS or not any(ch.isalnum() for ch in core)


def _checked(word: str, vocab: Vocabulary, reading: Reading) -> str:
    """The word, or its spelling correction; notes words nobody's title uses."""
    core = normalize_term(word)
    if not core or not core.isalpha() or vocab.knows(core):
        return word.strip(".,!?:;\"()[]")
    corrected = vocab.correct(core)
    if corrected:
        reading.corrections.append((word, corrected))
        return corrected
    reading.unknown.append(word)
    return word.strip(".,!?:;\"()[]")
