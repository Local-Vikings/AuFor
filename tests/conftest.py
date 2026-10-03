import pytest


@pytest.fixture(autouse=True)
def offline_weather(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never touch the network: force the bundled mock weather."""
    monkeypatch.setattr("app.weather.USE_MOCK_WEATHER", True)
