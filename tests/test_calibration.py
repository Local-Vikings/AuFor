"""app/calibration.py: PR from readings, clipped and smoothed (bible 9.9, 19.3, T51)."""

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app import calibration
from app.calibration import CalibrationState
from main import app
from tests.fixtures.mock_data import make_request
from tests.test_pipeline import BODY

client = TestClient(app)
ZONE = "Europe/Sofia"


def hourly(overrides=None):
    """A 24 h forecast table: sunny 08:00-17:00 with 2000 W DC power; ``overrides`` maps hour -> changed columns."""
    index = pd.date_range("2026-10-03 00:00", periods=24, freq="h", tz=ZONE)
    frame = pd.DataFrame({"p_dc_w": 0.0, "g_poa": 0.0, "clipped": False}, index=index)
    for hour in range(8, 18):
        frame.loc[frame.index.hour == hour, ["p_dc_w", "g_poa"]] = [2000.0, 600.0]
    for hour, values in (overrides or {}).items():
        frame.loc[frame.index.hour == hour, list(values)] = list(values.values())
    return frame


def row(label_hour, value, minutes_before=30, source="simulated", row_id=1, day=3):
    """A reading inside the hour that ends at ``label_hour`` (Sofia time)."""
    moment = datetime(2026, 10, day, label_hour, 0, tzinfo=timezone(timedelta(hours=3))) - timedelta(minutes=minutes_before)
    return {"source": source, "type": "power_w", "value": value, "timestamp": moment.astimezone(timezone.utc), "id": row_id}


def batch(value, hours=range(9, 15), per_hour=3, source="simulated", first_id=1):
    rows, n = [], first_id
    spacing = 55 // per_hour  # evenly inside the hour that ends at the row label
    for hour in hours:
        for k in range(per_hour):
            rows.append(row(hour, value, minutes_before=55 - spacing * k, source=source, row_id=n))
            n += 1
    return rows


# ---- the numbers from the bible ----

def test_the_bible_example_0_72_and_0_776() -> None:
    pr_new = calibration.compute_pr_new(measured_wh=5760, predicted_wh=8000)
    assert pr_new == pytest.approx(0.72)
    assert calibration.smooth(0.80, pr_new) == pytest.approx(0.776)


def test_the_demo_numbers_0_736_gives_about_0_78_in_one_step() -> None:
    assert calibration.smooth(0.80, 0.736) == pytest.approx(0.7808)


@pytest.mark.parametrize("measured, expected", [(100, 0.5), (3000, 1.05), (1600, 0.8)])
def test_pr_new_is_clipped_to_0_5_and_1_05(measured, expected) -> None:
    assert calibration.compute_pr_new(measured, 2000) == pytest.approx(expected)


# ---- which readings count ----

def test_estimate_compares_mean_hourly_power_with_dc_power() -> None:
    found = calibration.estimate(hourly(), batch(1472.0))  # 1472 W on 2000 W DC power = PR 0.736
    assert found.pr_new == pytest.approx(0.736) and found.points == 18 and found.source == "simulated"


def test_readings_within_an_hour_are_averaged_not_added() -> None:
    few = calibration.estimate(hourly(), batch(1472.0, per_hour=2, hours=range(9, 15)))
    many = calibration.estimate(hourly(), batch(1472.0, per_hour=12, hours=range(9, 15)))
    assert few.pr_new == pytest.approx(many.pr_new) == pytest.approx(0.736)


def test_fewer_than_12_valid_readings_means_no_estimate() -> None:
    assert calibration.estimate(hourly(), batch(1472.0, hours=range(9, 13), per_hour=2)) is None  # 8 readings
    assert calibration.estimate(hourly(), batch(1472.0, hours=range(9, 13), per_hour=3)) is not None  # exactly 12


def test_night_and_dim_hours_are_ignored() -> None:
    day = batch(1472.0, hours=range(9, 13))  # 12 readings at PR 0.736
    night = batch(5000.0, hours=[3, 4, 22], first_id=100)  # would wreck the estimate if they counted
    found = calibration.estimate(hourly(), day + night)
    assert found.points == 12 and found.pr_new == pytest.approx(0.736)
    dim = hourly({h: {"g_poa": 90.0} for h in range(9, 18)})  # below 100 W/m2 everywhere
    assert calibration.estimate(dim, batch(1472.0)) is None
    just_enough = hourly({h: {"g_poa": 100.0} for h in range(9, 15)})  # exactly 100 is not above 100
    assert calibration.estimate(just_enough, batch(1472.0)) is None


def test_clipped_hours_are_ignored_so_clipping_is_not_read_as_a_low_pr() -> None:
    clipped = hourly({h: {"clipped": True} for h in range(9, 12)})
    mixed = batch(1472.0, hours=range(12, 15), per_hour=5) + batch(900.0, hours=range(9, 12), per_hour=5, first_id=200)
    found = calibration.estimate(clipped, mixed)
    assert found.points == 15 and found.pr_new == pytest.approx(0.736)  # the clipped hours (900 W) are left out
    assert calibration.estimate(clipped, batch(900.0, hours=range(9, 12), per_hour=5)) is None  # nothing usable left


def test_a_reading_belongs_to_the_hour_that_ends_at_the_row_label() -> None:
    labelled_12 = [row(12, 1000.0, minutes_before=m, row_id=i) for i, m in enumerate((59, 45, 30, 15, 1, 0), 1)]
    just_after = [row(12, 1000.0, minutes_before=-1, row_id=7 + i) for i in range(6)]  # 12:00:01 belongs to the 13:00 row
    only_12 = hourly({13: {"g_poa": 50.0}})  # 13:00 row is dim, so readings there are dropped
    found = calibration.estimate(only_12, labelled_12 + just_after + batch(1000.0, hours=range(9, 11), first_id=50))
    assert found.points == 6 + 6  # the six before 12:00 plus the 9:00 and 10:00 readings; 12:00:01 went to the dim 13:00 row


def test_real_readings_beat_simulated_ones_when_there_are_enough() -> None:
    rows = batch(1000.0, source="simulated") + batch(1600.0, source="panel", first_id=500)
    assert calibration.estimate(hourly(), rows).source == "real"
    assert calibration.estimate(hourly(), rows).pr_new == pytest.approx(0.8)
    few_real = batch(1000.0, source="simulated") + batch(1600.0, hours=range(9, 10), per_hour=2, source="panel", first_id=500)
    assert calibration.estimate(hourly(), few_real).source == "simulated"


def test_the_cloud_camera_is_not_power_and_unknown_hours_are_ignored() -> None:
    other_day = batch(1472.0, hours=range(9, 15))
    for r in other_day:
        r["timestamp"] += timedelta(days=5)  # outside the forecast table
    assert calibration.estimate(hourly(), other_day) is None


# ---- which system ----

def test_system_key_identifies_the_configured_system() -> None:
    a = calibration.system_key(make_request("default"))
    assert a == calibration.system_key(make_request("default", days=7))  # days do not matter
    assert a != calibration.system_key(make_request("north"))
    assert a != calibration.system_key(make_request("small"))


# ---- the stored state ----

def test_default_state_is_0_80_and_not_calibrated() -> None:
    assert calibration.load("nothing") == CalibrationState(0.80, False)


def test_update_moves_one_step_and_only_once_per_batch_of_readings(monkeypatch) -> None:
    readings_store = []
    monkeypatch.setattr("app.readings.recent", lambda *a, **k: list(readings_store))
    readings_store += batch(1472.0)
    first = calibration.update("k", hourly())
    assert first.calibrated and first.pr == pytest.approx(0.7808, abs=1e-4) and first.source == "simulated" and first.points == 18
    again = calibration.update("k", hourly())
    assert again.pr == first.pr  # same readings: no second step
    readings_store += batch(1472.0, hours=range(9, 12), first_id=1000)
    third = calibration.update("k", hourly())
    assert third.pr == pytest.approx(0.7808 * 0.7 + 0.736 * 0.3, abs=1e-3) and third.pr < first.pr


def test_late_readings_with_older_timestamps_still_count_as_new(monkeypatch) -> None:
    store = batch(1472.0, hours=range(12, 16))
    monkeypatch.setattr("app.readings.recent", lambda *a, **k: list(store))
    first = calibration.update("k", hourly())
    store += batch(1472.0, hours=range(9, 12), first_id=900)  # older hours, but newly stored (a flushed buffer)
    assert calibration.update("k", hourly()).pr < first.pr


def test_repeated_batches_converge_on_the_true_pr(monkeypatch) -> None:
    store, n = [], 0
    monkeypatch.setattr("app.readings.recent", lambda *a, **k: list(store))
    pr = []
    for _ in range(12):
        n += 100
        store += batch(1472.0, first_id=n)
        pr.append(calibration.update("k", hourly()).pr)
    assert pr == sorted(pr, reverse=True) and pr[0] == pytest.approx(0.7808, abs=1e-3)
    assert pr[-1] == pytest.approx(0.736, abs=0.01)  # converged


def test_too_few_readings_keep_the_default(monkeypatch) -> None:
    monkeypatch.setattr("app.readings.recent", lambda *a, **k: batch(1472.0, hours=range(9, 11), per_hour=3))
    assert calibration.update("k", hourly()) == CalibrationState(0.80, False)


def test_state_is_kept_per_system_and_survives_a_new_connection(monkeypatch) -> None:
    monkeypatch.setattr("app.readings.recent", lambda *a, **k: batch(1472.0))
    calibration.update("system-a", hourly())
    assert calibration.load("system-a").calibrated and not calibration.load("system-b").calibrated
    assert [s["system_key"] for s in calibration.all_states()] == ["system-a"]
    assert calibration.reset("system-a") == 1 and not calibration.load("system-a").calibrated


# ---- end to end through the API ----

def post(value, minutes_ago, source="simulated"):
    stamp = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).replace(microsecond=0).isoformat()
    return client.post("/api/readings", json={"source": source, "type": "power_w", "value": value, "timestamp": stamp})


def forecast():
    return client.post("/api/forecast", json=BODY).json()


def test_a_fresh_forecast_is_not_calibrated() -> None:
    meta = forecast()["meta"]
    assert meta["calibrated"] is False and meta["pr_used"] == 0.8 and meta["calibration_points"] == 0 and meta["calibration_source"] is None


def test_readings_from_outside_the_forecast_hours_change_nothing() -> None:
    for minutes in range(1, 30):
        post(1500.0, 60 * 24 * 10 + minutes)  # ten days ago: not in the forecast table
    assert forecast()["meta"]["calibrated"] is False


def test_the_readings_endpoint_feeds_the_forecast_and_the_forecast_gets_lower(monkeypatch) -> None:
    base = forecast()
    pr_state = calibration.system_key(make_request("default"))
    # readings exactly on the mock day's sunny hours, from a system that makes 92% of the model
    hours = [h for h in base["hourly"] if h["p_ac_w"] > 300 and h["time"].startswith("2026-10-03")]
    sent = 0
    for h in hours:
        label = datetime.fromisoformat(h["time"]).astimezone(timezone.utc)
        for k in (45, 30, 15):
            stamp = (label - timedelta(minutes=k)).isoformat()
            body = {"source": "simulated", "type": "power_w", "value": h["p_ac_w"] / base["meta"]["pr_used"] * 0.736, "timestamp": stamp}
            if datetime.fromisoformat(stamp) < datetime.now(timezone.utc) and client.post("/api/readings", json=body).status_code == 201:
                sent += 1
    assert sent >= 12
    after = forecast()
    assert after["meta"]["calibrated"] and after["meta"]["calibration_source"] == "simulated"
    assert after["meta"]["calibration_points"] >= 12 and 0.76 < after["meta"]["pr_used"] < 0.79
    assert sum(d["kwh"] for d in after["daily"]) < sum(d["kwh"] for d in base["daily"])  # lower PR, lower energy
    assert client.get("/api/calibration").json()[0]["system_key"] == pr_state
    assert forecast()["meta"]["pr_used"] == after["meta"]["pr_used"]  # same readings: stable


def test_reset_forgets_the_learned_pr_and_needs_the_key_when_one_is_set(monkeypatch) -> None:
    monkeypatch.setattr("app.config.READINGS_API_KEY", "s3cret")
    assert client.post("/api/calibration/reset").status_code == 401
    assert client.post("/api/calibration/reset", headers={"X-API-Key": "s3cret"}).json() == {"removed": 0}
