import pytest


@pytest.fixture(autouse=True)
def temporary_database(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Every test gets its own empty readings database."""
    monkeypatch.setattr("app.readings.DATABASE_PATH", str(tmp_path / "readings.db"))


@pytest.fixture(autouse=True)
def offline_weather(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never touch the network: force the bundled mock weather."""
    monkeypatch.setattr("app.weather.USE_MOCK_WEATHER", True)
