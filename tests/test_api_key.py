"""READINGS_API_KEY: generated at start-up when missing, so writes are never open by accident."""

import pytest
from fastapi.testclient import TestClient

from app import config
from main import app


def test_a_missing_key_is_generated_and_saved_to_env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    env = tmp_path / ".env"
    env.write_text("USE_MOCK_WEATHER=0\nREADINGS_API_KEY=\nLOG_LEVEL=INFO")
    monkeypatch.setattr("app.config.READINGS_API_KEY", "")
    assert "generated" in config.ensure_readings_api_key(env)
    key = config.READINGS_API_KEY
    assert len(key) >= 24
    assert env.read_text() == f"USE_MOCK_WEATHER=0\nREADINGS_API_KEY={key}\nLOG_LEVEL=INFO"


def test_the_key_line_is_appended_when_env_has_none_or_does_not_exist(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    env = tmp_path / ".env"
    monkeypatch.setattr("app.config.READINGS_API_KEY", "")
    config.ensure_readings_api_key(env)
    assert env.read_text() == f"READINGS_API_KEY={config.READINGS_API_KEY}\n"
    other = tmp_path / "other.env"
    other.write_text("CARTO_API_KEY=x")
    monkeypatch.setattr("app.config.READINGS_API_KEY", "")
    config.ensure_readings_api_key(other)
    assert other.read_text() == f"CARTO_API_KEY=x\nREADINGS_API_KEY={config.READINGS_API_KEY}\n"


def test_a_set_key_or_off_is_left_alone(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    env = tmp_path / ".env"
    monkeypatch.setattr("app.config.READINGS_API_KEY", "mine")
    assert "need the X-API-Key" in config.ensure_readings_api_key(env)
    assert config.READINGS_API_KEY == "mine" and not env.exists()
    monkeypatch.setattr("app.config.READINGS_API_KEY", "OFF")
    assert "open" in config.ensure_readings_api_key(env)
    assert config.READINGS_API_KEY == "" and not env.exists()


def test_a_server_started_without_a_key_refuses_anonymous_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.config.READINGS_API_KEY", "")
    reading = {"source": "panel", "type": "power_w", "value": 1.0, "timestamp": "2026-01-01T12:00:00+00:00"}
    with TestClient(app) as client:  # runs the start-up hook
        key = config.READINGS_API_KEY
        assert key
        assert client.post("/api/readings", json=reading).status_code == 401
        assert client.post("/api/readings", json=reading, headers={"X-API-Key": key}).status_code == 201
    assert key in (config.ENV_FILE).read_text()  # the temporary .env from conftest, never the real one
