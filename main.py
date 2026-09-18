from __future__ import annotations

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

    fastapi_app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    fastapi_app.include_router(api_router)
    fastapi_app.include_router(ui_router)

    return fastapi_app


app = create_app()
