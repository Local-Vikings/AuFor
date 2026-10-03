"""app/clouds.py and GET /api/cloud-field: cloud and wind forecast over the visible map."""

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import clouds, weather
from app.config import CLOUD_FIELD_COLS, CLOUD_FIELD_ROWS
from main import app

client = TestClient(app)
SOFIA_VIEW = (41.5, 21.7, 43.9, 24.9)  # south, west, north, east
BASE = 1_790_000_000 - 1_790_000_000 % 3600  # a whole UTC hour


def fake_payload(lats, lons, hours, shifted=()):
    """Open-Meteo style list; points in `shifted` start one hour later (another timezone)."""
    places = []
    for k in range(len(lats) * len(lons)):
        first = BASE + (3600 if k in shifted else 0)
        times = [first + 3600 * h for h in range(hours + 24 + 24)]
        places.append({"hourly": {
            "time": times,
            "cloud_cover": [(t - BASE) // 3600 % 100 for t in times],
            "wind_speed_10m": [5.0] * len(times),
            "wind_direction_10m": [270.0] * len(times),
        }})
    return places


def test_view_axes_cover_the_view_and_snap_outward() -> None:
    lats, lons = clouds.view_axes(*SOFIA_VIEW)
    assert len(lats) == CLOUD_FIELD_ROWS and len(lons) == CLOUD_FIELD_COLS
    assert lats[0] <= SOFIA_VIEW[0] and lats[-1] >= SOFIA_VIEW[2]
    assert lons[0] <= SOFIA_VIEW[1] and lons[-1] >= SOFIA_VIEW[3]
    assert lats == sorted(lats) and lons == sorted(lons)
    assert lats[0] % 0.25 == 0 and lons[-1] % 0.25 == 0


def test_small_pans_share_one_grid_so_the_cache_is_reused() -> None:
    assert clouds.view_axes(41.51, 21.72, 43.89, 24.88) == clouds.view_axes(*SOFIA_VIEW)


def test_view_axes_stay_inside_the_map_and_never_collapse() -> None:
    lats, lons = clouds.view_axes(84.0, 179.0, 90.0, 180.0)
    assert lats[-1] <= 85.0 and lons[-1] <= 180.0 and lats[-1] > lats[0] and lons[-1] > lons[0]


def test_site_bounds_are_square_in_kilometres() -> None:
    south, west, north, east = clouds.site_bounds(42.7, 23.3)
    assert (east - west) > (north - south)  # a degree of longitude is shorter at 43 N


def test_parse_aligns_points_on_different_clocks_by_epoch() -> None:
    lats, lons = clouds.view_axes(*SOFIA_VIEW)
    payload = fake_payload(lats, lons, 48, shifted={0, 1, 15})  # corner points in another timezone
    field = clouds.parse_cloud_field(payload, lats, lons, 48)
    assert field.cloud.shape == (48, CLOUD_FIELD_ROWS, CLOUD_FIELD_COLS)
    assert np.allclose(field.cloud, field.cloud[:, :1, :1])  # same absolute time, same value
    assert field.times[0] == BASE + 24 * 3600


def test_parse_rejects_wrong_grid_and_missing_data() -> None:
    lats, lons = clouds.view_axes(*SOFIA_VIEW)
    with pytest.raises(weather.WeatherError, match="grid size"):
        clouds.parse_cloud_field([{}], lats, lons, 24)
    broken = fake_payload(lats, lons, 24)
    del broken[40]["hourly"]["cloud_cover"]
    with pytest.raises(weather.WeatherError, match="missing hourly"):
        clouds.parse_cloud_field(broken, lats, lons, 24)
    with pytest.raises(weather.WeatherError, match="shorter"):
        clouds.parse_cloud_field(fake_payload(lats, lons, 24), lats, lons, 200)


def test_missing_values_are_filled() -> None:
    lats, lons = clouds.view_axes(*SOFIA_VIEW)
    payload = fake_payload(lats, lons, 24)
    payload[3]["hourly"]["cloud_cover"][30] = None
    field = clouds._fill_gaps(clouds.parse_cloud_field(payload, lats, lons, 24))
    assert not np.isnan(field.cloud).any()


def test_mock_field_is_deterministic_drifts_and_has_varying_wind() -> None:
    lats, lons = clouds.view_axes(*SOFIA_VIEW)
    first = clouds.make_mock_cloud_field(lats, lons, 48)
    again = clouds.make_mock_cloud_field(lats, lons, 48)
    np.testing.assert_array_equal(first.cloud, again.cloud)
    assert first.source == "mock" and first.cloud.min() >= 0 and first.cloud.max() <= 100
    assert not np.allclose(first.cloud[0], first.cloud[6])  # the pattern moves with time
    assert first.wind_speed.std() > 0.5 and first.wind_dir.std() > 3  # wind differs across the map


def view_params(**extra):
    south, west, north, east = SOFIA_VIEW
    return {"south": south, "west": west, "north": north, "east": east, **extra}


def test_endpoint_returns_the_field_for_the_requested_view() -> None:
    data = client.get("/api/cloud-field", params=view_params(hours=24)).json()
    assert len(data["times"]) == 24 and len(data["lats"]) == CLOUD_FIELD_ROWS and len(data["lons"]) == CLOUD_FIELD_COLS
    assert np.array(data["cloud"]).shape == (24, CLOUD_FIELD_ROWS, CLOUD_FIELD_COLS)
    assert set(data) == {"times", "lats", "lons", "cloud", "wind_speed", "wind_dir", "source"}
    assert data["lats"][0] <= SOFIA_VIEW[0] and data["lons"][-1] >= SOFIA_VIEW[3]


def test_endpoint_still_accepts_a_site() -> None:
    data = client.get("/api/cloud-field", params={"lat": 42.7, "lon": 23.3, "hours": 24}).json()
    assert data["lats"][0] < 42.7 < data["lats"][-1] and data["lons"][0] < 23.3 < data["lons"][-1]


def test_endpoint_serves_the_whole_16_day_range_compressed() -> None:
    response = client.get("/api/cloud-field", params=view_params(hours=384), headers={"Accept-Encoding": "gzip"})
    assert response.status_code == 200 and response.headers.get("content-encoding") == "gzip"
    assert len(response.json()["times"]) == 384


def test_endpoint_validates_parameters() -> None:
    assert client.get("/api/cloud-field").status_code == 422
    assert client.get("/api/cloud-field", params={"lat": 95, "lon": 0}).status_code == 422
    assert client.get("/api/cloud-field", params=view_params(hours=385)).status_code == 422
    flipped = client.get("/api/cloud-field", params={"south": 44, "west": 20, "north": 42, "east": 25})
    assert flipped.status_code == 422 and "south < north" in flipped.json()["detail"]
    huge = client.get("/api/cloud-field", params={"south": -60, "west": -170, "north": 60, "east": 170})
    assert huge.status_code == 422 and "zoom in" in huge.json()["detail"]


def test_weather_failure_returns_502(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*_args):
        raise weather.WeatherError("Unable to fetch weather from Open-Meteo: timeout")

    monkeypatch.setattr("app.weather.USE_MOCK_WEATHER", False)
    monkeypatch.setattr(weather, "_get_json", broken)
    clouds._CACHE.clear()
    response = client.get("/api/cloud-field", params=view_params())
    assert response.status_code == 502 and "Open-Meteo" in response.json()["detail"]


def test_real_path_builds_one_request_and_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def fake(url, params):
        calls.append(params)
        lats, lons = clouds.view_axes(50.0, 8.0, 52.0, 11.0)
        return fake_payload(lats, lons, 24)

    monkeypatch.setattr("app.weather.USE_MOCK_WEATHER", False)
    monkeypatch.setattr(weather, "_get_json", fake)
    clouds._CACHE.clear()
    clouds.fetch_cloud_field(50.0, 8.0, 52.0, 11.0, 24)
    clouds.fetch_cloud_field(50.01, 8.02, 51.98, 10.97, 24)  # a slightly different view, same snapped grid
    assert len(calls) == 1
    assert len(calls[0]["latitude"].split(",")) == CLOUD_FIELD_ROWS * CLOUD_FIELD_COLS
    assert calls[0]["timeformat"] == "unixtime" and calls[0]["past_days"] == 1 and calls[0]["forecast_days"] == 2
    clouds._CACHE.clear()
