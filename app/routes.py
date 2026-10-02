"""HTTP routes for the SolarSight API.

Routes validate and serialize requests only. Weather, physics, battery, and
recommendation logic will be connected here after their domain tasks are complete.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter

from app.config import DEFAULT_PERFORMANCE_RATIO
from app.models import (
    DailyForecast,
    ForecastMeta,
    ForecastRequest,
    ForecastResponse,
    HourlyForecast,
    Recommendation,
)

router = APIRouter(prefix="/api")


@router.post("/forecast", response_model=ForecastResponse)
def forecast_stub(request: ForecastRequest) -> ForecastResponse:
    """Return a deterministic simulated payload until the forecast pipeline exists."""
    timestamp = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    hourly = [
        HourlyForecast(
            time=timestamp,
            ghi=0,
            g_poa=0,
            t_cell=10,
            p_ac_w=0,
            load_w=request.load.daily_kwh * 1000 / 24,
            soc_kwh=request.battery.initial_soc_kwh,
            grid_import_w=request.load.daily_kwh * 1000 / 24,
            grid_export_w=0,
        )
    ]
    daily = [DailyForecast(date=timestamp.date().isoformat(), kwh=0, self_consumption_pct=0)]
    recommendations = [
        Recommendation(
            subtopic="use",
            hour=timestamp,
            title="Forecast stub",
            reason="Simulated placeholder data; connect the weather and physics pipeline next.",
            kwh_effect=0,
        )
    ]
    return ForecastResponse(
        hourly=hourly,
        daily=daily,
        recommendations=recommendations,
        explanation=None,
        meta=ForecastMeta(
            pr_used=DEFAULT_PERFORMANCE_RATIO,
            calibrated=False,
            data_sources=["simulated stub"],
        ),
    )
