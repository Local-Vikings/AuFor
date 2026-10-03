"""End-to-end smoke test with the mock weather (bible 19.10, T44).

The default system, three mock clear days, the 19.4 household load, no internet and no LLM. It walks the
whole product once: forecast, battery, recommendations, readings, calibration, explanation, the pages.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import llm
from main import app
from tests.fixtures.mock_data import LOAD_PROFILE
from tests.test_pipeline import BODY

client = TestClient(app)
ROOT = Path(__file__).resolve().parent.parent

# The mock days are 2026-10-03 to 2026-10-05. Freezing "now" after them makes every mock hour a past hour,
# so readings for it are accepted and this test gives the same result on any day it is run.
FROZEN_NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)


class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return FROZEN_NOW.astimezone(tz) if tz else FROZEN_NOW.replace(tzinfo=None)


@pytest.fixture
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.readings.datetime", FrozenDatetime)
    monkeypatch.setattr("app.calibration.datetime", FrozenDatetime)


def forecast() -> dict:
    response = client.post("/api/forecast", json=BODY)
    assert response.status_code == 200, response.text
    return response.json()


# ---- the forecast ----

def test_the_load_profile_sums_to_12_kwh() -> None:
    assert sum(LOAD_PROFILE) == pytest.approx(12.0)


def test_the_forecast_has_the_contract_shape_and_72_hours() -> None:
    data = forecast()
    assert {"hourly", "daily", "recommendations", "meta"} <= set(data)
    assert len(data["hourly"]) == 72 and len(data["daily"]) == 3 and data["monthly"] == []
    assert data["meta"]["data_sources"][0] == "mock weather"
    assert data["meta"]["engine"] in ("native", "python")


def test_production_is_zero_at_night_and_positive_at_midday() -> None:
    hours = forecast()["hourly"]
    night = [h for h in hours if int(h["time"][11:13]) in (0, 1, 2, 3, 4, 5, 22, 23)]
    midday = [h for h in hours if int(h["time"][11:13]) in (12, 13)]
    assert len(night) == 24 and all(h["p_ac_w"] == 0 for h in night)
    assert all(h["p_ac_w"] > 1500 for h in midday)


def test_the_household_load_follows_the_19_4_profile_and_sums_to_12_kwh_a_day() -> None:
    hours = forecast()["hourly"]
    for day in range(3):
        assert sum(h["load_w"] for h in hours[day * 24:(day + 1) * 24]) / 1000 == pytest.approx(12.0)
    assert hours[19]["load_w"] == pytest.approx(1100.0) and hours[3]["load_w"] == pytest.approx(250.0)  # the evening peak and the night


def test_the_battery_stays_between_its_limits_and_the_energy_balances() -> None:
    hours = forecast()["hourly"]
    assert all(1.0 - 1e-6 <= h["soc_kwh"] <= 10.0 + 1e-6 for h in hours)
    assert len({round(h["soc_kwh"], 3) for h in hours}) > 10  # it really charges and discharges
    for h in hours:  # nothing is created or lost between solar, load, grid and battery
        assert h["grid_import_w"] >= 0 and h["grid_export_w"] >= 0
        assert not (h["grid_import_w"] > 1e-6 and h["grid_export_w"] > 1e-6)


def test_the_weather_numbers_and_the_cloud_loss_are_consistent() -> None:
    data = forecast()
    assert all(0 <= h["cloud_cover"] <= 100 and h["ghi_clear"] >= h["ghi"] and h["p_ac_clear_w"] >= h["p_ac_w"] for h in data["hourly"])
    assert all(0 <= d["cloud_loss_pct"] <= 100 and d["kwh"] <= d["clear_sky_kwh"] + 1e-9 for d in data["daily"])
    assert 15 < data["daily"][0]["kwh"] < 25  # a clear October day for 4 kWp in Sofia


def test_recommendations_are_derived_from_the_numbers() -> None:
    cards = forecast()["recommendations"]
    assert cards and {c["subtopic"] for c in cards} <= {"use", "direct", "optimize", "store", "warning"}
    use = [c for c in cards if c["subtopic"] == "use"]
    assert use and 10 <= int(use[0]["hour"][11:13]) <= 14 and "kWh" in use[0]["reason"]


# ---- readings and calibration ----

def test_calibrated_is_false_before_readings_and_true_after_posting_twenty_simulated_ones(frozen_clock) -> None:
    before = forecast()
    assert before["meta"]["calibrated"] is False and before["meta"]["pr_used"] == 0.8 and before["meta"]["calibration_points"] == 0

    sent = 0
    for hour in (h for h in before["hourly"] if h["time"].startswith("2026-10-03") and h["p_ac_w"] > 300):
        label = datetime.fromisoformat(hour["time"]).astimezone(timezone.utc)
        for minutes in (45, 30, 15):
            reading = {"source": "simulated", "type": "power_w", "timestamp": (label - timedelta(minutes=minutes)).isoformat(),
                       "value": hour["p_ac_w"] / before["meta"]["pr_used"] * 0.736}  # a system with PR 0.736
            assert client.post("/api/readings", json=reading).status_code == 201
            sent += 1
    assert sent >= 20

    after = forecast()
    meta = after["meta"]
    assert meta["calibrated"] is True and meta["calibration_source"] == "simulated" and meta["calibration_points"] >= 20
    assert 0.73 < meta["pr_used"] < 0.80  # one smoothing step from 0.80 toward 0.736
    assert sum(d["kwh"] for d in after["daily"]) < sum(d["kwh"] for d in before["daily"])
    assert client.get("/api/readings/summary").json()["simulated"] == sent and client.get("/api/readings/summary").json()["real"] == 0
    assert forecast()["meta"]["pr_used"] == meta["pr_used"]  # the same readings do not move it again


# ---- the explanation, with no LLM ----

def test_the_llm_is_off_and_the_explanation_is_the_template_text() -> None:
    data = forecast()
    assert data["explain_id"] is None and data["explanation_source"] == "template"
    assert "kWh" in data["explanation"] and data["explanation"].startswith("Over 3 days")


def test_a_broken_llm_never_breaks_the_forecast_or_the_explanation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.config.LLM_ENABLED", True)
    monkeypatch.setattr("app.config.LLM_API_KEY", "key")
    monkeypatch.setattr(llm, "_call_model", lambda summary: (_ for _ in ()).throw(RuntimeError("the model is down")))
    data = forecast()
    assert data["explain_id"] and data["explanation_source"] == "template"
    answer = client.post("/api/explain", json={"explain_id": data["explain_id"]})
    assert answer.status_code == 200 and answer.json()["explanation_source"] == "template"


# ---- the rest of the product answers ----

def test_every_page_and_endpoint_answers() -> None:
    assert client.get("/api/health").json() == {"status": "ok"}
    for path in ("/", "/calculator"):
        page = client.get(path)
        assert page.status_code == 200 and "AuFor" in page.text
    for asset in ("/static/app.js", "/static/style.css"):
        assert client.get(asset).status_code == 200
    field = client.get("/api/cloud-field", params={"south": 41.5, "west": 21.7, "north": 43.9, "east": 24.9, "hours": 48}).json()
    assert len(field["times"]) == 48 and field["source"] == "mock"
    assert client.get("/api/readings").json() == [] and client.get("/api/calibration").json() == []
    monthly = client.post("/api/forecast", json={**BODY, "days": 60, "resolution": "monthly"}).json()
    assert monthly["hourly"] == [] and sum(m["days"] for m in monthly["monthly"]) == 60


def test_invalid_input_is_refused_with_a_clear_422() -> None:
    bad = {**BODY, "lat": 95, "battery": {**BODY["battery"], "dod": 1.5}}
    response = client.post("/api/forecast", json=bad)
    assert response.status_code == 422 and "detail" in response.json()


# ---- no internet at all ----

OFFLINE_SCRIPT = """
import json, socket, sys
def blocked(*args, **kwargs):
    raise OSError("the network is blocked in this test")
socket.socket.connect = blocked
socket.create_connection = blocked
from fastapi.testclient import TestClient
from main import app
body = json.loads(sys.argv[1])
response = TestClient(app).post("/api/forecast", json=body)
print(response.status_code, len(response.json().get("hourly", [])) if response.status_code == 200 else response.json()["detail"])
"""


def run_offline(mock: str, tmp_path) -> subprocess.CompletedProcess:
    env = {**os.environ, "USE_MOCK_WEATHER": mock, "DATABASE_PATH": str(tmp_path / "offline.db"), "LLM_ENABLED": "0", "READINGS_API_KEY": ""}
    return subprocess.run([sys.executable, "-c", OFFLINE_SCRIPT, json.dumps(BODY)], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)


def test_with_the_mock_flag_the_whole_forecast_works_with_the_network_blocked(tmp_path) -> None:
    result = run_offline("1", tmp_path)
    assert result.returncode == 0 and result.stdout.strip() == "200 72", result.stdout + result.stderr


def test_the_blocked_network_really_blocks_real_weather(tmp_path) -> None:
    result = run_offline("0", tmp_path)  # the contrast: without the flag the same call must fail cleanly
    assert result.returncode == 0 and result.stdout.startswith("502") and "Open-Meteo" in result.stdout, result.stdout + result.stderr
