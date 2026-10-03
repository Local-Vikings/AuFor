"""Fetch and parse hourly weather data for SolarSight.

This module owns Open-Meteo access and the offline mock-weather switch. It does
not calculate solar power, simulate batteries, or translate errors into HTTP responses.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
import pandas as pd

from app.config import USE_MOCK_WEATHER, WEATHER_TIMEOUT_SECONDS

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
HOURLY_FIELDS = (
    "temperature_2m",
    "shortwave_radiation",
    "direct_normal_irradiance",
    "diffuse_radiation",
    "cloud_cover",
    "wind_speed_10m",
)
REQUIRED_COLUMNS = ("ghi", "dni", "dhi", "temp_air", "cloud_cover", "wind")
MOCK_TIMEZONE = "Europe/Sofia"
MOCK_START_DATE = date(2026, 10, 3)
MOCK_DAY = {
    7: (30, 90, 25, 9, 1.5),
    8: (140, 430, 55, 11, 1.5),
    9: (290, 640, 70, 13, 2.0),
    10: (430, 760, 80, 15, 2.0),
    11: (540, 810, 85, 17, 2.5),
    12: (610, 840, 88, 18, 2.5),
    13: (640, 850, 90, 19, 2.5),
    14: (610, 840, 88, 19, 2.5),
    15: (520, 800, 85, 18, 2.0),
    16: (390, 730, 78, 17, 2.0),
    17: (240, 600, 65, 15, 1.5),
    18: (100, 330, 45, 13, 1.5),
    19: (5, 20, 5, 12, 1.0),
}


class WeatherError(RuntimeError):
    """Raised when weather data cannot be fetched or parsed."""


def parse_weather(payload: dict[str, Any]) -> pd.DataFrame:
    """Parse an Open-Meteo payload into a timezone-aware hourly DataFrame.

    Args:
        payload: Open-Meteo JSON response.

    Returns:
        DataFrame indexed by the provider's timezone with internal weather columns.

    Raises:
        WeatherError: If required fields, timezone data, or array lengths are invalid.
    """
    try:
        timezone_name = payload["timezone"]
        hourly = payload["hourly"]
        times = hourly["time"]
        values = {field: hourly[field] for field in HOURLY_FIELDS}
    except (KeyError, TypeError) as error:
        raise WeatherError("Open-Meteo response is missing required hourly fields") from error

    if not isinstance(timezone_name, str) or not timezone_name:
        raise WeatherError("Open-Meteo response has an invalid timezone")
    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as error:
        raise WeatherError(f"Unknown weather timezone: {timezone_name}") from error

    lengths = {len(times), *(len(series) for series in values.values())}
    if len(lengths) != 1 or not times:
        raise WeatherError("Open-Meteo hourly arrays must be non-empty and equal in length")

    try:
        parsed_times = [datetime.fromisoformat(timestamp) for timestamp in times]
        if any(value.tzinfo is not None for value in parsed_times):
            raise ValueError("provider timestamps must be local wall-clock values")
        index = pd.DatetimeIndex(parsed_times).tz_localize(timezone)
    except (TypeError, ValueError) as error:
        raise WeatherError("Open-Meteo hourly times are invalid") from error

    frame = pd.DataFrame(
        {
            "ghi": values["shortwave_radiation"],
            "dni": values["direct_normal_irradiance"],
            "dhi": values["diffuse_radiation"],
            "temp_air": values["temperature_2m"],
            "cloud_cover": values["cloud_cover"],
            "wind": values["wind_speed_10m"],
        },
        index=index,
    )
    if frame.isna().any().any():
        raise WeatherError("Open-Meteo hourly fields must not contain null values")
    return frame


def fetch_weather(lat: float, lon: float, days: int) -> pd.DataFrame:
    """Fetch hourly weather or return the configured offline mock forecast.

    Args:
        lat: Latitude in degrees.
        lon: Longitude in degrees.
        days: Number of forecast days.

    Returns:
        Timezone-aware hourly weather DataFrame.

    Raises:
        WeatherError: If Open-Meteo is unavailable or returns invalid data.
    """
    if USE_MOCK_WEATHER:
        return make_mock_weather(days)

    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": ",".join(HOURLY_FIELDS),
        "forecast_days": days,
        "timezone": "auto",
        "wind_speed_unit": "ms",
    }
    try:
        with httpx.Client(timeout=WEATHER_TIMEOUT_SECONDS) as client:
            response = client.get(OPEN_METEO_URL, params=params)
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as error:
        raise WeatherError(f"Unable to fetch weather from Open-Meteo: {error}") from error
    return parse_weather(payload)


def make_mock_weather(days: int) -> pd.DataFrame:
    """Build the bible's labelled clear-day weather fixture for ``days`` days."""
    if days < 1:
        raise ValueError("days must be at least 1")

    timezone = ZoneInfo(MOCK_TIMEZONE)
    rows: list[dict[str, float]] = []
    timestamps: list[datetime] = []
    for day_offset in range(days):
        current_date = MOCK_START_DATE + timedelta(days=day_offset)
        for hour in range(24):
            ghi, dni, dhi, temp_air, wind = MOCK_DAY.get(hour, (0, 0, 0, 10, 1.0))
            timestamps.append(datetime.combine(current_date, time(hour), timezone))
            rows.append(
                {
                    "ghi": ghi,
                    "dni": dni,
                    "dhi": dhi,
                    "temp_air": temp_air,
                    "cloud_cover": 5.0,
                    "wind": wind,
                }
            )
    return pd.DataFrame(rows, index=pd.DatetimeIndex(timestamps))
