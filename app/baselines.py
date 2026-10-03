"""Baselines and error metrics for validating the forecast (bible 8, T42).

* Clear-sky bound: the configured system's AC power under a pvlib Ineichen clear sky. The forecast
  must stay at or below it (with 5% tolerance).
* Persistence baseline: "tomorrow = today", the same hour 24 h earlier.
* Metrics: RMSE, MAE and skill = 1 - RMSE_model / RMSE_persistence, from solar.metrics (C++ with a
  Python fallback).
* Measured data: real power readings aligned to forecast hours. Simulated readings are never
  treated as measurements, so no accuracy is claimed from them.

No network and no disk access here; scripts/validate.py does the fetching and the printing.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pvlib

from app import pipeline, solar
from app.calibration import hour_label
from app.config import DEFAULT_PERFORMANCE_RATIO, REAL_READING_SOURCES, WEATHER_INTERVAL_SHIFT_S
from app.models import ForecastRequest

CLEAR_SKY_TOLERANCE = 1.05  # the forecast may exceed the clear-sky bound by at most 5%
CLEAR_SKY_SLACK_FRACTION = 0.02  # plus 2% of the bound's peak: at sunrise and sunset an hourly average straddles the horizon
# Linke turbidity of the bound. pvlib's lookup says about 3.1 for Sofia, which is too dim for an UPPER
# bound: real Open-Meteo forecasts beat it by up to 27%. 2.0 is the clearest realistic air, so the bound
# really is an upper bound (on 16 real forecast days nothing beats it).
CLEAR_SKY_LINKE_TURBIDITY = 2.0
PERSISTENCE_LAG_H = 24
MIN_ACCURACY_POINTS = 24  # fewer aligned hours than this and no accuracy is reported
_EPOCH = pd.Timestamp("1970-01-01", tz="UTC")


# ---- clear-sky bound ----


def clear_sky_weather(
    request: ForecastRequest, weather: pd.DataFrame, linke_turbidity: float = CLEAR_SKY_LINKE_TURBIDITY
) -> pd.DataFrame:
    """The weather table with Ineichen clear-sky irradiance in place of the forecast's irradiance."""
    instants = (weather.index - pd.Timedelta(seconds=WEATHER_INTERVAL_SHIFT_S)).tz_convert("UTC")
    location = pvlib.location.Location(request.lat, request.lon)
    clear = location.get_clearsky(instants, model="ineichen", linke_turbidity=linke_turbidity)
    return weather.assign(ghi=clear["ghi"].to_numpy(), dni=clear["dni"].to_numpy(), dhi=clear["dhi"].to_numpy())


def clear_sky(request: ForecastRequest, weather: pd.DataFrame, pr: float = DEFAULT_PERFORMANCE_RATIO) -> np.ndarray:
    """AC power (W) of the configured system under a clear sky, hour by hour: the production upper bound."""
    t_utc = pipeline._epoch_seconds(weather.index)
    zenith, azimuth = solar.sun_position(t_utc - WEATHER_INTERVAL_SHIFT_S, request.lat, request.lon)
    clear = clear_sky_weather(request, weather)
    groups = [pipeline._group_power(g, clear, zenith, azimuth, pr)[2] for g in request.panel_groups]
    return np.sum(groups, axis=0)


def exceedances(values, bound, tolerance: float = CLEAR_SKY_TOLERANCE) -> list[int]:
    """Positions where ``values`` is above ``bound * tolerance`` plus a small slack (2% of the bound's peak)."""
    values, bound = np.asarray(values, dtype=float), np.asarray(bound, dtype=float)
    slack = CLEAR_SKY_SLACK_FRACTION * float(bound.max()) if len(bound) else 0.0
    return [int(i) for i in np.flatnonzero(values > bound * tolerance + slack + 1e-6)]


def over(vals, clear, tolerance: float = CLEAR_SKY_TOLERANCE) -> int:
    """How many hours the forecast is above the clear-sky bound (0 is the goal)."""
    return len(exceedances(vals, clear, tolerance))


# ---- persistence and metrics ----


def persistence(values, lag: int = PERSISTENCE_LAG_H) -> np.ndarray:
    """The "tomorrow = today" forecast: each value is predicted by the one ``lag`` samples earlier."""
    return np.asarray(values, dtype=float)[:-lag]


def evaluate(pred, obs, lag: int = PERSISTENCE_LAG_H) -> tuple[float, float, float]:
    """(RMSE, MAE, skill) of ``pred`` against ``obs``, with persistence as the reference.

    Both series cover the same hours; the first ``lag`` hours have no persistence forecast and are left out.

    Raises:
        ValueError: If the series differ in length or are not longer than ``lag``.
    """
    pred, obs = np.asarray(list(pred), dtype=float), np.asarray(list(obs), dtype=float)
    if len(pred) != len(obs) or len(obs) <= lag:
        raise ValueError(f"need equally long series with more than {lag} samples")
    return solar.metrics(pred[lag:], obs[lag:], persistence(obs, lag))


# ---- measured data ----


def measured_hourly(hourly: pd.DataFrame, rows: list[dict], real_only: bool = True) -> pd.Series:
    """Mean measured power (W) per forecast hour, indexed like ``hourly``; hours without readings are NaN.

    A reading belongs to the row whose label is the end of its hour (bible 9.9). Only power readings from
    real sources (battery, panel) are used unless ``real_only`` is False, because simulated readings
    measure nothing.
    """
    wanted = [r for r in rows if r["type"] == "power_w" and (not real_only or r["source"] in REAL_READING_SOURCES)]
    labels = pd.Series(
        ((hourly.index.tz_convert("UTC") - _EPOCH).total_seconds()).to_numpy().astype(int), index=hourly.index
    )
    by_label: dict[int, list[float]] = {}
    for row in wanted:
        by_label.setdefault(hour_label(int(row["timestamp"].timestamp())), []).append(row["value"])
    return labels.map(lambda label: float(np.mean(by_label[label])) if label in by_label else np.nan)


def accuracy(pred: pd.Series, obs: pd.Series, lag: int = PERSISTENCE_LAG_H) -> dict | None:
    """Forecast accuracy against measurements, or None when there is not enough measured data.

    Uses the hours with a forecast, a measurement and a measurement ``lag`` hours earlier (so
    persistence can be computed), and needs at least MIN_ACCURACY_POINTS of them.
    """
    earlier = obs.reindex(obs.index - pd.Timedelta(hours=lag)).set_axis(obs.index)  # the same hour, lag hours before
    frame = pd.DataFrame({"pred": pred, "obs": obs, "ref": earlier}).dropna()
    if len(frame) < MIN_ACCURACY_POINTS:
        return None
    rmse, mae, skill = solar.metrics(frame["pred"].to_numpy(), frame["obs"].to_numpy(), frame["ref"].to_numpy())
    ref_rmse = float(np.sqrt(np.mean((frame["ref"] - frame["obs"]) ** 2)))
    return {"points": len(frame), "rmse_w": rmse, "mae_w": mae, "skill": skill, "persistence_rmse_w": ref_rmse}
