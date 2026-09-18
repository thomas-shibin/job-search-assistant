from __future__ import annotations

from fastapi import FastAPI


def create_app() -> FastAPI:
    fastapi_app = FastAPI(title="Local AI Job Application Assistant")

    @fastapi_app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    return fastapi_app


app = create_app()
