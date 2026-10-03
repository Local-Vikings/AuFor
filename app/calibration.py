"""Calibrate the performance ratio (PR) from measured power readings (bible 9.9, T51).

The forecast assumes PR 0.80. When real or simulated power readings exist, the PR is
moved toward what they show:

    PR_new  = clip(sum(measured Wh) / sum(predicted Wh at PR = 1), 0.5, 1.05)
    PR_used = 0.7 * PR_old + 0.3 * PR_new

Only hours that tell something about PR are used: daytime (plane-of-array irradiance above
100 W/m2) and not clipped by the inverter, and at least 12 readings in total, otherwise the PR
stays as it is. The PR is stored per configured system and moves one smoothing step each time
new readings have arrived (detected by their database id, so late or out-of-order readings count),
so it converges over several updates and never twice on the same data.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from app import readings
from app.config import (
    CALIBRATION_MIN_IRRADIANCE_W_M2,
    CALIBRATION_MIN_POINTS,
    CALIBRATION_PR_MAX,
    CALIBRATION_PR_MIN,
    CALIBRATION_WEIGHT_OLD,
    CALIBRATION_WINDOW_HOURS,
    DEFAULT_PERFORMANCE_RATIO,
    REAL_READING_SOURCES,
)
from app.models import ForecastRequest

_SCHEMA = """
CREATE TABLE IF NOT EXISTS calibration (
    system_key TEXT PRIMARY KEY,
    pr REAL NOT NULL,
    last_id INTEGER NOT NULL,
    points INTEGER NOT NULL,
    source TEXT NOT NULL,
    updated_ts INTEGER NOT NULL
);
"""
_EPOCH = pd.Timestamp("1970-01-01", tz="UTC")


@dataclass(frozen=True)
class CalibrationState:
    """The performance ratio in use and how it was obtained."""

    pr: float
    calibrated: bool
    points: int = 0
    source: str | None = None  # "real" or "simulated"
    last_id: int = 0  # newest reading used so far; only readings with a higher id count as new


@dataclass(frozen=True)
class Estimate:
    """What the readings say the PR is."""

    pr_new: float
    points: int
    measured_wh: float
    predicted_wh: float
    last_id: int
    source: str


# ---- pure maths ----


def compute_pr_new(measured_wh: float, predicted_wh: float) -> float:
    """PR the readings imply, clipped to [0.5, 1.05]."""
    return float(min(max(measured_wh / predicted_wh, CALIBRATION_PR_MIN), CALIBRATION_PR_MAX))


def smooth(pr_old: float, pr_new: float) -> float:
    """One smoothing step toward the new estimate."""
    return CALIBRATION_WEIGHT_OLD * pr_old + (1.0 - CALIBRATION_WEIGHT_OLD) * pr_new


def hour_label(timestamp: int) -> int:
    """The forecast hour a reading belongs to. Weather values average the PRECEDING hour, so a reading
    at 11:30 belongs to the row labelled 12:00 (and one exactly at 12:00 also to 12:00)."""
    return -(-timestamp // 3600) * 3600


def _estimate_from(hourly: pd.DataFrame, rows: list[dict], source: str) -> Estimate | None:
    labels = ((hourly.index.tz_convert("UTC") - _EPOCH).total_seconds()).to_numpy().astype(int)
    usable = {
        int(label): float(p_dc)
        for label, p_dc, poa, clipped in zip(labels, hourly["p_dc_w"], hourly["g_poa"], hourly["clipped"], strict=True)
        if poa > CALIBRATION_MIN_IRRADIANCE_W_M2 and p_dc > 0 and not clipped
    }
    buckets: dict[int, list[float]] = {}
    last_id = 0
    for row in rows:
        ts = int(row["timestamp"].timestamp())
        label = hour_label(ts)
        if label in usable:
            buckets.setdefault(label, []).append(row["value"])
            last_id = max(last_id, row["id"])
    points = sum(len(values) for values in buckets.values())
    if points < CALIBRATION_MIN_POINTS:
        return None
    measured = sum(float(np.mean(values)) for values in buckets.values())  # W for one hour = Wh
    predicted = sum(usable[label] for label in buckets)
    return Estimate(compute_pr_new(measured, predicted), points, measured, predicted, last_id, source)


def estimate(hourly: pd.DataFrame, rows: list[dict]) -> Estimate | None:
    """Estimate PR from power readings against the forecast table; real readings beat simulated ones.

    Returns None when there are fewer than 12 valid readings (daytime, not clipped).
    """
    real = [r for r in rows if r["source"] in REAL_READING_SOURCES]
    fake = [r for r in rows if r["source"] not in REAL_READING_SOURCES]
    return _estimate_from(hourly, real, "real") or _estimate_from(hourly, fake, "simulated")


# ---- stored state ----


def system_key(request: ForecastRequest) -> str:
    """Identify the configured system, so a PR learned for one array is not applied to another."""
    system = {
        "lat": round(request.lat, 2),
        "lon": round(request.lon, 2),
        "panels": sorted(
            (g.count, g.watt_peak, g.tilt, g.azimuth, g.noct, g.gamma, g.inverter_max_w) for g in request.panel_groups
        ),
    }
    return hashlib.sha1(json.dumps(system, sort_keys=True).encode()).hexdigest()[:16]


def _connect() -> sqlite3.Connection:
    connection = readings.connect()
    connection.executescript(_SCHEMA)
    return connection


def load(key: str) -> CalibrationState:
    """The stored PR for a system, or the default 0.80 (not calibrated)."""
    with closing(_connect()) as connection:
        row = connection.execute("SELECT pr, last_id, points, source FROM calibration WHERE system_key = ?", (key,)).fetchone()
    if row is None:
        return CalibrationState(DEFAULT_PERFORMANCE_RATIO, False)
    return CalibrationState(row[0], True, row[2], row[3], row[1])


def update(key: str, hourly: pd.DataFrame, now: datetime | None = None) -> CalibrationState:
    """Take one smoothing step if new valid readings have arrived since the last one.

    Args:
        key: system_key of the configured system.
        hourly: forecast table from the pipeline (needs p_dc_w, g_poa, clipped).
        now: clock for tests.

    Returns:
        The state to use for this forecast.
    """
    state = load(key)
    clock = now or datetime.now(timezone.utc)
    rows = readings.recent("power_w", CALIBRATION_WINDOW_HOURS, now=clock, include_id=True)
    found = estimate(hourly, rows)
    if found is None or found.last_id <= state.last_id:
        return state
    pr = round(smooth(state.pr, found.pr_new), 4)
    with closing(_connect()) as connection, connection:
        connection.execute(
            "INSERT INTO calibration (system_key, pr, last_id, points, source, updated_ts) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(system_key) DO UPDATE SET pr = excluded.pr, last_id = excluded.last_id, "
            "points = excluded.points, source = excluded.source, updated_ts = excluded.updated_ts",
            (key, pr, found.last_id, found.points, found.source, int(clock.timestamp())),
        )
    return CalibrationState(pr, True, found.points, found.source, found.last_id)


def reset(key: str | None = None) -> int:
    """Forget the stored PR of one system (or of all). Returns how many were removed."""
    with closing(_connect()) as connection, connection:
        cursor = connection.execute("DELETE FROM calibration" + (" WHERE system_key = ?" if key else ""), (key,) if key else ())
        return cursor.rowcount


def all_states() -> list[dict]:
    """Every stored calibration, for inspection."""
    with closing(_connect()) as connection:
        rows = connection.execute("SELECT system_key, pr, points, source, updated_ts FROM calibration ORDER BY updated_ts").fetchall()
    return [
        {"system_key": k, "pr": pr, "points": n, "source": s, "updated": datetime.fromtimestamp(u, timezone.utc)}
        for k, pr, n, s, u in rows
    ]
