"""Radius search's places (app/modules/jobs/places.py): where a searched place
is, which Canadian places a job's free-text location names, and distances."""
from __future__ import annotations

import pytest

from app.modules.jobs import places


@pytest.mark.parametrize("label, expected", [
    ("Victoria, BC", "Victoria, BC"),
    ("Victoria", "Victoria, BC"),  # the larger Victoria (not the one in NL)
    ("Victoria, NL", "Victoria, NL"),
    ("Calgary region, AB", "Calgary, AB"),
    ("Greater Vancouver, BC", "Vancouver, BC"),
    ("Montreal", "Montréal, QC"),
    ("Québec City, QC", "Québec, QC"),
    ("St. John's, NL", "St. John's, NL"),
    ("Sudbury", "Greater Sudbury, ON"),
])
def test_searched_places_have_a_centre(label, expected):
    assert places.center(label).label == expected


@pytest.mark.parametrize("label", ["British Columbia", "Ontario", "Remote", "Nowhereville", ""])
def test_provinces_and_unknown_places_have_none(label):
    assert places.center(label) is None


def test_distances_around_victoria():
    victoria = places.center("Victoria, BC")

    assert 40 < places.distance_km(victoria, places.find("Duncan", "BC")) < 50
    assert 85 < places.distance_km(victoria, places.find("Nanaimo", "BC")) < 100
    assert places.distance_km(victoria, victoria) == 0


@pytest.mark.parametrize("location, expected", [
    ("Toronto, Canada", ["Toronto, ON"]),
    ("Vancouver, British Columbia, Canada", ["Vancouver, BC"]),
    ("CA-Ontario-Toronto", ["Toronto, ON"]),
    ("Toronto Headquarters", ["Toronto, ON"]),
    ("Kitchener-Waterloo, ON; Toronto, ON", ["Kitchener, ON", "Toronto, ON"]),
    ("Oakville, Ontario - Canada; Toronto, Ontario - Canada; Waterloo, Ontario - Canada",
     ["Oakville, ON", "Toronto, ON", "Waterloo, ON"]),
    ("Bellevue, Washington; Chicago, Illinois; Toronto, Ontario, Canada", ["Toronto, ON"]),  # US places skipped
    ("US-Remote; US-Chicago; CA-Toronto; CA-Remote", ["Toronto, ON"]),
    ("Remote Canada", []),
    ("Canada", []),
    ("N/A", []),
    ("London, United Kingdom", []),
    (None, []),
])
def test_places_named_in_job_locations(location, expected):
    assert [p.label for p in places.points(location)] == expected


def test_provinces_named_in_job_locations():
    assert places.provinces("Canada - Remote (ON, AB, BC, or NS Only)") == {"ON", "AB", "BC", "NS"}
    assert places.provinces("Remote - Ontario, Canada") == {"ON"}
    assert places.provinces("Remote Canada") == set()
    assert places.provinces("Toronto, on site") == set()  # "on" in lower case is a word, not Ontario


def test_within_a_radius():
    victoria = places.center("Victoria, BC")

    assert places.within("Duncan, BC", False, victoria, 60)
    assert not places.within("Nanaimo, BC", False, victoria, 60)
    assert places.within("Nanaimo, BC", False, victoria, 100)
    assert places.within("Toronto, ON; Duncan, BC", False, victoria, 60)  # any of its places
    assert not places.within("Canada", False, victoria, 60)  # can't be placed


def test_remote_jobs_are_within_reach_unless_limited_to_other_provinces():
    victoria = places.center("Victoria, BC")

    assert places.within("Remote Canada", True, victoria, 25)
    assert places.within("Canada - Remote (ON, AB, BC, or NS Only)", True, victoria, 25)
    assert not places.within("Remote - Ontario, Canada", True, victoria, 25)


@pytest.mark.parametrize("value, expected", [
    ("60", 60), (25, 25), ("25.0", 25), ("500", places.MAX_RADIUS_KM), ("0", None), ("-5", None), ("", None),
    (None, None), ("far", None),
])
def test_parse_radius(value, expected):
    assert places.parse_radius(value) == expected


def test_the_place_list_is_credited_and_complete():
    header = places.DATA_FILE.read_text(encoding="utf-8").split("\n", 3)[:3]
    assert any("GeoNames" in line and "CC BY 4.0" in line for line in header)
    provinces = {p.province for group in places._places().values() for p in group}
    assert provinces == {"AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT"}
