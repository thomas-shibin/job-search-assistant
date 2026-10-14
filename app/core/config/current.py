"""The configuration in effect for the signed-in user (their own sections plus
the platform's), loaded from the database with config/config.yaml as the
seed. Route handlers and workflows call `load_config(session)` instead of
repeating the path everywhere, so there is one place to point elsewhere --
tests replace `load_app_config` here."""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.core import paths
from app.core.config.schema import AppConfig
from app.core.config.store import load_app_config


def load_config(session: Session) -> AppConfig:
    """The merged settings for the session's user.

    Raises:
        ConfigError: If the stored settings don't validate.
    """
    return load_app_config(session, paths.DEFAULT_CONFIG_PATH)
