"""Gridded cloud-cover and wind forecast around a site, for the map overlay.

Fetches Open-Meteo for a small square grid of points (one request) and returns
arrays shaped [time][lat][lon]. Mock mode returns a deterministic drifting field so
the app and the tests work offline. No solar physics here.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np

from app import weather
from app.config import (
    CLOUD_FIELD_CACHE_SECONDS,
    CLOUD_FIELD_COLS,
    CLOUD_FIELD_ROWS,
    CLOUD_FIELD_SITE_SPAN_DEG,
    CLOUD_FIELD_SNAP_DEG,
    OPEN_METEO_MAX_FORECAST_DAYS,
)

CLOUD_VARIABLES = ("cloud_cover", "wind_speed_10m", "wind_direction_10m")
PAST_HOURS = 24  # past_days=1 pads every point so they can be aligned by epoch
_CACHE: dict[tuple, tuple[float, "CloudField"]] = {}


@dataclass(frozen=True)
class CloudField:
    """Hourly cloud cover (%), wind speed (m/s) and wind direction (deg it blows from)."""

    times: list[int]  # UTC epoch seconds
    lats: list[float]  # south to north
    lons: list[float]  # west to east
    cloud: np.ndarray  # [time][lat][lon], 0-100
    wind_speed: np.ndarray
    wind_dir: np.ndarray
    source: str

    def to_dict(self) -> dict[str, Any]:
        """Compact JSON-ready form."""
        return {
            "times": self.times,
            "lats": [round(v, 4) for v in self.lats],
            "lons": [round(v, 4) for v in self.lons],
            "cloud": np.rint(self.cloud).astype(int).tolist(),
            "wind_speed": np.round(self.wind_speed, 1).tolist(),
            "wind_dir": np.rint(self.wind_dir).astype(int).tolist(),
            "source": self.source,
        }


def view_axes(south: float, west: float, north: float, east: float) -> tuple[list[float], list[float]]:
    """Grid axes covering a map view: latitudes south to north, longitudes west to east.

    The bounds snap outward to CLOUD_FIELD_SNAP_DEG so that small pans hit the same cache entry.
    """
    snap = CLOUD_FIELD_SNAP_DEG
    low_lat = max(math.floor(south / snap) * snap, -85.0)
    high_lat = min(math.ceil(north / snap) * snap, 85.0)
    low_lon = max(math.floor(west / snap) * snap, -180.0)
    high_lon = min(math.ceil(east / snap) * snap, 180.0)
    high_lat = max(high_lat, low_lat + snap)
    high_lon = max(high_lon, low_lon + snap)
    lats = np.round(np.linspace(low_lat, high_lat, CLOUD_FIELD_ROWS), 4)
    lons = np.round(np.linspace(low_lon, high_lon, CLOUD_FIELD_COLS), 4)
    return lats.tolist(), lons.tolist()


def site_bounds(lat: float, lon: float) -> tuple[float, float, float, float]:
    """(south, west, north, east) of a square (in km) patch around a site."""
    span = CLOUD_FIELD_SITE_SPAN_DEG
    lon_span = span / max(math.cos(math.radians(lat)), 0.2)
    return lat - span, lon - lon_span, lat + span, lon + lon_span


def parse_cloud_field(
    payload: Any, lats: list[float], lons: list[float], hours: int, start: int = PAST_HOURS
) -> CloudField:
    """Turn an Open-Meteo multi-location response into a CloudField.

    Each point comes back on its own local clock (a grid can straddle a timezone, so
    the series start at different instants). Every point is aligned by UTC epoch onto
    the time axis of the centre point, starting ``start`` hours into it (local midnight
    when the request used ``past_days=1``).

    Raises:
        weather.WeatherError: If the response does not match the requested grid.
    """
    places = payload if isinstance(payload, list) else [payload]
    if len(places) != len(lats) * len(lons):
        raise weather.WeatherError("Open-Meteo cloud field response does not match the grid size")
    try:
        centre = places[(len(lats) // 2) * len(lons) + len(lons) // 2]
        times = [int(t) for t in centre["hourly"]["time"]][start : start + hours]
        axis = np.array(times, dtype=float)
        arrays = {
            name: np.array(
                [
                    np.interp(axis, [float(t) for t in place["hourly"]["time"]],
                              [_clean(v) for v in place["hourly"][name]])
                    for place in places
                ]
            )
            for name in CLOUD_VARIABLES
        }
    except (KeyError, TypeError, ValueError, IndexError) as error:
        raise weather.WeatherError("Open-Meteo cloud field response is missing hourly data") from error
    if len(times) < hours:
        raise weather.WeatherError("Open-Meteo cloud field response is shorter than requested")
    shape = (len(lats), len(lons), len(times))

    def cube(name: str) -> np.ndarray:
        return arrays[name].reshape(shape).transpose(2, 0, 1)  # -> [time][lat][lon]

    return CloudField(
        times, lats, lons,
        np.clip(cube("cloud_cover"), 0.0, 100.0), np.clip(cube("wind_speed_10m"), 0.0, None),
        np.mod(cube("wind_direction_10m"), 360.0), "open-meteo",
    )


def _clean(value: Any) -> float:
    return float(value) if value is not None else float("nan")


def _fill_gaps(field: CloudField) -> CloudField:
    """Replace missing values by the nearest valid value along time, or zero."""
    for array in (field.cloud, field.wind_speed, field.wind_dir):
        if np.isnan(array).any():
            flat = array.reshape(array.shape[0], -1)
            for column in range(flat.shape[1]):
                series = flat[:, column]
                valid = ~np.isnan(series)
                flat[:, column] = np.interp(np.arange(len(series)), np.flatnonzero(valid), series[valid]) if valid.any() else 0.0
    return field


def make_mock_cloud_field(lats: list[float], lons: list[float], hours: int) -> CloudField:
    """Deterministic clouds drifting east at the mock wind speed (offline mode, tests)."""
    zone = ZoneInfo(weather.MOCK_TIMEZONE)
    start = datetime.combine(weather.MOCK_START_DATE, datetime.min.time(), zone)
    times = [int((start + timedelta(hours=h)).timestamp()) for h in range(hours)]
    t = np.arange(hours)[:, None, None]
    y = np.linspace(0, 3.5, len(lats))[None, :, None]
    x = np.linspace(0, 6.0, len(lons))[None, None, :]
    cover = 50.0 + 45.0 * np.sin(1.1 * (x - 0.18 * t) + 0.6) * np.cos(0.9 * y + 0.04 * t)
    speed = 6.0 + 3.0 * np.sin(0.8 * x + 0.5 * y + 0.02 * t)
    direction = 270.0 + 25.0 * np.sin(0.6 * y - 0.5 * x + 0.03 * t)
    return CloudField(times, lats, lons, np.clip(cover, 0, 100), speed, np.mod(direction, 360.0), "mock")


def fetch_cloud_field(
    south: float, west: float, north: float, east: float, hours: int
) -> CloudField:
    """Hourly cloud and wind field over a map view for the next ``hours`` hours.

    Raises:
        weather.WeatherError: If Open-Meteo is unavailable or returns invalid data.
    """
    lats, lons = view_axes(south, west, north, east)
    if weather.USE_MOCK_WEATHER:
        return make_mock_cloud_field(lats, lons, hours)

    key = (lats[0], lats[-1], lons[0], lons[-1], hours)
    cached = _CACHE.get(key)
    if cached and time.monotonic() - cached[0] < CLOUD_FIELD_CACHE_SECONDS:
        return cached[1]

    pairs = [(a, b) for a in lats for b in lons]
    params = {
        "latitude": ",".join(f"{a:.4f}" for a, _ in pairs),
        "longitude": ",".join(f"{b:.4f}" for _, b in pairs),
        "hourly": ",".join(CLOUD_VARIABLES),
        "past_days": 1,
        "forecast_days": min(math.ceil(hours / 24) + 1, OPEN_METEO_MAX_FORECAST_DAYS),
        "timezone": "auto",
        "timeformat": "unixtime",
        "wind_speed_unit": "ms",
    }
    field = _fill_gaps(parse_cloud_field(weather._get_json(weather.OPEN_METEO_URL, params), lats, lons, hours))
    _CACHE[key] = (time.monotonic(), field)
    return field
