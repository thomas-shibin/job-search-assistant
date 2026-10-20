"""Where Canadian places are and how far apart, for the radius search
("jobs within 60 km of Victoria").

- The place list is app/modules/jobs/data/ca_places.tsv: Canadian towns and
  cities of 500+ people, from GeoNames (CC BY 4.0), rebuilt by
  scripts/build_places.py. It's read once, on first use. Nothing is looked up
  online, and nobody's own location is involved: distance is measured from the
  place they searched for.
- `center(label)`: the point a searched place stands for ("Victoria, BC",
  "Calgary region, AB", "Greater Vancouver", "Montreal"). Provinces, regions
  without a town of that name, and unknown names have none; they are searched
  by name as before.
- `points(location)`: the Canadian places a job's free-text location names
  ("Oakville, Ontario - Canada; Toronto, Ontario - Canada",
  "CA-Ontario-Toronto", "Kitchener-Waterloo, ON"). Parts naming US states or
  other countries are skipped, so "Bellevue, Washington" isn't read as a
  Canadian town.
- `within(location, remote, center, km)`: whether a job is in reach. A remote
  job is, unless it's limited to other provinces ("Remote (ON or AB only)").
"""
from __future__ import annotations

import math
import re
import threading
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.modules.jobs.catalog.geo import province_code
from app.shared.terms import normalize_term

DATA_FILE = Path(__file__).resolve().parent / "data" / "ca_places.tsv"
RADIUS_CHOICES = (10, 25, 50, 75, 100)  # km, offered next to the location box
MAX_RADIUS_KM = 200
_EARTH_RADIUS_KM = 6371.0

# Words around a place name that aren't part of it.
_NOISE = frozenset("""
    remote hybrid onsite on site office offices hq headquarters head area metro region regions greater city of the
    canada can ca anywhere only based in or and select locations location multiple various downtown
""".split())
_PREFIXES = ("greater ", "city of ", "ville de ")
_SUFFIXES = (" region", " city", " area", " metro", " metropolitan area", " cma", " census metropolitan area")
_ALIASES = {"sudbury": "greater sudbury", "kitchener waterloo": "kitchener", "quebec city": "quebec",
            "ville de quebec": "quebec", "tri cities": "coquitlam", "lower mainland": "vancouver",
            "gta": "toronto", "greater toronto": "toronto", "ncr": "ottawa", "national capital region": "ottawa"}
_US_STATES = (
    "alabama alaska arizona arkansas california colorado connecticut delaware florida georgia hawaii idaho "
    "illinois indiana iowa kansas kentucky louisiana maine maryland massachusetts michigan minnesota mississippi "
    "missouri montana nebraska nevada ohio oklahoma oregon pennsylvania tennessee texas utah vermont virginia "
    "washington wisconsin wyoming"
).split() + ["new hampshire", "new jersey", "new mexico", "new york", "north carolina", "north dakota",
             "rhode island", "south carolina", "south dakota", "west virginia", "district of columbia"]
_COUNTRIES = [
    "united states", "usa", "us", "u s", "united kingdom", "uk", "england", "scotland", "ireland", "israel",
    "india", "mexico", "brazil", "australia", "new zealand", "germany", "france", "spain", "portugal",
    "italy", "netherlands", "poland", "sweden", "norway", "denmark", "finland", "switzerland", "austria",
    "belgium", "japan", "china", "singapore", "philippines", "argentina", "colombia", "chile", "europe", "emea",
    "apac", "latam", "dc",
]
_FOREIGN = re.compile(r"(?<!\S)(?:" + "|".join(re.escape(n) for n in sorted(_US_STATES + _COUNTRIES, key=len,
                                                                          reverse=True)) + r")(?!\S)")
_SEGMENTS = re.compile(r"\s*(?:;|\||\n|\s/\s|\s+or\s+)\s*", re.IGNORECASE)
_PARTS = re.compile(r"\s*(?:[,()\[\]:]|\s[-–—]\s)\s*")
_CODE = re.compile(r"\b[A-Z]{2}\b")


@dataclass(frozen=True)
class Place:
    name: str
    province: str
    lat: float
    lon: float
    population: int

    @property
    def label(self) -> str:
        """How the job board is asked for it: "Victoria, BC"."""
        return f"{self.name}, {self.province}"


_lock = threading.Lock()
_index: dict[str, tuple[Place, ...]] | None = None


def _places() -> dict[str, tuple[Place, ...]]:
    """Normalized name -> the places with that name, most populous first."""
    global _index
    if _index is None:
        with _lock:
            if _index is None:
                found: dict[str, list[Place]] = {}
                for line in DATA_FILE.read_text(encoding="utf-8").splitlines():
                    if not line or line.startswith("#"):
                        continue
                    name, province, lat, lon, population = line.split("\t")
                    place = Place(name, province, float(lat), float(lon), int(population))
                    found.setdefault(normalize_term(name), []).append(place)
                _index = {key: tuple(sorted(group, key=lambda p: -p.population)) for key, group in found.items()}
    return _index


def distance_km(a: Place, b: Place) -> float:
    """Great-circle distance."""
    lat1, lon1, lat2, lon2 = map(math.radians, (a.lat, a.lon, b.lat, b.lon))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * _EARTH_RADIUS_KM * math.asin(math.sqrt(h))


def parse_radius(value) -> int | None:
    """A radius from a form or URL, in whole km (1-200), or None."""
    try:
        km = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None
    return min(km, MAX_RADIUS_KM) if km > 0 else None


def find(name: str, province: str | None = None) -> Place | None:
    """The place called `name` (in `province`, if given): the most populous one."""
    index = _places()
    key = normalize_term(name)
    keys = [key, _ALIASES.get(key, "")]
    for prefix in _PREFIXES:
        if key.startswith(prefix):
            keys.append(key[len(prefix):])
    for suffix in _SUFFIXES:
        if key.endswith(suffix):
            keys.append(key[: -len(suffix)])
    for candidate in filter(None, keys):
        for place in index.get(_ALIASES.get(candidate, candidate), ()):
            if province is None or place.province == province:
                return place
    return None


@lru_cache(maxsize=2048)
def center(label: str) -> Place | None:
    """The point a searched place stands for, or None (a province, or unknown)."""
    parts = [p.strip() for p in (label or "").split(",") if p.strip()]
    if not parts or province_code(parts[0]):
        return None
    province = next((code for part in parts[1:] if (code := province_code(part))), None)
    return find(parts[0], province)


_PROVINCE_NAMES = [(normalize_term(name), code) for name, code in (
    ("British Columbia", "BC"), ("Alberta", "AB"), ("Saskatchewan", "SK"), ("Manitoba", "MB"), ("Ontario", "ON"),
    ("Quebec", "QC"), ("New Brunswick", "NB"), ("Nova Scotia", "NS"), ("Prince Edward Island", "PE"),
    ("Newfoundland", "NL"), ("Yukon", "YT"), ("Northwest Territories", "NT"), ("Nunavut", "NU"))]


def provinces(location: str | None) -> frozenset[str]:
    """The provinces a job location names, by name or capitalized code
    ("Remote (ON, AB, BC, or NS Only)"); a lower-case "on" is just a word."""
    text = location or ""
    codes = {code for code in _CODE.findall(text) if province_code(code)}
    normalized = f" {normalize_term(text)} "
    codes |= {code for name, code in _PROVINCE_NAMES if f" {name} " in normalized}
    return frozenset(codes)


@lru_cache(maxsize=20000)
def points(location: str | None) -> tuple[Place, ...]:
    """Every Canadian place a job's location names, in order."""
    found: list[Place] = []
    for segment in _SEGMENTS.split(location or ""):
        if not segment.strip() or _FOREIGN.search(normalize_term(segment)):
            continue
        parts = [p for p in _PARTS.split(segment) if p.strip()]
        hint = next((code for p in reversed(parts) if (code := province_code(p.strip()))), None)
        for part in parts:
            place = _place_in(part, hint)
            if place is not None and place not in found:
                found.append(place)
    return tuple(found)


def _place_in(part: str, province: str | None) -> Place | None:
    words = [w for w in normalize_term(part).split() if w not in _NOISE]
    if not words or province_code(" ".join(words)):
        return None
    place = find(" ".join(words), province)
    if place is None and "-" in part:  # "CA-Ontario-Toronto", "Kitchener-Waterloo"
        pieces = [p for p in part.split("-") if p.strip()]
        hint = province or next((code for p in pieces if (code := province_code(p.strip()))), None)
        place = next((p for piece in pieces if (p := _place_in(piece, hint)) is not None), None)
    return place


def within(location: str | None, remote: bool, origin: Place, radius_km: float) -> bool:
    """Whether a job at `location` is within `radius_km` of `origin`. A remote
    job counts unless it's limited to provinces other than the origin's."""
    if any(distance_km(origin, place) <= radius_km for place in points(location)):
        return True
    if remote:
        named = provinces(location)
        return not named or origin.province in named
    return False
