"""Tests for DB-backed settings (app/config/store.py) and the schema-driven
settings form parser (app/ui/forms.py).
"""
from __future__ import annotations

import pytest
import yaml
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401  register all models on Base.metadata
from app.config.loader import ConfigError
from app.config.schema import AppConfig
from app.config.store import has_settings, import_yaml_config, load_app_config, save_section, save_watch
from app.database.base import Base
from app.models.settings import WatchlistEntry
from app.ui.forms import parse_form, section_form

YAML = {
    "candidate": {"name": "Test", "base_resume": "x.docx"},
    "address_profiles": {"bc": {"city": "Victoria", "province": "BC", "country": "Canada"}},
    "search_profiles": [{"name": "SP", "address_profile": "bc", "titles": ["Backend"]}],
    "scheduler": {"interval_minutes": 15},
    "watchlist": {"enabled": True, "watches": [{"name": "Dream", "companies": ["Shopify"]}]},
}


@pytest.fixture()
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture()
def yaml_path(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(YAML), encoding="utf-8")
    return path


def test_first_load_imports_only_platform_settings(session, yaml_path):
    """A file's personal values never land in whoever happens to load settings first."""
    config = load_app_config(session, yaml_path)

    assert has_settings(session)
    assert config.scheduler.interval_minutes == 15
    assert config.search_profiles == [] and config.watchlist.watches == []
    assert session.query(WatchlistEntry).count() == 0


def test_explicit_import_brings_in_the_users_own_settings(session, yaml_path):
    import_yaml_config(session, yaml_path)
    config = load_app_config(session, yaml_path)

    assert config.search_profiles[0].titles == ["Backend"]
    assert [w.name for w in config.watchlist.watches] == ["Dream"]
    assert session.query(WatchlistEntry).count() == 1


def test_db_wins_over_yaml_after_import(session, yaml_path):
    load_app_config(session, yaml_path)
    save_section(session, "scheduler", {"enabled": True, "interval_minutes": 60})
    yaml_path.write_text(yaml.safe_dump({**YAML, "scheduler": {"interval_minutes": 5}}), encoding="utf-8")

    assert load_app_config(session, yaml_path).scheduler.interval_minutes == 60


def test_missing_yaml_and_empty_db_uses_the_defaults(session, tmp_path):
    config = load_app_config(session, tmp_path / "nope.yaml")
    assert config.scheduler.interval_minutes == 15 and config.search.titles == []


def test_invalid_save_is_rejected_and_not_persisted(session, yaml_path):
    load_app_config(session, yaml_path)
    bad_weights = {**AppConfig.model_fields["matching"].default_factory().model_dump(), "weights": {"technical_skills": 99}}

    with pytest.raises(ConfigError):
        save_section(session, "matching", bad_weights)
    assert load_app_config(session, yaml_path).matching.weights.technical_skills == 30


def test_saving_watchlist_settings_keeps_watch_rows(session, yaml_path):
    import_yaml_config(session, yaml_path)
    returned = save_section(session, "watchlist", {"enabled": False, "max_alerts_per_run": 5, "telegram_chat_id": "42"})
    assert [w.name for w in returned.watchlist.watches] == ["Dream"]

    config = load_app_config(session, yaml_path)
    assert config.watchlist.telegram_chat_id == "42"
    assert [w.name for w in config.watchlist.watches] == ["Dream"]


def test_paused_watches_are_excluded_from_effective_config(session, yaml_path):
    import_yaml_config(session, yaml_path)
    entry = session.query(WatchlistEntry).one()
    entry.enabled = False
    session.commit()

    assert load_app_config(session, yaml_path).watchlist.watches == []


def test_save_watch_rejects_watch_without_criteria(session):
    with pytest.raises(ConfigError):
        save_watch(session, WatchlistEntry(), name="empty", enabled=True)


def test_reimport_replaces_settings(session, yaml_path):
    load_app_config(session, yaml_path)
    save_section(session, "scheduler", {"enabled": True, "interval_minutes": 60})

    import_yaml_config(session, yaml_path)

    assert load_app_config(session, yaml_path).scheduler.interval_minutes == 15


def test_parse_form_handles_types_lists_and_markers():
    fields = [
        ("search_profiles.0.name", "SP"),
        ("search_profiles.0.titles:lines", "Backend\n\n  Java  \n"),
        ("search_profiles.0.enabled:bool", "false"),
        ("search_profiles.0.enabled:bool", "true"),
        ("search_profiles.0._delete:bool", "false"),
        ("search_profiles.1.name", "Gone"),
        ("search_profiles.1._delete:bool", "true"),
        ("search_profiles.2._new", "1"),
        ("search_profiles.2.name", ""),
        ("watchlist.dashboard_url:opt", "  "),
    ]
    parsed = parse_form(fields)

    assert parsed["search_profiles"] == [{"name": "SP", "titles": ["Backend", "Java"], "enabled": True}]
    assert parsed["watchlist"]["dashboard_url"] is None


def test_parse_form_builds_dicts_from_keyed_entries():
    fields = [
        ("address_profiles.0._key", "bc"),
        ("address_profiles.0.city", "Victoria"),
        ("address_profiles.1._new", "1"),
        ("address_profiles.1._key", ""),
    ]
    assert parse_form(fields) == {"address_profiles": {"bc": {"city": "Victoria"}}}
    assert parse_form([("address_profiles.0._key", "bc"), ("address_profiles.0._delete:bool", "true")]) == {
        "address_profiles": {}
    }


def test_section_form_round_trips_through_parse_form():
    """Every rendered field name, submitted with its rendered value, saves back to the same section
    (ints arrive as strings; validation coerces them, exactly as save_section does).
    """
    config = AppConfig.model_validate({k: v for k, v in YAML.items() if k != "watchlist"})
    dumped = config.model_dump(mode="json")
    for section in ("search_profiles", "address_profiles", "matching", "scheduler", "candidate", "ai", "resume"):
        form = section_form(AppConfig.model_fields[section].annotation, section, dumped[section])
        resubmitted = AppConfig.model_validate({**dumped, section: parse_form(_submitted(form))[section]})
        assert resubmitted.model_dump(mode="json")[section] == dumped[section], section


def _submitted(node: dict) -> list[tuple[str, str]]:
    kind = node["kind"]
    if kind == "bool":
        return [(node["name"], "false")] + ([(node["name"], "true")] if node["value"] else [])
    if kind in ("int", "text", "lines"):
        return [(node["name"], "" if node["value"] is None else str(node["value"]))]
    if kind == "group":
        return [pair for child in node["children"] for pair in _submitted(child)]
    pairs = []
    for item in node["entries"]:
        if item["new"]:
            pairs.append((f"{item['path']}._new", "1"))
        if kind == "dict":
            pairs.append((f"{item['path']}._key", item["key"]))
        pairs += [pair for child in item["children"] for pair in _submitted(child)]
    return pairs
