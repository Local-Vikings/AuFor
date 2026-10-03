"""HTTP routes for the SolarSight API.

Routes validate and serialize requests only; the calculation lives in app.pipeline.
Battery simulation and recommendations are not connected yet (T16, T17, T22).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.config import DEFAULT_PERFORMANCE_RATIO, USE_MOCK_WEATHER
from app.models import (
    DailyForecast,
    ForecastMeta,
    ForecastRequest,
    ForecastResponse,
    HourlyForecast,
)
from app.pipeline import build_forecast
from app.weather import WeatherError

router = APIRouter(prefix="/api")


@router.post("/forecast", response_model=ForecastResponse)
def forecast(request: ForecastRequest) -> ForecastResponse:
    """Return hourly and daily solar production for the requested system."""
    try:
        result = build_forecast(request)
    except WeatherError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error

    soc = request.battery.initial_soc_kwh  # placeholder until battery.py (T16) lands
    hourly = [
        HourlyForecast(
            time=timestamp.to_pydatetime(),
            ghi=row.ghi,
            g_poa=row.g_poa,
            t_cell=row.t_cell,
            p_ac_w=row.p_ac_w,
            load_w=row.load_w,
            soc_kwh=soc,
            grid_import_w=max(row.load_w - row.p_ac_w, 0.0),
            grid_export_w=max(row.p_ac_w - row.load_w, 0.0),
        )
        for timestamp, row in result.hourly.iterrows()
    ]
    daily = [
        DailyForecast(date=day, kwh=row.kwh, self_consumption_pct=row.self_consumption_pct)
        for day, row in result.daily.iterrows()
    ]
    weather_label = "mock weather" if USE_MOCK_WEATHER else "open-meteo"
    return ForecastResponse(
        hourly=hourly,
        daily=daily,
        recommendations=[],
        explanation=None,
        meta=ForecastMeta(
            pr_used=DEFAULT_PERFORMANCE_RATIO,
            calibrated=False,
            data_sources=[weather_label, f"{result.engine} physics"],
            engine=result.engine,
        ),
    )
