from __future__ import annotations

from datetime import date

import httpx
import pytest

from app import weather


def make_payload() -> dict:
    return {
        "timezone": "Europe/Sofia",
        "hourly": {
            "time": ["2026-10-03T00:00", "2026-10-03T12:00"],
            "temperature_2m": [10, 18],
            "shortwave_radiation": [0, 610],
            "direct_normal_irradiance": [0, 840],
            "diffuse_radiation": [0, 88],
            "cloud_cover": [5, 5],
            "wind_speed_10m": [1, 2.5],
        },
    }


def test_parse_weather_maps_fields_and_timezone() -> None:
    frame = weather.parse_weather(make_payload())

    assert list(frame.columns) == ["ghi", "dni", "dhi", "temp_air", "cloud_cover", "wind"]
    assert frame.loc[frame.index[1], "ghi"] == 610
    assert str(frame.index.tz) == "Europe/Sofia"


def test_parse_weather_rejects_missing_or_unequal_fields() -> None:
    missing = make_payload()
    del missing["hourly"]["wind_speed_10m"]
    with pytest.raises(weather.WeatherError, match="missing required"):
        weather.parse_weather(missing)

    unequal = make_payload()
    unequal["hourly"]["cloud_cover"] = [5]
    with pytest.raises(weather.WeatherError, match="equal in length"):
        weather.parse_weather(unequal)


def test_fetch_weather_builds_open_meteo_request(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return make_payload()

    class FakeClient:
        def __init__(self, *, timeout: float) -> None:
            captured["timeout"] = timeout

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def get(self, url: str, *, params: dict[str, object]) -> FakeResponse:
            captured["url"] = url
            captured["params"] = params
            return FakeResponse()

    monkeypatch.setattr(weather.httpx, "Client", FakeClient)
    monkeypatch.setattr(weather, "USE_MOCK_WEATHER", False)

    frame = weather.fetch_weather(42.6977, 23.3219, 2)

    params = captured["params"]
    assert captured["url"] == weather.OPEN_METEO_URL
    assert captured["timeout"] == weather.WEATHER_TIMEOUT_SECONDS
    assert isinstance(params, dict)
    assert params["latitude"] == 42.6977
    assert params["longitude"] == 23.3219
    assert params["forecast_days"] == 2
    assert params["timezone"] == "auto"
    assert "shortwave_radiation" in params["hourly"]
    assert len(frame) == 2


def test_fetch_weather_wraps_http_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    class BrokenClient:
        def __init__(self, *, timeout: float) -> None:
            pass

        def __enter__(self) -> "BrokenClient":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def get(self, url: str, *, params: dict[str, object]) -> None:
            raise httpx.TimeoutException("request timed out")

    monkeypatch.setattr(weather.httpx, "Client", BrokenClient)
    monkeypatch.setattr(weather, "USE_MOCK_WEATHER", False)

    with pytest.raises(weather.WeatherError, match="Unable to fetch weather"):
        weather.fetch_weather(42.6977, 23.3219, 1)


def test_mock_weather_is_offline_and_repeats_requested_days(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ForbiddenClient:
        def __init__(self, **kwargs: object) -> None:
            raise AssertionError("mock weather must not construct an HTTP client")

    monkeypatch.setattr(weather.httpx, "Client", ForbiddenClient)
    monkeypatch.setattr(weather, "USE_MOCK_WEATHER", True)

    frame = weather.fetch_weather(42.6977, 23.3219, 3)

    assert len(frame) == 72
    assert frame.index.tz is not None
    assert frame.index[0].date() == date(2026, 10, 3)
    assert frame[frame.index.hour == 13]["ghi"].gt(0).all()
    assert frame[frame.index.hour == 2]["ghi"].eq(0).all()
