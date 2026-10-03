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


def test_hourly_resolution_returns_hourly_and_daily_only() -> None:
    data = client.post("/api/forecast", json={**BODY, "days": 5}).json()
    assert len(data["hourly"]) == 120 and len(data["daily"]) == 5 and data["monthly"] == []


def test_daily_resolution_skips_hourly_rows() -> None:
    data = client.post("/api/forecast", json={**BODY, "days": 14, "resolution": "daily"}).json()
    assert data["hourly"] == [] and len(data["daily"]) == 14 and data["monthly"] == []
    assert all(row["kwh"] > 0 for row in data["daily"])


def test_monthly_resolution_totals_match_daily_sums() -> None:
    body = {**BODY, "days": 90, "resolution": "monthly"}
    data = client.post("/api/forecast", json=body).json()
    assert data["hourly"] == [] and data["daily"] == [] and len(data["monthly"]) >= 3
    assert sum(month["days"] for month in data["monthly"]) == 90
    daily = pipeline.build_forecast(request(days=90, resolution="daily"), make_mock_weather(90)).daily
    assert sum(month["kwh"] for month in data["monthly"]) == pytest.approx(daily["kwh"].sum())


def test_year_long_monthly_forecast_is_allowed() -> None:
    data = client.post("/api/forecast", json={**BODY, "days": 365, "resolution": "monthly"}).json()
    assert sum(month["days"] for month in data["monthly"]) == 365


def test_horizon_limits_return_422() -> None:
    assert client.post("/api/forecast", json={**BODY, "days": 366, "resolution": "daily"}).status_code == 422
    hourly_too_long = client.post("/api/forecast", json={**BODY, "days": 40})
    assert hourly_too_long.status_code == 422
    assert "hourly resolution supports at most 31 days" in hourly_too_long.text
    assert client.post("/api/forecast", json={**BODY, "resolution": "weekly"}).status_code == 422


needs_native = pytest.mark.skipif(solar.ENGINE != "native", reason="native library not built")


def optimize_cards(data: dict) -> list[dict]:
    return [card for card in data["recommendations"] if card["subtopic"] == "optimize"]


@needs_native
def test_north_facing_roof_gets_an_optimize_card() -> None:
    body = {**BODY, "panel": {**PANEL, "azimuth": 0}}
    cards = optimize_cards(client.post("/api/forecast", json=body).json())
    assert len(cards) == 1
    assert cards[0]["kwh_effect"] > 0
    assert "azimuth 180" in cards[0]["title"]


@needs_native
def test_good_orientation_gets_no_optimize_card() -> None:
    assert optimize_cards(client.post("/api/forecast", json=BODY).json()) == []


@needs_native
def test_only_the_bad_panel_group_is_flagged() -> None:
    bad = {**PANEL, "azimuth": 0, "count": 4}
    body = {key: value for key, value in BODY.items() if key != "panel"}
    cards = optimize_cards(client.post("/api/forecast", json={**body, "panels": [PANEL, bad]}).json())
    assert len(cards) == 1 and "azimuth 0" in cards[0]["reason"]


def test_page_has_status_chips() -> None:
    page = client.get("/calculator").text
    for element_id in ("weather-status", "calibration-status", "readings-status", "engine-status"):
        assert f'id="{element_id}"' in page


def cloudy(weather, cloud_pct=90.0, keep=0.25):
    dim = weather.copy()
    dim[["ghi", "dni", "dhi"]] = dim[["ghi", "dni", "dhi"]] * keep
    dim["cloud_cover"] = cloud_pct
    return dim


def test_hourly_rows_carry_the_weather_forecast() -> None:
    hourly = pipeline.build_forecast(request(), cloudy(make_mock_weather(1))).hourly
    for column in ("cloud_cover", "temp_air", "wind_ms", "ghi_clear", "p_ac_clear_w"):
        assert column in hourly.columns
    assert (hourly["cloud_cover"] == 90.0).all()
    assert (hourly["p_ac_clear_w"] >= hourly["p_ac_w"]).all()
    assert (hourly["ghi_clear"] >= hourly["ghi"]).all()


def test_clouds_cost_energy_and_clear_days_cost_almost_none() -> None:
    clear = pipeline.build_forecast(request(), make_mock_weather(1)).daily.iloc[0]
    dim = pipeline.build_forecast(request(), cloudy(make_mock_weather(1))).daily.iloc[0]
    assert dim["cloud_loss_pct"] > 60 and dim["cloud_loss_pct"] > clear["cloud_loss_pct"] + 40
    assert dim["avg_cloud_cover"] == pytest.approx(90.0)
    assert dim["kwh"] < dim["clear_sky_kwh"]
    assert dim["kwh"] == pytest.approx(clear["kwh"] * 0.25, rel=0.35)


def test_monthly_cloud_numbers_are_consistent_with_daily() -> None:
    weather = cloudy(make_mock_weather(45), cloud_pct=60.0, keep=0.5)
    result = pipeline.build_forecast(request(days=45, resolution="daily"), weather)
    assert result.monthly["clear_sky_kwh"].sum() == pytest.approx(result.daily["clear_sky_kwh"].sum())
    assert result.monthly["avg_cloud_cover"].between(0, 100).all()
    assert result.monthly["cloud_loss_pct"].between(0, 100).all()


def test_route_returns_cloud_fields() -> None:
    data = client.post("/api/forecast", json=BODY).json()
    assert {"cloud_cover", "temp_air", "wind_ms", "ghi_clear", "p_ac_clear_w"} <= set(data["hourly"][0])
    assert {"avg_cloud_cover", "clear_sky_kwh", "cloud_loss_pct"} <= set(data["daily"][0])
    monthly = client.post("/api/forecast", json={**BODY, "days": 60, "resolution": "monthly"}).json()
    assert {"avg_cloud_cover", "clear_sky_kwh", "cloud_loss_pct"} <= set(monthly["monthly"][0])
