"""Build app/modules/jobs/data/ca_places.tsv -- the Canadian towns and cities
the radius search measures distance between (app/modules/jobs/places.py).

Source: GeoNames (https://www.geonames.org), licensed CC BY 4.0. Download these
two files from https://download.geonames.org/export/dump/ into one folder:

    cities500.zip          places with 500+ people (or cities500.txt, unzipped)
    admin1CodesASCII.txt   province names for the codes in the file above

then run:

    python scripts/build_places.py <that folder>

Only Canadian populated places are kept (not neighbourhoods, or abandoned or
historical places). Re-run it to refresh the list; the output is committed.
"""
from __future__ import annotations

import csv
import io
import sys
import unicodedata
import zipfile
from datetime import date
from pathlib import Path

OUTPUT = Path(__file__).resolve().parent.parent / "app" / "modules" / "jobs" / "data" / "ca_places.tsv"

# GeoNames feature codes kept: populated places, seats of government. Left out:
# PPLX (a section of a city), PPLH (historical), PPLQ (abandoned), PPLW (destroyed).
FEATURE_CODES = {"PPL", "PPLA", "PPLA2", "PPLA3", "PPLA4", "PPLA5", "PPLC", "PPLG", "PPLS", "PPLL", "PPLF", "PPLR"}

PROVINCE_CODES = {
    "alberta": "AB", "british columbia": "BC", "manitoba": "MB", "new brunswick": "NB",
    "newfoundland and labrador": "NL", "nova scotia": "NS", "ontario": "ON", "prince edward island": "PE",
    "quebec": "QC", "saskatchewan": "SK", "yukon": "YT", "northwest territories": "NT", "nunavut": "NU",
}


def _plain(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold().strip()


def _province_codes(folder: Path) -> dict[str, str]:
    """GeoNames admin1 code ("CA.02") -> postal code ("BC")."""
    codes: dict[str, str] = {}
    for line in (folder / "admin1CodesASCII.txt").read_text(encoding="utf-8").splitlines():
        key, name, ascii_name, _ = line.split("\t", 3)
        if key.startswith("CA."):
            code = PROVINCE_CODES.get(_plain(ascii_name)) or PROVINCE_CODES.get(_plain(name))
            if code is None:
                raise SystemExit(f"Unknown province in admin1CodesASCII.txt: {name!r}")
            codes[key.split(".", 1)[1]] = code
    if len(codes) != 13:
        raise SystemExit(f"Expected 13 provinces and territories, found {len(codes)}")
    return codes


SOURCE = "cities500"  # places with 500+ people


def _rows(folder: Path):
    text_path, zip_path = folder / f"{SOURCE}.txt", folder / f"{SOURCE}.zip"
    if text_path.exists():
        handle = text_path.open(encoding="utf-8", newline="")
    elif zip_path.exists():
        handle = io.TextIOWrapper(zipfile.ZipFile(zip_path).open(f"{SOURCE}.txt"), encoding="utf-8", newline="")
    else:
        raise SystemExit(f"{SOURCE}.zip not found in {folder}")
    with handle:
        yield from csv.reader(handle, delimiter="\t", quoting=csv.QUOTE_NONE)


def build(folder: Path) -> int:
    provinces = _province_codes(folder)
    places = []
    for row in _rows(folder):
        # geonameid, name, asciiname, alternatenames, latitude, longitude, feature class,
        # feature code, country code, cc2, admin1 code, ..., population, ...
        if len(row) < 15 or row[8] != "CA" or row[6] != "P" or row[7] not in FEATURE_CODES:
            continue
        province = provinces.get(row[10])
        if province is None:
            continue
        name = " ".join(row[1].split())
        if not name or "\t" in name:
            continue
        places.append((name, province, round(float(row[4]), 4), round(float(row[5]), 4), int(row[14] or 0)))
    places.sort(key=lambda p: (p[1], _plain(p[0]), -p[4]))
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open("w", encoding="utf-8", newline="\n") as out:
        out.write(f"# Canadian towns and cities of 500+ people: name, province, latitude, longitude, population.\n"
                  f"# Data from GeoNames (https://www.geonames.org), CC BY 4.0 "
                  f"(https://creativecommons.org/licenses/by/4.0/).\n"
                  f"# Built {date.today().isoformat()} by scripts/build_places.py from {SOURCE}; "
                  f"neighbourhoods and abandoned places left out.\n")
        for name, province, lat, lon, population in places:
            out.write(f"{name}\t{province}\t{lat}\t{lon}\t{population}\n")
    return len(places)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    print(f"Wrote {build(Path(sys.argv[1]))} places to {OUTPUT}")
