"""Schema-driven settings forms: `section_form` walks a pydantic model's fields
and produces a tree of nodes that `_settings_form.html` renders, and
`parse_form` turns the submitted fields back into the nested dict that
`app.config.store.save_section` validates. Driving both from the schema means a
new field in `app/config/schema.py` shows up on the Settings page by itself.

Form field names are dotted paths ("search_profiles.0.titles") plus a type
suffix the parser needs: ":lines" (textarea, one item per line), ":tags"
(a catalog picker, one value per selected item -- fields marked
`{"input": "tags"}` in the schema), ":bool"
(hidden "false" + checkbox "true"; the last value wins) and ":opt" (blank
means None). List items carry "_delete"/"_new" markers and dict entries a
"_key" field.
"""
from __future__ import annotations

import types
import typing
from typing import Any, Iterable

from pydantic import BaseModel


# Help text shown under a field, keyed by dotted path without list indices.
HELP: dict[str, str] = {
    "candidate.base_resume": "Path to your base resume (.docx or .pdf), relative to the project folder.",
    "candidate.override_parsed_name": "Use the name above instead of the one parsed from your resume header.",
    "search_profiles.locations": "One per line. Jobs from this profile's boards outside these are filtered out.",
    "search_profiles.address_profile": "Key of an address profile below; used on tailored resumes for this search.",
    "search_profiles.titles": "One per line. Used to score title similarity for jobs from this profile's boards.",
    "search_profiles.greenhouse_boards": "One per line -- the <token> in boards.greenhouse.io/<token>.",
    "search_profiles.lever_companies": "One per line -- the <company> in jobs.lever.co/<company>.",
    "address_profiles.street": "Optional. With a street, tailored resumes show the complete address.",
    "address_profiles.unit": "Optional: unit, suite, apartment or building.",
    "address_profiles.province": "Province or state, e.g. ON or Ontario. Jobs in this province use this address.",
    "address_profiles.postal_code": "Optional.",
    "search.titles": "Pick from the list or type your own and press Enter. Empty = any title.",
    "search.keywords": "Optional. Jobs must mention at least one of these, e.g. Java, Kafka. Empty = no filter.",
    "search.locations": "Cities, regions or provinces. \"Remote\" finds remote jobs anywhere in the country. Empty = anywhere.",
    "search.country": "Two-letter country code, e.g. ca, us, gb.",
    "search.adzuna_daily_limit": "Requests/day your Adzuna plan allows (free: ~250). Scheduled scans use at most 60%.",
    "search.scan_in_background": "Also fetch new matches on every scheduled scan (feeds Telegram watchlist alerts).",
    "matching.minimum_score": "Jobs scoring at least this are QUALIFIED and get a tailored resume.",
    "matching.negative_keywords": "One per line. Jobs whose title contains any of these are filtered out.",
    "matching.weights": "Must add up to 100.",
    "ai.enabled": "AI requirement extraction (billed OpenAI calls; needs OPENAI_API_KEY in .env).",
    "ai.provider": "openai or mock (free, offline).",
    "notifications.minimum_score": "Desktop notification for qualified jobs scoring at least this.",
    "scheduler.enabled": "Scan job sources automatically while `serve` / Docker is running.",
    "scheduler.interval_minutes": "Minutes between scans (60 = hourly). Takes effect immediately.",
    "watchlist.enabled": "Send Telegram alerts for new jobs matching any watch below.",
    "watchlist.max_alerts_per_run": "Past this many alerts in one scan, the rest come as one summary message.",
    "watchlist.dashboard_url": "Optional, e.g. http://127.0.0.1:8001 -- adds a dashboard link to alerts.",
    "watchlist.telegram_chat_id": "Filled in by Detect chat ID.",
}


_LABELS = {"search": "Job search", "ai": "AI", "dashboard_url": "Dashboard URL", "telegram_chat_id": "Telegram chat ID"}


def _label(name: str) -> str:
    return _LABELS.get(name) or name.replace("_", " ").capitalize()


def _unwrap_optional(annotation: Any) -> tuple[Any, bool]:
    if typing.get_origin(annotation) in (typing.Union, types.UnionType):
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return args[0], True
    return annotation, False


def _is_model(annotation: Any) -> bool:
    return isinstance(annotation, type) and issubclass(annotation, BaseModel)


def _blank(model: type[BaseModel]) -> dict:
    """Defaults for a new list/dict entry, with required string fields left empty."""
    return {
        name: ("" if field.is_required() else field.get_default(call_default_factory=True))
        for name, field in model.model_fields.items()
    }


def _model_children(model: type[BaseModel], value: dict, path: str, help_path: str, skip: Iterable[str] = ()) -> list[dict]:
    return [
        _field_node(field.annotation, name, value.get(name), f"{path}.{name}", f"{help_path}.{name}",
                    extra=field.json_schema_extra if isinstance(field.json_schema_extra, dict) else None)
        for name, field in model.model_fields.items()
        if name not in skip
    ]


def _field_node(annotation: Any, name: str, value: Any, path: str, help_path: str, extra: dict | None = None) -> dict:
    annotation, optional = _unwrap_optional(annotation)
    origin = typing.get_origin(annotation)
    node = {"label": _label(name), "help": HELP.get(help_path), "path": path}

    if extra and extra.get("input") == "tags":
        return {**node, "kind": "tags", "name": f"{path}:tags", "value": list(value or []), "catalog": extra["catalog"]}
    if annotation is bool:
        return {**node, "kind": "bool", "name": f"{path}:bool", "value": bool(value)}
    if annotation is int:
        return {**node, "kind": "int", "name": path, "value": value}
    if annotation is str:
        return {**node, "kind": "text", "name": f"{path}:opt" if optional else path, "value": value or ""}
    if origin is list:
        (item,) = typing.get_args(annotation)
        if _is_model(item):
            items = [
                {"children": _model_children(item, v, f"{path}.{i}", help_path), "path": f"{path}.{i}", "new": False}
                for i, v in enumerate(value or [])
            ]
            n = len(items)
            items.append({"children": _model_children(item, _blank(item), f"{path}.{n}", help_path),
                          "path": f"{path}.{n}", "new": True})
            return {**node, "kind": "list", "entries": items}
        return {**node, "kind": "lines", "name": f"{path}:lines", "value": "\n".join(value or [])}
    if origin is dict:
        _, item = typing.get_args(annotation)
        entries = list((value or {}).items()) + [("", _blank(item))]
        items = [
            {
                "key": key, "path": f"{path}.{i}", "new": i == len(entries) - 1,
                "children": _model_children(item, v, f"{path}.{i}", help_path),
            }
            for i, (key, v) in enumerate(entries)
        ]
        return {**node, "kind": "dict", "entries": items}
    if _is_model(annotation):
        return {**node, "kind": "group", "children": _model_children(annotation, value or {}, path, help_path)}
    return {**node, "kind": "text", "name": path, "value": "" if value is None else str(value)}


def section_form(annotation: Any, section: str, value: Any, skip: Iterable[str] = ()) -> dict:
    """Node tree for one top-level config section."""
    if _is_model(annotation):
        return {"kind": "group", "label": _label(section), "path": section, "help": HELP.get(section),
                "children": _model_children(annotation, value or {}, section, section, skip)}
    return _field_node(annotation, section, value, section, section)


def _coerce(suffix: str, raw: str) -> Any:
    if suffix == "lines":
        return [line.strip() for line in raw.splitlines() if line.strip()]
    if suffix == "bool":
        return raw == "true"
    if suffix == "opt":
        return raw.strip() or None
    return raw.strip()


def _finalize(node: Any) -> Any:
    if not isinstance(node, dict):
        return node
    node = {k: _finalize(v) for k, v in node.items()}
    if node and all(k.isdigit() for k in node):
        items = [node[k] for k in sorted(node, key=int)]
        kept = [
            item for item in items
            if not (isinstance(item, dict) and (item.get("_delete") or (item.get("_new") and not _identity(item))))
        ]
        if any(isinstance(item, dict) and "_key" in item for item in items):
            return {item.pop("_key"): _strip_markers(item) for item in kept}
        return [_strip_markers(item) for item in kept]
    return node


def _identity(item: dict) -> str:
    return item.get("_key") or item.get("name") or ""


def _strip_markers(item: Any) -> Any:
    if isinstance(item, dict):
        for marker in ("_delete", "_new"):
            item.pop(marker, None)
    return item


def parse_form(fields: Iterable[tuple[str, str]]) -> dict:
    """Submitted (name, value) pairs -> nested dict (see module docstring)."""
    root: dict = {}
    for raw_name, raw_value in fields:
        path, _, suffix = raw_name.partition(":")
        parts = path.split(".")
        target = root
        for part in parts[:-1]:
            target = target.setdefault(part, {})
        if suffix == "tags":  # one value per selected tag, plus an empty one so "none selected" still arrives
            values = target.setdefault(parts[-1], [])
            if raw_value.strip() and raw_value.strip() not in values:
                values.append(raw_value.strip())
            continue
        target[parts[-1]] = _coerce(suffix, raw_value)
    return _finalize(root)
