from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

STATIC_DIR = Path(__file__).resolve().parent / "app" / "ui" / "static"


def create_app() -> FastAPI:
    fastapi_app = FastAPI(title="Local AI Job Application Assistant")

    @fastapi_app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    from app.api.routes import router as api_router
    from app.ui.routes import router as ui_router
    from app.ui.settings_routes import router as settings_router

    @fastapi_app.on_event("startup")
    def seed_search_catalog() -> None:
        # Built-in titles/keywords/locations for the search pickers (idempotent).
        from app.catalog.store import seed_catalog
        from app.database.session import SessionLocal

        try:
            with SessionLocal() as session:
                seed_catalog(session)
        except Exception:  # e.g. migrations not applied yet -- the app still serves
            logging.getLogger(__name__).warning("Could not seed the search-term catalog", exc_info=True)

    fastapi_app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    fastapi_app.include_router(api_router)
    fastapi_app.include_router(ui_router)
    fastapi_app.include_router(settings_router)

    return fastapi_app


app = create_app()
