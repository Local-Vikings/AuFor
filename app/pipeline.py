"""Forecast pipeline: weather -> hourly solar power -> daily kWh (bible 7, 9).

Pure orchestration over app.weather and app.solar. No HTTP, no disk, no battery
or recommendation logic (those are separate modules).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from app import solar
from app.config import (
    DEFAULT_ALBEDO,
    DEFAULT_PERFORMANCE_RATIO,
    FORECAST_DT_H,
    WEATHER_INTERVAL_SHIFT_S,
)
from app.models import ForecastRequest, PanelConfig
from app.weather import fetch_weather

_EPOCH = pd.Timestamp("1970-01-01", tz="UTC")


@dataclass(frozen=True)
class PipelineResult:
    """Hourly and daily forecast tables plus the engine that produced them."""

    hourly: pd.DataFrame  # index: tz-aware hour; columns ghi, g_poa, t_cell, p_ac_w, load_w
    daily: pd.DataFrame  # index: local date string; columns kwh, self_consumption_pct
    engine: str


def _epoch_seconds(index: pd.DatetimeIndex) -> np.ndarray:
    return (index.tz_convert("UTC") - _EPOCH).total_seconds().to_numpy()


def _fill_missing_irradiance(
    weather: pd.DataFrame, zenith: np.ndarray, t_utc: np.ndarray
) -> pd.DataFrame:
    """Replace rows with missing irradiance using the cloud-cover fallback (bible 9.7)."""
    missing = weather[["ghi", "dni", "dhi"]].isna().any(axis=1).to_numpy()
    if not missing.any():
        return weather
    cloud = weather["cloud_cover"].fillna(0.0).to_numpy()
    ghi, dni, dhi = solar.irradiance_from_clouds(zenith, cloud, t_utc)
    filled = weather.copy()
    for column, values in (("ghi", ghi), ("dni", dni), ("dhi", dhi)):
        filled.loc[missing, column] = values[missing]
    return filled


def _group_power(
    group: PanelConfig, weather: pd.DataFrame, zenith: np.ndarray, azimuth: np.ndarray, pr: float
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return (g_poa, t_cell, p_ac_w) for one panel group with its own inverter."""
    poa = solar.poa_irradiance(
        zenith,
        azimuth,
        weather["ghi"].to_numpy(),
        weather["dni"].to_numpy(),
        weather["dhi"].to_numpy(),
        group.tilt,
        group.azimuth,
        DEFAULT_ALBEDO,
    )
    t_cell = solar.cell_temperature(weather["temp_air"].to_numpy(), poa, group.noct)
    p_dc = solar.dc_power(poa, t_cell, group.count * group.watt_peak, group.gamma)
    return poa, t_cell, solar.ac_power(p_dc, pr, group.inverter_max_w)


def build_forecast(
    request: ForecastRequest,
    weather: pd.DataFrame | None = None,
    pr: float = DEFAULT_PERFORMANCE_RATIO,
) -> PipelineResult:
    """Compute hourly AC power and daily kWh for a request.

    Args:
        request: Validated forecast request (azimuth 180 = south).
        weather: Optional hourly weather; fetched from the configured provider if omitted.
        pr: Performance ratio (bible 9.5).

    Returns:
        PipelineResult with hourly and daily tables.

    Raises:
        app.weather.WeatherError: If weather cannot be fetched.
    """
    if weather is None:
        weather = fetch_weather(request.lat, request.lon, request.days)

    t_utc = _epoch_seconds(weather.index)
    zenith, azimuth = solar.sun_position(
        t_utc - WEATHER_INTERVAL_SHIFT_S, request.lat, request.lon
    )
    weather = _fill_missing_irradiance(weather, zenith, t_utc)

    groups = request.panel_groups
    results = [_group_power(group, weather, zenith, azimuth, pr) for group in groups]
    weights = np.array([group.count * group.watt_peak for group in groups])
    p_ac = np.sum([result[2] for result in results], axis=0)
    g_poa = np.average([result[0] for result in results], axis=0, weights=weights)
    t_cell = np.average([result[1] for result in results], axis=0, weights=weights)

    load_w = request.load.daily_kwh * 1000.0 / 24.0
    hourly = pd.DataFrame(
        {
            "ghi": weather["ghi"].to_numpy(),
            "g_poa": g_poa,
            "t_cell": t_cell,
            "p_ac_w": p_ac,
            "load_w": np.full(len(weather), load_w),
        },
        index=weather.index,
    )
    return PipelineResult(hourly=hourly, daily=_daily_totals(hourly), engine=solar.ENGINE)


def _daily_totals(hourly: pd.DataFrame) -> pd.DataFrame:
    """Daily kWh and the share of production used directly by the load (no battery)."""
    rows = {}
    for day, chunk in hourly.groupby(hourly.index.date):
        produced = solar.energy_kwh(chunk["p_ac_w"].to_numpy(), FORECAST_DT_H)
        direct = solar.energy_kwh(
            np.minimum(chunk["p_ac_w"], chunk["load_w"]).to_numpy(), FORECAST_DT_H
        )
        share = 100.0 * direct / produced if produced > 0 else 0.0
        rows[day.isoformat()] = {"kwh": produced, "self_consumption_pct": share}
    return pd.DataFrame.from_dict(rows, orient="index")
