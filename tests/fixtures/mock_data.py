"""Mock builders from bible 19: weather scenarios, panel and battery configs, load profile."""

from __future__ import annotations

import numpy as np
import pandas as pd

from app.config import LOAD_PROFILE_KWH
from app.models import ForecastRequest
from app.weather import make_mock_weather as clear_day

SCENARIOS = ("clear", "overcast", "partly_cloudy", "cloudy_tomorrow", "night_only", "missing_irradiance")
LOAD_PROFILE = LOAD_PROFILE_KWH
SOFIA = (42.6977, 23.3219)


def _overcast(frame: pd.DataFrame) -> pd.DataFrame:
    dim = frame.copy()
    dim["ghi"] = frame["ghi"] * 0.30
    dim["dni"] = frame["dni"] * 0.05
    dim["dhi"] = 0.9 * dim["ghi"]
    dim["cloud_cover"] = 95.0
    return dim


def make_mock_weather(scenario: str, days: int) -> pd.DataFrame:
    """Build one of the six bible 19.2 scenarios from the clear day."""
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown scenario: {scenario}")
    frame = clear_day(days)
    day_of = np.arange(len(frame)) // 24
    hour_of = frame.index.hour
    if scenario == "overcast":
        frame = _overcast(frame)
    elif scenario == "partly_cloudy":
        dim = _overcast(frame)
        rows = np.flatnonzero((hour_of >= 12) & (hour_of <= 14))
        frame.iloc[rows] = dim.iloc[rows]
    elif scenario == "cloudy_tomorrow":
        rows = np.flatnonzero(day_of == 1)
        frame.iloc[rows] = _overcast(frame).iloc[rows]
    elif scenario == "night_only":
        frame[["ghi", "dni", "dhi"]] = 0.0
    elif scenario == "missing_irradiance":
        frame[["ghi", "dni", "dhi"]] = np.nan
    return frame


def _panel(count: int, tilt: float, azimuth: float) -> dict:
    return {"count": count, "watt_peak": 400, "tilt": tilt, "azimuth": azimuth,
            "noct": 45, "gamma": -0.004, "inverter_max_w": 4000}


def _battery(capacity: float, power: float = 5.0, initial: float | None = None) -> dict:
    return {"capacity_kwh": capacity, "dod": 0.9, "eta_c": 0.95, "eta_d": 0.95,
            "max_power_kw": power, "initial_soc_kwh": capacity / 2 if initial is None else initial}


CONFIGS = {
    "default": (_panel(10, 35, 180), _battery(10)),
    "small": (_panel(3, 30, 180), _battery(0, 0.0, 0.0)),
    "east_roof": (_panel(8, 20, 90), _battery(5)),
    "flat": (_panel(10, 0, 180), _battery(10)),
    "north": (_panel(10, 35, 0), _battery(10)),
}


def make_request(config: str = "default", days: int = 3, **battery_overrides) -> ForecastRequest:
    panel, battery = CONFIGS[config]
    return ForecastRequest.model_validate({
        "lat": SOFIA[0], "lon": SOFIA[1], "panel": panel,
        "battery": {**battery, **battery_overrides}, "load": {"daily_kwh": 12}, "days": days,
    })
