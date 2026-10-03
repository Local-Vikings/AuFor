"""Forecast pipeline: weather -> hourly solar power -> daily kWh (bible 7, 9).

Pure orchestration over app.weather and app.solar. No HTTP, no disk, no battery
or recommendation logic (those are separate modules).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from app import battery, solar
from app.config import (
    CALIBRATION_CLIP_MARGIN,
    DEFAULT_ALBEDO,
    DEFAULT_PERFORMANCE_RATIO,
    FORECAST_DT_H,
    LOAD_PROFILE_KWH,
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
    monthly: pd.DataFrame  # index: "YYYY-MM"; columns days, kwh, self_consumption_pct
    engine: str
    sources: list[str]  # weather sources, e.g. ["open-meteo", "climatology (...)"]


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
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Return (g_poa, t_cell, p_ac_w, p_dc_w) for one panel group with its own inverter."""
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
    return poa, t_cell, solar.ac_power(p_dc, pr, group.inverter_max_w), p_dc


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
    p_dc = np.sum([result[3] for result in results], axis=0)
    clipped = np.any(
        [r[3] * pr >= g.inverter_max_w * CALIBRATION_CLIP_MARGIN for r, g in zip(results, groups)], axis=0
    )
    g_poa = np.average([result[0] for result in results], axis=0, weights=weights)
    t_cell = np.average([result[1] for result in results], axis=0, weights=weights)

    ghi_clear, p_ac_clear = _clear_sky(groups, weather, zenith, azimuth, t_utc, pr)
    # Real irradiance can beat the clear-sky model for a moment; never report a negative cloud loss.
    ghi_clear = np.maximum(ghi_clear, weather["ghi"].to_numpy())
    p_ac_clear = np.maximum(p_ac_clear, p_ac)

    shape = np.array(LOAD_PROFILE_KWH)
    load_w = request.load.daily_kwh * 1000.0 * (shape / shape.sum())[weather.index.hour]
    cfg = request.battery
    sim = battery.simulate(
        p_ac / 1000.0, load_w / 1000.0, cfg.capacity_kwh, cfg.dod, cfg.eta_c, cfg.eta_d,
        cfg.max_power_kw, cfg.initial_soc_kwh, FORECAST_DT_H,
    )
    hourly = pd.DataFrame(
        {
            "ghi": weather["ghi"].to_numpy(),
            "g_poa": g_poa,
            "t_cell": t_cell,
            "p_ac_w": p_ac,
            "p_dc_w": p_dc,  # DC power before the performance ratio; calibration compares readings with it
            "clipped": clipped,  # an inverter is at its limit, so the output says nothing about PR
            "load_w": load_w,
            "soc_kwh": sim.soc_kwh,
            "grid_import_w": sim.import_kw * 1000.0,
            "grid_export_w": sim.export_kw * 1000.0,
            "cloud_cover": weather["cloud_cover"].to_numpy(),
            "temp_air": weather["temp_air"].to_numpy(),
            "wind_ms": weather["wind"].to_numpy(),
            "ghi_clear": ghi_clear,
            "p_ac_clear_w": p_ac_clear,
        },
        index=weather.index,
    )
    daily = _daily_totals(hourly)
    return PipelineResult(
        hourly=hourly,
        daily=daily,
        monthly=_monthly_totals(daily),
        engine=solar.ENGINE,
        sources=list(weather.attrs.get("sources", ["open-meteo"])),
    )


def _clear_sky(
    groups: list[PanelConfig],
    weather: pd.DataFrame,
    zenith: np.ndarray,
    azimuth: np.ndarray,
    t_utc: np.ndarray,
    pr: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Clear-sky GHI and the AC power the same system would give under it (bible 9.7)."""
    ghi, dni, dhi = solar.irradiance_from_clouds(zenith, np.zeros(len(zenith)), t_utc)
    clear_weather = weather.assign(ghi=ghi, dni=dni, dhi=dhi)
    power = [_group_power(g, clear_weather, zenith, azimuth, pr)[2] for g in groups]
    return ghi, np.sum(power, axis=0)


def _cloud_loss_pct(produced_kwh: float, clear_kwh: float) -> float:
    """Share of the clear-sky energy that clouds took away, in percent."""
    return 100.0 * (1.0 - produced_kwh / clear_kwh) if clear_kwh > 0 else 0.0


def _daily_totals(hourly: pd.DataFrame) -> pd.DataFrame:
    """Daily kWh, the share of solar energy kept on site (used or stored) and cloud losses."""
    rows = {}
    for day, chunk in hourly.groupby(hourly.index.date):
        produced = solar.energy_kwh(chunk["p_ac_w"].to_numpy(), FORECAST_DT_H)
        clear = solar.energy_kwh(chunk["p_ac_clear_w"].to_numpy(), FORECAST_DT_H)
        exported = solar.energy_kwh(chunk["grid_export_w"].to_numpy(), FORECAST_DT_H)
        daylight = chunk[chunk["ghi_clear"] > 0]
        rows[day.isoformat()] = {
            "kwh": produced,
            "self_consumption_pct": 100.0 * (1.0 - exported / produced) if produced > 0 else 0.0,
            "avg_cloud_cover": float(daylight["cloud_cover"].mean()) if len(daylight) else 0.0,
            "clear_sky_kwh": clear,
            "cloud_loss_pct": _cloud_loss_pct(produced, clear),
        }
    return pd.DataFrame.from_dict(rows, orient="index")


def _monthly_totals(daily: pd.DataFrame) -> pd.DataFrame:
    """Sum daily kWh per calendar month; self-consumption is weighted by daily kWh."""
    rows = {}
    for month, chunk in daily.groupby(daily.index.str[:7]):
        kwh = float(chunk["kwh"].sum())
        weighted = float((chunk["kwh"] * chunk["self_consumption_pct"]).sum())
        clear = float(chunk["clear_sky_kwh"].sum())
        cloud = float((chunk["avg_cloud_cover"] * chunk["clear_sky_kwh"]).sum())
        rows[month] = {
            "days": len(chunk),
            "kwh": kwh,
            "self_consumption_pct": weighted / kwh if kwh > 0 else 0.0,
            "avg_cloud_cover": cloud / clear if clear > 0 else 0.0,
            "clear_sky_kwh": clear,
            "cloud_loss_pct": _cloud_loss_pct(kwh, clear),
        }
    return pd.DataFrame.from_dict(rows, orient="index")
