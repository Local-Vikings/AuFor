"""Store and read real measurements (bible 11, 19.7).

SQLite through the standard library, one short connection per call so it is safe
under FastAPI's thread pool. Timestamps are stored as UTC epoch seconds. This module
does not calibrate anything; it only keeps the data.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import (
    DATABASE_PATH,
    READING_FUTURE_TOLERANCE_S,
    READINGS_MAX_HOURS,
    REAL_READING_SOURCES,
)
from app.models import ReadingCreate

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    type TEXT NOT NULL,
    value REAL NOT NULL,
    ts INTEGER NOT NULL,
    UNIQUE (source, type, ts)
);
CREATE INDEX IF NOT EXISTS readings_type_ts ON readings (type, ts);
"""


class DuplicateReading(ValueError):
    """The same source, type and timestamp is already stored."""


class FutureReading(ValueError):
    """The timestamp is later than the allowed clock tolerance."""


def _connect() -> sqlite3.Connection:
    path = Path(DATABASE_PATH)
    path = path if path.is_absolute() else PROJECT_ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.executescript(_SCHEMA)
    return connection


def connect() -> sqlite3.Connection:
    """A connection to the readings database (the calibration state lives in the same file)."""
    return _connect()


def _epoch(moment: datetime) -> int:
    return int(moment.timestamp())


def _row(source: str, kind: str, value: float, ts: int, row_id: int | None = None) -> dict:
    row = {
        "source": source,
        "type": kind,
        "value": value,
        "timestamp": datetime.fromtimestamp(ts, timezone.utc),
    }
    if row_id is not None:
        row["id"] = row_id
    return row


def add_reading(reading: ReadingCreate, now: datetime | None = None) -> dict:
    """Store one reading.

    Raises:
        FutureReading: If the timestamp is more than the tolerance ahead of ``now``.
        DuplicateReading: If source, type and timestamp are already stored.
    """
    clock = now or datetime.now(timezone.utc)
    ts = _epoch(reading.timestamp)
    if ts > _epoch(clock) + READING_FUTURE_TOLERANCE_S:
        raise FutureReading("timestamp is in the future")
    try:
        with closing(_connect()) as connection, connection:
            connection.execute(
                "INSERT INTO readings (source, type, value, ts) VALUES (?, ?, ?, ?)",
                (reading.source, reading.type, reading.value, ts),
            )
    except sqlite3.IntegrityError as error:
        raise DuplicateReading("a reading with this source, type and timestamp already exists") from error
    return _row(reading.source, reading.type, reading.value, ts)


def recent(
    kind: str | None = None,
    hours: int = 24,
    source: str | None = None,
    now: datetime | None = None,
    include_id: bool = False,
) -> list[dict]:
    """Readings of the last ``hours`` hours, oldest first, optionally filtered.

    ``include_id`` adds the database id, which only ever grows: calibration uses it to notice new
    readings even when they carry older timestamps (for example a buffer flushed after an outage).
    """
    hours = min(max(hours, 1), READINGS_MAX_HOURS)
    clock = _epoch(now or datetime.now(timezone.utc))
    query = "SELECT source, type, value, ts, id FROM readings WHERE ts >= ? AND ts <= ?"
    args: list = [clock - hours * 3600, clock + READING_FUTURE_TOLERANCE_S]
    for column, wanted in (("type", kind), ("source", source)):
        if wanted:
            query += f" AND {column} = ?"
            args.append(wanted)
    with closing(_connect()) as connection:
        rows = connection.execute(query + " ORDER BY ts, id", args).fetchall()
    return [_row(*row[:4], row[4] if include_id else None) for row in rows]


def summary() -> dict:
    """Counts of stored readings (all time) for the status chip."""
    with closing(_connect()) as connection:
        rows = connection.execute("SELECT source, type, COUNT(*), MAX(ts) FROM readings GROUP BY source, type").fetchall()
    real = sum(count for source, _, count, _ in rows if source in REAL_READING_SOURCES)
    total = sum(count for _, _, count, _ in rows)
    by_type: dict[str, int] = {}
    for _, kind, count, _ in rows:
        by_type[kind] = by_type.get(kind, 0) + count
    last = max((ts for *_, ts in rows), default=None)
    return {
        "total": total,
        "real": real,
        "simulated": total - real,
        "last_timestamp": datetime.fromtimestamp(last, timezone.utc) if last is not None else None,
        "by_type": by_type,
    }
