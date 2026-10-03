"""app/pipeline.py: weather -> hourly power -> daily kWh (T15) and the /api/forecast route."""

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import pipeline, solar
from app.models import ForecastRequest
from app.weather import WeatherError, make_mock_weather
from main import app

PANEL = {
    "count": 10, "watt_peak": 400, "tilt": 35, "azimuth": 180,
    "noct": 45, "gamma": -0.004, "inverter_max_w": 4000,
}
BODY = {
    "lat": 42.6977, "lon": 23.3219, "panel": PANEL,
    "battery": {
        "capacity_kwh": 10, "dod": 0.9, "eta_c": 0.95, "eta_d": 0.95,
        "max_power_kw": 5, "initial_soc_kwh": 5,
    },
    "load": {"daily_kwh": 12}, "days": 3,
}


def request(**overrides) -> ForecastRequest:
    return ForecastRequest.model_validate({**BODY, **overrides})


def test_three_day_mock_forecast_has_72_hours_and_3_daily_totals() -> None:
    result = pipeline.build_forecast(request(), make_mock_weather(3))
    assert len(result.hourly) == 72
    assert len(result.daily) == 3
    assert result.hourly.index.tz is not None


def test_night_is_zero_and_midday_is_positive() -> None:
    hourly = pipeline.build_forecast(request(), make_mock_weather(1)).hourly
    assert hourly.loc[hourly.index.hour <= 4, "p_ac_w"].eq(0).all()
    assert hourly.loc[hourly.index.hour == 12, "p_ac_w"].iloc[0] > 1500
    assert hourly["p_ac_w"].max() <= 4000 * 0.8 + 1e-9
    assert 10 <= hourly["p_ac_w"].idxmax().hour <= 14


def test_daily_total_matches_hourly_sum() -> None:
    result = pipeline.build_forecast(request(), make_mock_weather(3))
    for day, row in result.daily.iterrows():
        chunk = result.hourly[result.hourly.index.date.astype(str) == day]
        assert row["kwh"] == pytest.approx(chunk["p_ac_w"].sum() / 1000.0)
        assert 0 <= row["self_consumption_pct"] <= 100


def test_south_facing_beats_north_facing() -> None:
    weather = make_mock_weather(1)
    south = pipeline.build_forecast(request(), weather).daily["kwh"].iloc[0]
    north_panel = {**PANEL, "azimuth": 0}
    north = pipeline.build_forecast(request(panel=north_panel), weather).daily["kwh"].iloc[0]
    assert south > north


def test_two_groups_sum_like_one_big_group() -> None:
    weather = make_mock_weather(1)
    one = pipeline.build_forecast(request(), weather).hourly["p_ac_w"]
    half = {**PANEL, "count": 5, "inverter_max_w": 2000}
    two = pipeline.build_forecast(
        request(panel=None, panels=[half, half]), weather
    ).hourly["p_ac_w"]
    np.testing.assert_allclose(one.to_numpy(), two.to_numpy(), rtol=1e-9)


def test_missing_irradiance_still_gives_a_forecast() -> None:
    weather = make_mock_weather(1)
    weather[["ghi", "dni", "dhi"]] = np.nan
    weather["cloud_cover"] = 30.0
    hourly = pipeline.build_forecast(request(), weather).hourly
    assert hourly["p_ac_w"].max() > 500
    assert hourly.loc[hourly.index.hour <= 4, "p_ac_w"].eq(0).all()


def test_pipeline_reports_the_engine() -> None:
    assert pipeline.build_forecast(request(), make_mock_weather(1)).engine == solar.ENGINE


client = TestClient(app)


def test_route_returns_real_rows() -> None:
    data = client.post("/api/forecast", json=BODY).json()
    assert len(data["hourly"]) == 72 and len(data["daily"]) == 3
    assert max(row["p_ac_w"] for row in data["hourly"]) > 1500
    assert data["meta"]["engine"] == solar.ENGINE
    assert all(1.0 <= row["soc_kwh"] <= 10.0 for row in data["hourly"])


def test_weather_failure_returns_502(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*_args) -> None:
        raise WeatherError("Unable to fetch weather from Open-Meteo: timeout")

    monkeypatch.setattr("app.pipeline.fetch_weather", broken)
    response = client.post("/api/forecast", json=BODY)
    assert response.status_code == 502
    assert "Open-Meteo" in response.json()["detail"]
