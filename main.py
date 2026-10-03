"""FastAPI entry point for SolarSight.

This module owns application assembly and basic HTTP routes. Forecast calculations
belong in the domain modules described by bible.md.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import CARTO_API_KEY
from app.routes import router as api_router

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="SolarSight")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")
app.include_router(api_router)


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    """Render the SolarSight dashboard shell."""
    return templates.TemplateResponse(
        "index.html", {"request": request, "carto_api_key": CARTO_API_KEY}
    )


@app.get("/api/health")
def health() -> dict[str, str]:
    """Return the service health status."""
    return {"status": "ok"}
