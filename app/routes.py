"""HTTP routes for the SolarSight API.

Routes validate and serialize requests only; the calculation lives in app.pipeline.
Battery simulation and recommendations are not connected yet (T16, T17, T22).
"""

from __future__ import annotations

import pandas as pd
from fastapi import APIRouter, HTTPException

from app.config import DEFAULT_PERFORMANCE_RATIO
from app.models import (
    DailyForecast,
    ForecastMeta,
    ForecastRequest,
    ForecastResponse,
    HourlyForecast,
    MonthlyForecast,
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

    hourly: list[HourlyForecast] = []
    daily: list[DailyForecast] = []
    monthly: list[MonthlyForecast] = []
    if request.resolution == "hourly":
        hourly = _hourly_rows(result.hourly, request.battery.initial_soc_kwh)
    if request.resolution in ("hourly", "daily"):
        daily = [
            DailyForecast(date=day, kwh=row.kwh, self_consumption_pct=row.self_consumption_pct)
            for day, row in result.daily.iterrows()
        ]
    if request.resolution == "monthly":
        monthly = [
            MonthlyForecast(
                month=month,
                days=int(row.days),
                kwh=row.kwh,
                self_consumption_pct=row.self_consumption_pct,
            )
            for month, row in result.monthly.iterrows()
        ]
    return ForecastResponse(
        hourly=hourly,
        daily=daily,
        monthly=monthly,
        recommendations=[],
        explanation=None,
        meta=ForecastMeta(
            pr_used=DEFAULT_PERFORMANCE_RATIO,
            calibrated=False,
            data_sources=[*result.sources, f"{result.engine} physics"],
            engine=result.engine,
        ),
    )


def _hourly_rows(hourly: pd.DataFrame, soc_kwh: float) -> list[HourlyForecast]:
    """Convert the hourly table; SoC is a placeholder until battery.py (T16) lands."""
    return [
        HourlyForecast(
            time=timestamp.to_pydatetime(),
            ghi=row.ghi,
            g_poa=row.g_poa,
            t_cell=row.t_cell,
            p_ac_w=row.p_ac_w,
            load_w=row.load_w,
            soc_kwh=soc_kwh,
            grid_import_w=max(row.load_w - row.p_ac_w, 0.0),
            grid_export_w=max(row.p_ac_w - row.load_w, 0.0),
        )
        for timestamp, row in hourly.iterrows()
    ]
