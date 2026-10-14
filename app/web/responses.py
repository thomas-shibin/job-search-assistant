"""Small helpers shared by route handlers across modules."""
from __future__ import annotations

from urllib.parse import urlencode

from fastapi import HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.core.config.current import user_config
from app.core.config.loader import ConfigError
from app.core.config.schema import AppConfig


def redirect(path: str, **params: str) -> RedirectResponse:
    """A 303 to `path`, with `params` (e.g. ``msg=...``) as its query string."""
    query = f"?{urlencode(params)}" if params else ""
    return RedirectResponse(url=f"{path}{query}", status_code=303)


def load_config_or_500(session: Session) -> AppConfig:
    """The user's settings for a settings page; invalid stored settings are a
    server error there (the page can't render a form from them)."""
    try:
        return user_config(session)
    except ConfigError as exc:
        raise HTTPException(status_code=500, detail=f"Your settings are invalid: {'; '.join(exc.errors)}")
