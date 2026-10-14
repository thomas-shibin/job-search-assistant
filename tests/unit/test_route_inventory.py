"""Characterization test for the modular-monolith refactor: the app must
expose exactly the same URLs and methods as before (the snapshot was taken
from the pre-refactor app). Update the snapshot only for a deliberate route
change."""
from __future__ import annotations

import json
from pathlib import Path

SNAPSHOT = Path(__file__).resolve().parents[1] / "fixtures" / "routes_snapshot.json"


def _routes(routes):
    for route in routes:
        if type(route).__name__ == "_IncludedRouter":
            yield from _routes(route.original_router.routes)
        elif getattr(route, "methods", None):
            for method in sorted(route.methods - {"HEAD"}):
                yield f"{method} {route.path}"
        elif hasattr(route, "path"):
            yield f"MOUNT {route.path}"


def test_routes_are_unchanged():
    import main

    assert sorted(set(_routes(main.app.router.routes))) == json.loads(SNAPSHOT.read_text())
