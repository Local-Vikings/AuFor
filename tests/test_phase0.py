from datetime import datetime, timezone

from fastapi.testclient import TestClient

from main import app
from app.models import ReadingCreate

client = TestClient(app)


def test_health_and_index() -> None:
    assert client.get("/api/health").json() == {"status": "ok"}
    assert client.get("/").status_code == 200


def test_forecast_stub_has_contract_shape() -> None:
    response = client.post(
        "/api/forecast",
        json={
            "lat": 42.6977,
            "lon": 23.3219,
            "panel": {
                "count": 10,
                "watt_peak": 400,
                "tilt": 35,
                "azimuth": 180,
                "noct": 45,
                "gamma": -0.004,
                "inverter_max_w": 4000,
            },
            "battery": {
                "capacity_kwh": 10,
                "dod": 0.9,
                "eta_c": 0.95,
                "eta_d": 0.95,
                "max_power_kw": 5,
                "initial_soc_kwh": 5,
            },
            "load": {"daily_kwh": 12},
            "days": 3,
        },
    )
    assert response.status_code == 200
    assert set(response.json()) == {
        "hourly",
        "daily",
        "monthly",
        "recommendations",
        "explanation",
        "explanation_source",
        "explain_id",
        "meta",
    }


def test_invalid_forecast_values_return_422() -> None:
    payload = {
        "lat": 95,
        "lon": 200,
        "panel": {
            "count": 0,
            "watt_peak": 400,
            "tilt": 120,
            "azimuth": -10,
            "noct": 45,
            "gamma": -0.004,
            "inverter_max_w": 4000,
        },
        "battery": {
            "capacity_kwh": -1,
            "dod": 1.5,
            "eta_c": 0.95,
            "eta_d": 0.95,
            "max_power_kw": 5,
            "initial_soc_kwh": 0,
        },
        "load": {"daily_kwh": 12},
    }
    assert client.post("/api/forecast", json=payload).status_code == 422


def test_readings_require_timezone_aware_timestamps() -> None:
    valid = ReadingCreate(
        source="simulated",
        type="power_w",
        value=1875,
        timestamp=datetime(2026, 10, 3, 11, tzinfo=timezone.utc),
    )
    assert valid.timestamp.tzinfo is not None
    try:
        ReadingCreate(
            source="simulated",
            type="power_w",
            value=1875,
            timestamp=datetime(2026, 10, 3, 11),
        )
    except ValueError as error:
        assert "timezone" in str(error)
    else:
        raise AssertionError("naive timestamp was accepted")
