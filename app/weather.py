"""Fetch and parse hourly weather data for AuFor.

This module owns Open-Meteo access and the offline mock-weather switch. It does
not calculate solar power, simulate batteries, or translate errors into HTTP responses.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time as clock
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
import pandas as pd

from app.config import (
    OPEN_METEO_MAX_FORECAST_DAYS,
    USE_MOCK_WEATHER,
    WEATHER_CACHE_DIR,
    WEATHER_CACHE_SECONDS,
    WEATHER_STALE_MAX_SECONDS,
    WEATHER_TIMEOUT_SECONDS,
)

logger = logging.getLogger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
CLIMATOLOGY_LABEL = "climatology (same dates last year)"
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
        index = pd.DatetimeIndex(parsed_times).tz_localize(
            timezone, ambiguous="NaT", nonexistent="shift_forward"
        )
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
    # The repeated hour when clocks go back is ambiguous; drop it instead of guessing.
    frame = frame[frame.index.notna()]
    frame = frame[~frame.index.duplicated(keep="first")]
    if frame.isna().any().any():
        raise WeatherError("Open-Meteo hourly fields must not contain null values")
    return frame


def _status_message(response: httpx.Response) -> str:
    """A short, readable reason for an HTTP error (never the request URL, which can be thousands of characters)."""
    try:
        reason = str(response.json().get("reason", ""))
    except ValueError:
        reason = ""
    if response.status_code == 429:
        return (f"Open-Meteo's free request limit is used up for now ({reason or 'HTTP 429'}). "
                "Try again later, or run with USE_MOCK_WEATHER=1 to work offline")
    return f"Open-Meteo answered HTTP {response.status_code}" + (f": {reason}" if reason else "")


def _get_json(url: str, params: dict[str, Any]) -> dict[str, Any]:
    try:
        with httpx.Client(timeout=WEATHER_TIMEOUT_SECONDS) as client:
            response = client.get(url, params=params)
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as error:
        raise WeatherError(f"Unable to fetch weather: {_status_message(error.response)}") from error
    except (httpx.HTTPError, ValueError) as error:
        raise WeatherError(f"Unable to fetch weather from Open-Meteo: {error}") from error


def _cache_path(url: str, params: dict[str, Any]) -> Path:
    key = hashlib.sha1(json.dumps([url, params], sort_keys=True, default=str).encode()).hexdigest()
    return Path(WEATHER_CACHE_DIR) / f"{key}.json"


def _read_cache(path: Path) -> tuple[dict[str, Any], float] | None:
    """(payload, age in seconds) of a cached answer, or None if there is no readable one."""
    try:
        age = clock.time() - path.stat().st_mtime
        return json.loads(path.read_text()), age
    except (OSError, ValueError):
        return None


def _write_cache(path: Path, payload: dict[str, Any]) -> None:
    """Save an answer atomically and drop answers too old to be served even as a fallback."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload))
        os.replace(temporary, path)
        for old in path.parent.glob("*.json"):
            if clock.time() - old.stat().st_mtime > WEATHER_STALE_MAX_SECONDS:
                old.unlink(missing_ok=True)
    except OSError as error:  # a read-only disk must not break the forecast
        logger.warning("could not write the weather cache: %s", error)


def cached_json(url: str, params: dict[str, Any], fresh_seconds: float = WEATHER_CACHE_SECONDS) -> tuple[dict[str, Any], float | None]:
    """Open-Meteo JSON through a disk cache, so reloads and restarts do not spend the free daily quota.

    Returns:
        (payload, stale_age): stale_age is None for a live or fresh answer, or the age in seconds
        of an older cached answer used because Open-Meteo failed (rate limit, no network).

    Raises:
        WeatherError: If Open-Meteo fails and no cached answer is young enough.
    """
    path = _cache_path(url, params)
    saved = _read_cache(path)
    if saved and saved[1] < fresh_seconds:
        return saved[0], None
    try:
        payload = _get_json(url, params)
    except WeatherError as error:
        if saved is None or saved[1] > WEATHER_STALE_MAX_SECONDS:
            raise
        logger.warning("Open-Meteo failed (%s); using a cached answer %.0f min old", error, saved[1] / 60)
        return saved[0], saved[1]
    _write_cache(path, payload)
    return payload, None


def cached_label(stale_age: float) -> str:
    """Source label for a forecast built from an older cached answer."""
    return f"open-meteo (cached {stale_age / 3600:.1f} h ago, live request failed)"


def _localize(naive: pd.DatetimeIndex, timezone: Any) -> pd.DatetimeIndex:
    """Attach a timezone, dropping wall-clock hours that do not exist or repeat (DST)."""
    return naive.tz_localize(timezone, ambiguous="NaT", nonexistent="NaT")


def _climatology(lat: float, lon: float, forecast: pd.DataFrame, days: int) -> pd.DataFrame:
    """Hourly weather for the days after the forecast, from the same dates one year earlier."""
    timezone = forecast.index.tz
    first_day = forecast.index[-1].normalize().tz_localize(None) + pd.Timedelta(days=1)
    start_day = forecast.index[0].normalize().tz_localize(None)
    last_day = start_day + pd.Timedelta(days=days - 1)
    last_year = pd.DateOffset(years=1)
    params = {
        "latitude": lat,
        "longitude": lon,
        "start_date": (first_day - last_year).date().isoformat(),
        "end_date": (last_day - last_year).date().isoformat(),
        "hourly": ",".join(HOURLY_FIELDS),
        "timezone": "auto",
        "wind_speed_unit": "ms",
    }
    archive = parse_weather(cached_json(OPEN_METEO_ARCHIVE_URL, params, WEATHER_STALE_MAX_SECONDS)[0])  # the past does not change
    shifted = _localize(archive.index.tz_localize(None) + last_year, timezone)
    archive = archive.set_axis(shifted)
    archive = archive[archive.index.notna()]
    archive = archive[~archive.index.duplicated(keep="first")]

    wanted_naive = pd.date_range(first_day, last_day + pd.Timedelta(hours=23), freq="h")
    wanted = _localize(wanted_naive, timezone)
    wanted = wanted[wanted.notna()]
    return archive.reindex(wanted).ffill().bfill()


def fetch_weather(lat: float, lon: float, days: int) -> pd.DataFrame:
    """Fetch hourly weather or return the configured offline mock forecast.

    Open-Meteo forecasts reach OPEN_METEO_MAX_FORECAST_DAYS days. Longer horizons
    append the same dates from the previous year (a climate average, not a
    forecast). The sources used are listed in ``frame.attrs["sources"]``.

    Args:
        lat: Latitude in degrees.
        lon: Longitude in degrees.
        days: Number of days to cover.

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
        "forecast_days": min(days, OPEN_METEO_MAX_FORECAST_DAYS),
        "timezone": "auto",
        "wind_speed_unit": "ms",
    }
    payload, stale_age = cached_json(OPEN_METEO_URL, params)
    frame = parse_weather(payload)
    sources = ["open-meteo" if stale_age is None else cached_label(stale_age)]
    if days > OPEN_METEO_MAX_FORECAST_DAYS:
        frame = pd.concat([frame, _climatology(lat, lon, frame, days)])
        sources.append(CLIMATOLOGY_LABEL)
    frame.attrs["sources"] = sources
    return frame


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
    frame = pd.DataFrame(rows, index=pd.DatetimeIndex(timestamps))
    frame.attrs["sources"] = ["mock weather"]
    return frame
