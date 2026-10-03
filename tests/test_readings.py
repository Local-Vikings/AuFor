"""app/readings.py and /api/readings: the plug-in endpoint and its edge cases (bible 11, 19.7)."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import readings
from app.models import ReadingCreate
from main import app

client = TestClient(app)


def ago(minutes: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


def body(kind="power_w", value=1875.0, source="simulated", minutes=30, **overrides):
    return {"source": source, "type": kind, "value": value, "timestamp": ago(minutes), **overrides}


def test_post_then_get_roundtrip() -> None:
    created = client.post("/api/readings", json=body())
    assert created.status_code == 201 and created.json()["source"] == "simulated"
    stored = client.get("/api/readings", params={"type": "power_w"}).json()
    assert len(stored) == 1 and stored[0]["value"] == 1875.0 and stored[0]["type"] == "power_w"


def test_the_bible_sample_rows_are_valid_payloads() -> None:
    for kind, value in (("power_w", 1875.0), ("soc_kwh", 6.4), ("cloud_fraction", 0.12)):
        assert client.post("/api/readings", json=body(kind, value, minutes=10 + int(value))).status_code == 201


def test_timestamps_are_stored_in_utc_whatever_offset_was_sent() -> None:
    moment = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=1)
    local = moment.astimezone(timezone(timedelta(hours=3))).isoformat()
    client.post("/api/readings", json=body(timestamp=local))
    stored = client.get("/api/readings").json()[0]
    assert datetime.fromisoformat(stored["timestamp"]) == moment
    assert stored["timestamp"].endswith("Z") or stored["timestamp"].endswith("+00:00")


# ---- the edge cases from bible 19.7 ----

def test_duplicate_timestamp_is_rejected_with_409() -> None:
    row = body()
    assert client.post("/api/readings", json=row).status_code == 201
    again = client.post("/api/readings", json=row)
    assert again.status_code == 409 and "already exists" in again.json()["detail"]
    assert len(client.get("/api/readings").json()) == 1  # not stored twice


def test_same_timestamp_from_another_source_or_type_is_not_a_duplicate() -> None:
    row = body()
    assert client.post("/api/readings", json=row).status_code == 201
    assert client.post("/api/readings", json={**row, "source": "panel"}).status_code == 201
    assert client.post("/api/readings", json={**row, "type": "soc_kwh", "value": 5.0}).status_code == 201


def test_future_timestamp_is_rejected() -> None:
    future = client.post("/api/readings", json=body(minutes=-60))
    assert future.status_code == 422 and "future" in future.json()["detail"]
    assert client.post("/api/readings", json=body(minutes=-2)).status_code == 201  # clock a little ahead is fine


def test_negative_power_is_rejected() -> None:
    assert client.post("/api/readings", json=body(value=-5.0)).status_code == 422


def test_unknown_type_and_source_are_rejected() -> None:
    assert client.post("/api/readings", json=body(kind="temperature")).status_code == 422
    assert client.post("/api/readings", json=body(source="satellite")).status_code == 422


def test_naive_timestamp_is_rejected() -> None:
    naive = (datetime.now(timezone.utc) - timedelta(minutes=5)).replace(tzinfo=None).isoformat()
    response = client.post("/api/readings", json=body(timestamp=naive))
    assert response.status_code == 422 and "timezone" in response.text


def test_cloud_fraction_must_be_between_zero_and_one() -> None:
    assert client.post("/api/readings", json=body("cloud_fraction", 1.5)).status_code == 422
    assert client.post("/api/readings", json=body("cloud_fraction", 0.909, source="camera")).status_code == 201


def test_extra_and_missing_fields_are_rejected() -> None:
    assert client.post("/api/readings", json={**body(), "note": "x"}).status_code == 422
    assert client.post("/api/readings", json={"source": "panel", "type": "power_w"}).status_code == 422


# ---- reading back ----

def test_get_filters_by_type_source_and_window_and_sorts_oldest_first() -> None:
    client.post("/api/readings", json=body("power_w", 300, minutes=10))
    client.post("/api/readings", json=body("power_w", 100, minutes=50))
    client.post("/api/readings", json=body("power_w", 200, minutes=30, source="panel"))
    client.post("/api/readings", json=body("soc_kwh", 6.0, minutes=20))
    client.post("/api/readings", json=body("power_w", 999, minutes=60 * 30))  # 30 hours old

    power = client.get("/api/readings", params={"type": "power_w"}).json()
    assert [row["value"] for row in power] == [100, 200, 300]  # oldest first, the 30 h old one is outside 24 h
    assert [r["value"] for r in client.get("/api/readings", params={"type": "power_w", "hours": 1}).json()] == [100, 200, 300]
    assert [r["value"] for r in client.get("/api/readings", params={"type": "power_w", "hours": 40}).json()][0] == 999
    only_panel = client.get("/api/readings", params={"source": "panel"}).json()
    assert [row["value"] for row in only_panel] == [200]
    assert len(client.get("/api/readings").json()) == 4  # all types


def test_get_with_nothing_stored_returns_an_empty_list() -> None:
    assert client.get("/api/readings", params={"type": "power_w"}).json() == []


def test_get_validates_its_parameters() -> None:
    assert client.get("/api/readings", params={"type": "bogus"}).status_code == 422
    assert client.get("/api/readings", params={"hours": 0}).status_code == 422
    assert client.get("/api/readings", params={"hours": 24 * 31}).status_code == 422


def test_simulated_rows_stay_labelled_simulated() -> None:
    client.post("/api/readings", json=body(source="simulated"))
    client.post("/api/readings", json=body(source="panel", minutes=40))
    sources = {row["source"] for row in client.get("/api/readings").json()}
    assert sources == {"simulated", "panel"}


def test_summary_tells_none_simulated_and_real_apart() -> None:
    empty = client.get("/api/readings/summary").json()
    assert empty == {"total": 0, "real": 0, "simulated": 0, "last_timestamp": None, "by_type": {}}
    client.post("/api/readings", json=body(minutes=40))
    only_fake = client.get("/api/readings/summary").json()
    assert only_fake["total"] == 1 and only_fake["simulated"] == 1 and only_fake["real"] == 0
    client.post("/api/readings", json=body("cloud_fraction", 0.4, source="camera", minutes=5))
    both = client.get("/api/readings/summary").json()
    assert both["total"] == 2 and both["real"] == 1 and both["simulated"] == 1
    assert both["by_type"] == {"power_w": 1, "cloud_fraction": 1} and both["last_timestamp"] is not None


# ---- the store itself ----

def test_data_survives_a_new_connection_and_uses_the_configured_path(tmp_path) -> None:
    reading = ReadingCreate(source="panel", type="power_w", value=42.0, timestamp=datetime.now(timezone.utc) - timedelta(minutes=3))
    readings.add_reading(reading)
    assert (tmp_path / "readings.db").exists()
    assert readings.recent("power_w", 1)[0]["value"] == 42.0  # read through a fresh connection


def test_add_and_recent_accept_a_fixed_clock() -> None:
    now = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
    sample = ReadingCreate(source="simulated", type="power_w", value=1875.0, timestamp=datetime(2026, 10, 3, 11, 0, tzinfo=timezone(timedelta(hours=3))))
    readings.add_reading(sample, now=now)
    assert readings.recent("power_w", 24, now=now)[0]["timestamp"] == datetime(2026, 10, 3, 8, 0, tzinfo=timezone.utc)
    with pytest.raises(readings.DuplicateReading):
        readings.add_reading(sample, now=now)
    future = sample.model_copy(update={"timestamp": now + timedelta(hours=2)})
    with pytest.raises(readings.FutureReading):
        readings.add_reading(future, now=now)
