"""HTTP routes for the SolarSight API.

Routes validate and serialize requests only; the calculation lives in app.pipeline.
Recommendations come from app.advisor.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Literal

import pandas as pd
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from app import calibration, llm, readings, vision
from app import config as settings
from app.advisor import recommend
from app.clouds import fetch_cloud_field, site_bounds
from app.config import (
    CLOUD_FIELD_MAX_HOURS,
    CLOUD_FIELD_MAX_LAT_SPAN_DEG,
    CLOUD_FIELD_MAX_LON_SPAN_DEG,
    CLOUD_MAX_IMAGE_BYTES,
    CLOUD_ROI_RADIUS_PX,
    READINGS_MAX_HOURS,
)
from app.models import (
    DailyForecast,
    ForecastMeta,
    ForecastRequest,
    ForecastResponse,
    HourlyForecast,
    CameraResult,
    MonthlyForecast,
    ReadingCreate,
    ReadingOut,
    ReadingSource,
    ReadingsSummary,
    ReadingType,
    Recommendation,
)
from app.pipeline import PipelineResult, build_forecast
from app.weather import WeatherError, fetch_weather

router = APIRouter(prefix="/api")


@router.post("/forecast", response_model=ForecastResponse)
def forecast(request: ForecastRequest) -> ForecastResponse:
    """Return hourly and daily solar production for the requested system."""
    try:
        weather = fetch_weather(request.lat, request.lon, request.days)
    except WeatherError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error

    key = calibration.system_key(request)
    before = calibration.load(key)
    result = build_forecast(request, weather, pr=before.pr)
    state = calibration.update(key, result.hourly)  # one smoothing step if new readings arrived
    if state.pr != before.pr:
        result = build_forecast(request, weather, pr=state.pr)

    hourly: list[HourlyForecast] = []
    daily: list[DailyForecast] = []
    monthly: list[MonthlyForecast] = []
    if request.resolution == "hourly":
        hourly = _hourly_rows(result.hourly)
    if request.resolution in ("hourly", "daily"):
        daily = [
            DailyForecast(
                date=day,
                kwh=row.kwh,
                self_consumption_pct=row.self_consumption_pct,
                avg_cloud_cover=row.avg_cloud_cover,
                clear_sky_kwh=row.clear_sky_kwh,
                cloud_loss_pct=_clamp_pct(row.cloud_loss_pct),
            )
            for day, row in result.daily.iterrows()
        ]
    if request.resolution == "monthly":
        monthly = [
            MonthlyForecast(
                month=month,
                days=int(row.days),
                kwh=row.kwh,
                self_consumption_pct=row.self_consumption_pct,
                avg_cloud_cover=row.avg_cloud_cover,
                clear_sky_kwh=row.clear_sky_kwh,
                cloud_loss_pct=_clamp_pct(row.cloud_loss_pct),
            )
            for month, row in result.monthly.iterrows()
        ]
    response = ForecastResponse(
        hourly=hourly,
        daily=daily,
        monthly=monthly,
        recommendations=_recommendations(request, result, state.pr),
        explanation=None,
        meta=ForecastMeta(
            pr_used=state.pr,
            calibrated=state.calibrated,
            calibration_points=state.points,
            calibration_source=state.source,
            data_sources=[*result.sources, f"{result.engine} physics"],
            engine=result.engine,
        ),
    )
    summary = llm.build_summary(request, response)
    return response.model_copy(
        update={
            "explanation": llm.template_explanation(summary),
            "explanation_source": "template",
            "explain_id": llm.remember(summary) if llm.available() else None,
        }
    )


def _hourly_rows(hourly: pd.DataFrame) -> list[HourlyForecast]:
    """Convert the hourly table into response rows."""
    return [
        HourlyForecast(
            time=timestamp.to_pydatetime(),
            ghi=row.ghi,
            g_poa=row.g_poa,
            t_cell=row.t_cell,
            p_ac_w=row.p_ac_w,
            load_w=row.load_w,
            soc_kwh=row.soc_kwh,
            grid_import_w=row.grid_import_w,
            grid_export_w=row.grid_export_w,
            cloud_cover=row.cloud_cover,
            temp_air=row.temp_air,
            wind_ms=row.wind_ms,
            ghi_clear=row.ghi_clear,
            p_ac_clear_w=row.p_ac_clear_w,
        )
        for timestamp, row in hourly.iterrows()
    ]


def _recommendations(request: ForecastRequest, result: PipelineResult, pr: float) -> list[Recommendation]:
    """Rule-based recommendations; mock weather is judged from its first hour, not today's clock."""
    first_hour = result.hourly.index[0]
    now = first_hour if "mock weather" in result.sources else max(pd.Timestamp.now(tz=first_hour.tz), first_hour)
    return recommend(request, result.hourly, result.daily, now.to_pydatetime(), pr)


def _clamp_pct(value: float) -> float:
    """Keep a percentage inside 0-100 against floating-point noise."""
    return min(max(value, 0.0), 100.0)


@router.get("/cloud-field")
def cloud_field(
    south: float | None = Query(default=None, ge=-90, le=90),
    west: float | None = Query(default=None, ge=-180, le=180),
    north: float | None = Query(default=None, ge=-90, le=90),
    east: float | None = Query(default=None, ge=-180, le=180),
    lat: float | None = Query(default=None, ge=-90, le=90),
    lon: float | None = Query(default=None, ge=-180, le=180),
    hours: int = Query(default=72, ge=1, le=CLOUD_FIELD_MAX_HOURS),
) -> dict:
    """Hourly cloud cover and wind over a map view (south/west/north/east) or around a site (lat/lon)."""
    bounds = (south, west, north, east)
    if None in bounds:
        if lat is None or lon is None:
            raise HTTPException(status_code=422, detail="give south, west, north and east, or lat and lon")
        bounds = site_bounds(lat, lon)
    south, west, north, east = bounds
    if south >= north or west >= east:
        raise HTTPException(status_code=422, detail="bounds must have south < north and west < east")
    if north - south > CLOUD_FIELD_MAX_LAT_SPAN_DEG or east - west > CLOUD_FIELD_MAX_LON_SPAN_DEG:
        raise HTTPException(status_code=422, detail="map view is too large for a cloud forecast; zoom in")
    try:
        return fetch_cloud_field(south, west, north, east, hours).to_dict()
    except WeatherError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error




def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """Writes need the shared X-API-Key when READINGS_API_KEY is set; with no key set they stay open (development)."""
    wanted = settings.READINGS_API_KEY
    if wanted and not (x_api_key and secrets.compare_digest(x_api_key.encode(), wanted.encode())):
        raise HTTPException(status_code=401, detail="missing or wrong X-API-Key header")


@router.post("/readings", response_model=ReadingOut, status_code=201, dependencies=[Depends(require_api_key)])
def post_reading(reading: ReadingCreate) -> dict:
    """Store one measurement from a sensor, the camera or a simulation (source says which)."""
    try:
        return readings.add_reading(reading)
    except readings.FutureReading as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except readings.DuplicateReading as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get("/readings/summary", response_model=ReadingsSummary)
def readings_summary() -> dict:
    """How many real and simulated readings are stored."""
    return readings.summary()


@router.get("/readings", response_model=list[ReadingOut])
def get_readings(
    type: ReadingType | None = None,
    source: ReadingSource | None = None,
    hours: int = Query(default=24, ge=1, le=READINGS_MAX_HOURS),
) -> list[dict]:
    """Readings of the last ``hours`` hours, oldest first. Simulated rows keep source "simulated"."""
    return readings.recent(type, hours, source)


class ExplainRequest(BaseModel):
    """Ask for the AI text of a forecast the server just made."""

    explain_id: str


class ExplainResponse(BaseModel):
    """The explanation text and whether the LLM or the template wrote it."""

    explanation: str
    explanation_source: Literal["llm", "template"]


@router.post("/explain", response_model=ExplainResponse)
def explain(request: ExplainRequest) -> ExplainResponse:
    """AI explanation of a recent forecast; falls back to the template text, never fails on the LLM."""
    summary = llm.recall(request.explain_id)
    if summary is None:
        raise HTTPException(status_code=404, detail="unknown or expired forecast; run the forecast again")
    result = llm.explain(summary)
    return ExplainResponse(explanation=result.text, explanation_source=result.source)


@router.post("/camera/analyze", response_model=CameraResult, status_code=201, dependencies=[Depends(require_api_key)])
async def analyze_sky_photo(request: Request, radius: int = Query(default=CLOUD_ROI_RADIUS_PX, ge=20, le=2000)) -> CameraResult:
    """Measure the cloud fraction of a sky photo (raw image bytes in the body) and store it as a camera reading.

    Only works where the PyTorch model is installed. Otherwise answer 501: run the model on the camera
    machine instead (scripts/pi_cloud_agent.py) and POST the number to /api/readings.
    """
    image = await request.body()
    if not image:
        raise HTTPException(status_code=422, detail="send the photo as the raw request body")
    if len(image) > CLOUD_MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="photo is too large")
    try:
        free = await run_in_threadpool(vision.measure_free_percent, image, radius)
    except vision.ModelUnavailable as error:
        raise HTTPException(
            status_code=501,
            detail=f"{error}. Run the model on the camera machine and POST cloud_fraction to /api/readings.",
        ) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    fraction = round(min(max(1.0 - free / 100.0, 0.0), 1.0), 3)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    try:
        readings.add_reading(ReadingCreate(source="camera", type="cloud_fraction", value=fraction, timestamp=now))
    except readings.DuplicateReading as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return CameraResult(free_percent=round(free, 2), cloud_fraction=fraction, radius_px=radius, timestamp=now)


@router.get("/calibration")
def calibration_states() -> list[dict]:
    """The performance ratio learned for each configured system."""
    return calibration.all_states()


@router.post("/calibration/reset", dependencies=[Depends(require_api_key)])
def reset_calibration() -> dict:
    """Forget every learned performance ratio (for repeatable demos). Readings are kept."""
    return {"removed": calibration.reset()}
