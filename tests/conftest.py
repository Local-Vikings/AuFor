import pytest


@pytest.fixture(autouse=True)
def temporary_database(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Every test gets its own empty readings database."""
    monkeypatch.setattr("app.readings.DATABASE_PATH", str(tmp_path / "readings.db"))


@pytest.fixture(autouse=True)
def offline_weather(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never touch the network: force the bundled mock weather."""
    monkeypatch.setattr("app.weather.USE_MOCK_WEATHER", True)


@pytest.fixture(autouse=True)
def temporary_weather_cache(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Cached Open-Meteo answers never leak between tests (or from the developer's data/ folder)."""
    monkeypatch.setattr("app.weather.WEATHER_CACHE_DIR", str(tmp_path / "weather_cache"))


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Whatever is in the developer's .env (LLM key, API key) must not change what tests do."""
    monkeypatch.setattr("app.config.LLM_ENABLED", False)
    monkeypatch.setattr("app.config.LLM_API_KEY", "")
    monkeypatch.setattr("app.config.READINGS_API_KEY", "")
