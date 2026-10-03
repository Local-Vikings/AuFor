from __future__ import annotations

from datetime import date

import httpx
import pandas as pd
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


def _hourly_payload(start: str, end: str) -> dict:
    index = pd.date_range(start, end, freq="h")
    count = len(index)
    return {
        "timezone": "Europe/Sofia",
        "hourly": {
            "time": [stamp.strftime("%Y-%m-%dT%H:%M") for stamp in index],
            "temperature_2m": [10.0] * count,
            "shortwave_radiation": [100.0] * count,
            "direct_normal_irradiance": [200.0] * count,
            "diffuse_radiation": [50.0] * count,
            "cloud_cover": [20.0] * count,
            "wind_speed_10m": [2.0] * count,
        },
    }


def test_long_horizon_appends_last_years_weather(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict]] = []

    def fake_get_json(url: str, params: dict) -> dict:
        calls.append((url, params))
        if url == weather.OPEN_METEO_URL:
            return _hourly_payload("2026-10-03T00:00", "2026-10-18T23:00")  # 16 days
        return _hourly_payload(params["start_date"], params["end_date"] + "T23:00")

    monkeypatch.setattr(weather, "USE_MOCK_WEATHER", False)
    monkeypatch.setattr(weather, "_get_json", fake_get_json)

    frame = weather.fetch_weather(42.7, 23.3, 30)

    assert abs(len(frame) - 30 * 24) <= 1  # the repeated DST hour is dropped
    assert frame.index.is_unique and frame.index.is_monotonic_increasing
    assert str(frame.index.tz) == "Europe/Sofia"
    assert frame.attrs["sources"] == ["open-meteo", weather.CLIMATOLOGY_LABEL]
    forecast_call, archive_call = calls
    assert forecast_call[1]["forecast_days"] == 16
    assert archive_call[0] == weather.OPEN_METEO_ARCHIVE_URL
    assert archive_call[1]["start_date"] == "2025-10-19"
    assert archive_call[1]["end_date"] == "2025-11-01"


def test_short_horizon_uses_only_the_forecast_api(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def fake_get_json(url: str, params: dict) -> dict:
        seen.append(url)
        return _hourly_payload("2026-10-03T00:00", "2026-10-05T23:00")

    monkeypatch.setattr(weather, "USE_MOCK_WEATHER", False)
    monkeypatch.setattr(weather, "_get_json", fake_get_json)
    frame = weather.fetch_weather(42.7, 23.3, 3)
    assert seen == [weather.OPEN_METEO_URL]
    assert frame.attrs["sources"] == ["open-meteo"]


def test_parse_weather_survives_the_daylight_saving_change() -> None:
    payload = _hourly_payload("2025-10-25T22:00", "2025-10-26T06:00")
    frame = weather.parse_weather(payload)
    assert frame.index.is_unique and frame.index.is_monotonic_increasing
    assert len(frame) >= 8


def test_a_rate_limit_gives_a_short_readable_message_not_the_url(monkeypatch: pytest.MonkeyPatch) -> None:
    class Limited:
        status_code = 429

        def json(self) -> dict:
            return {"error": True, "reason": "Daily API request limit exceeded. Please try again tomorrow."}

    class FakeClient:
        def __init__(self, *, timeout: float) -> None:
            pass

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *args: object) -> bool:
            return False

        def get(self, url: str, params: dict) -> object:
            request = httpx.Request("GET", url, params={"latitude": ",".join(["42.5"] * 600)})
            response = httpx.Response(429, json=Limited().json(), request=request)
            response.raise_for_status()

    monkeypatch.setattr(weather.httpx, "Client", FakeClient)
    with pytest.raises(weather.WeatherError) as caught:
        weather._get_json(weather.OPEN_METEO_URL, {})
    message = str(caught.value)
    assert "free request limit" in message and "try again tomorrow" in message.lower() and "USE_MOCK_WEATHER=1" in message
    assert "latitude" not in message and len(message) < 300  # no 4 KB URL


def test_other_http_errors_name_the_status_without_the_url(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeClient:
        def __init__(self, *, timeout: float) -> None:
            pass

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *args: object) -> bool:
            return False

        def get(self, url: str, params: dict) -> object:
            response = httpx.Response(503, text="down", request=httpx.Request("GET", url))
            response.raise_for_status()

    monkeypatch.setattr(weather.httpx, "Client", FakeClient)
    with pytest.raises(weather.WeatherError, match="Open-Meteo answered HTTP 503") as caught:
        weather._get_json(weather.OPEN_METEO_URL, {})
    assert "http" not in str(caught.value).lower().replace("http 503", "")


def _counting_fetch(monkeypatch: pytest.MonkeyPatch, payloads: list) -> list:
    """Replace the network with a list of answers (an exception in it is raised); returns the call log."""
    calls: list[dict] = []

    def fake_get_json(url: str, params: dict) -> dict:
        calls.append(params)
        answer = payloads[min(len(calls), len(payloads)) - 1]
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(weather, "_get_json", fake_get_json)
    return calls


def _age_cache(seconds: float) -> None:
    for path in weather.Path(weather.WEATHER_CACHE_DIR).glob("*.json"):
        stamp = path.stat().st_mtime - seconds
        weather.os.utime(path, (stamp, stamp))


def test_cache_reuses_a_fresh_answer_without_the_network(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _counting_fetch(monkeypatch, [{"a": 1}])
    assert weather.cached_json(weather.OPEN_METEO_URL, {"x": 1}) == ({"a": 1}, None)
    assert weather.cached_json(weather.OPEN_METEO_URL, {"x": 1}) == ({"a": 1}, None)
    assert len(calls) == 1
    weather.cached_json(weather.OPEN_METEO_URL, {"x": 2})  # other parameters, other entry
    assert len(calls) == 2


def test_cache_refreshes_an_old_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _counting_fetch(monkeypatch, [{"a": 1}, {"a": 2}])
    weather.cached_json(weather.OPEN_METEO_URL, {"x": 1})
    _age_cache(weather.WEATHER_CACHE_SECONDS + 1)
    assert weather.cached_json(weather.OPEN_METEO_URL, {"x": 1}) == ({"a": 2}, None)
    assert len(calls) == 2


def test_cache_serves_a_stale_answer_when_open_meteo_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    _counting_fetch(monkeypatch, [{"a": 1}, weather.WeatherError("quota used up")])
    weather.cached_json(weather.OPEN_METEO_URL, {"x": 1})
    _age_cache(2 * 3600)
    payload, stale_age = weather.cached_json(weather.OPEN_METEO_URL, {"x": 1})
    assert payload == {"a": 1} and stale_age == pytest.approx(7200, abs=60)
    assert weather.cached_label(stale_age) == "open-meteo (cached 2.0 h ago, live request failed)"


def test_cache_does_not_serve_answers_older_than_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    _counting_fetch(monkeypatch, [{"a": 1}, weather.WeatherError("quota used up")])
    weather.cached_json(weather.OPEN_METEO_URL, {"x": 1})
    _age_cache(weather.WEATHER_STALE_MAX_SECONDS + 60)
    with pytest.raises(weather.WeatherError, match="quota"):
        weather.cached_json(weather.OPEN_METEO_URL, {"x": 1})


def test_failure_without_a_cached_answer_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _counting_fetch(monkeypatch, [weather.WeatherError("no network")])
    with pytest.raises(weather.WeatherError, match="no network"):
        weather.cached_json(weather.OPEN_METEO_URL, {"x": 1})


def test_forecast_from_a_stale_cache_is_labelled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(weather, "USE_MOCK_WEATHER", False)
    _counting_fetch(monkeypatch, [_hourly_payload("2026-10-03T00:00", "2026-10-05T23:00"), weather.WeatherError("HTTP 429")])
    assert weather.fetch_weather(42.7, 23.3, 3).attrs["sources"] == ["open-meteo"]
    _age_cache(3600)
    sources = weather.fetch_weather(42.7, 23.3, 3).attrs["sources"]
    assert sources == ["open-meteo (cached 1.0 h ago, live request failed)"]
