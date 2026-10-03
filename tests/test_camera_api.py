"""API key on writes and POST /api/camera/analyze (the model may live on the server or on the Pi)."""

import sys
import types
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import readings, vision
from main import app

client = TestClient(app)


def reading(minutes=5):
    stamp = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    return {"source": "camera", "type": "cloud_fraction", "value": 0.909, "timestamp": stamp}


@pytest.fixture
def key_on(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.config.READINGS_API_KEY", "s3cret")


# ---- API key ----

def test_writes_are_open_when_no_key_is_configured() -> None:
    assert client.post("/api/readings", json=reading()).status_code == 201


def test_writes_need_the_key_when_one_is_configured(key_on) -> None:
    assert client.post("/api/readings", json=reading()).status_code == 401
    assert client.post("/api/readings", json=reading(), headers={"X-API-Key": "wrong"}).status_code == 401
    ok = client.post("/api/readings", json=reading(), headers={"X-API-Key": "s3cret"})
    assert ok.status_code == 201


def test_reads_stay_open_so_the_page_needs_no_key(key_on) -> None:
    client.post("/api/readings", json=reading(), headers={"X-API-Key": "s3cret"})
    assert client.get("/api/readings").status_code == 200
    assert client.get("/api/readings/summary").json()["real"] == 1


def test_a_rejected_key_stores_nothing_and_says_why(key_on) -> None:
    response = client.post("/api/readings", json=reading())
    assert response.status_code == 401 and "X-API-Key" in response.json()["detail"]
    assert readings.summary()["total"] == 0


# ---- analyze a photo on the server ----

PHOTO = b"\x89PNG fake image bytes"


def test_analyze_stores_a_camera_reading_from_the_models_free_percent(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = {}

    def fake(image, radius):
        seen.update(image=image, radius=radius)
        return 9.1

    monkeypatch.setattr(vision, "measure_free_percent", fake)
    response = client.post("/api/camera/analyze", content=PHOTO, params={"radius": 220})
    assert response.status_code == 201
    body = response.json()
    assert body["free_percent"] == 9.1 and body["cloud_fraction"] == 0.909 and body["radius_px"] == 220
    assert seen == {"image": PHOTO, "radius": 220}
    stored = client.get("/api/readings", params={"type": "cloud_fraction", "source": "camera"}).json()
    assert len(stored) == 1 and stored[0]["value"] == 0.909


def test_analyze_uses_the_default_radius(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vision, "measure_free_percent", lambda image, radius: 50.0 if radius == 150 else -1)
    assert client.post("/api/camera/analyze", content=PHOTO).json()["cloud_fraction"] == 0.5


def test_analyze_says_501_with_instructions_when_the_model_is_not_here(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(image, radius):
        raise vision.ModelUnavailable("model libraries are not installed on this server")

    monkeypatch.setattr(vision, "measure_free_percent", missing)
    response = client.post("/api/camera/analyze", content=PHOTO)
    assert response.status_code == 501
    assert "camera machine" in response.json()["detail"] and "/api/readings" in response.json()["detail"]
    assert readings.summary()["total"] == 0


def test_analyze_rejects_empty_oversized_and_unreadable_uploads(monkeypatch: pytest.MonkeyPatch) -> None:
    assert client.post("/api/camera/analyze", content=b"").status_code == 422
    monkeypatch.setattr("app.routes.CLOUD_MAX_IMAGE_BYTES", 10)
    assert client.post("/api/camera/analyze", content=PHOTO).status_code == 413
    streamed = (PHOTO[i:i + 4] for i in range(0, len(PHOTO), 4))  # chunked, no Content-Length: counted while reading
    assert client.post("/api/camera/analyze", content=streamed).status_code == 413
    monkeypatch.setattr("app.routes.CLOUD_MAX_IMAGE_BYTES", 10_000)

    def unreadable(image, radius):
        raise ValueError("the upload is not a readable image")

    monkeypatch.setattr(vision, "measure_free_percent", unreadable)
    assert client.post("/api/camera/analyze", content=PHOTO).status_code == 422
    assert client.post("/api/camera/analyze", content=PHOTO, params={"radius": 5}).status_code == 422


def test_analyze_needs_the_key_too(key_on, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vision, "measure_free_percent", lambda image, radius: 20.0)
    assert client.post("/api/camera/analyze", content=PHOTO).status_code == 401
    assert client.post("/api/camera/analyze", content=PHOTO, headers={"X-API-Key": "s3cret"}).status_code == 201


def test_analyze_reports_a_duplicate_second_with_409(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vision, "measure_free_percent", lambda image, radius: 20.0)

    def duplicate(_reading):
        raise readings.DuplicateReading("a reading with this source, type and timestamp already exists")

    monkeypatch.setattr(readings, "add_reading", duplicate)
    assert client.post("/api/camera/analyze", content=PHOTO).status_code == 409


# ---- app/vision.py itself, with the model faked ----

def fake_predictor(monkeypatch: pytest.MonkeyPatch, run):
    module = types.ModuleType("cloud_predictor")
    module.run = run
    monkeypatch.setitem(sys.modules, "cloud_predictor", module)


def test_vision_passes_the_photo_as_a_file_and_cleans_it_up(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = {}

    def run(path, radius, label):
        seen.update(path=path, radius=radius, bytes=open(path, "rb").read())
        return 41.5, "out.jpg"

    fake_predictor(monkeypatch, run)
    assert vision.measure_free_percent(PHOTO, 120) == 41.5
    assert seen["bytes"] == PHOTO and seen["radius"] == 120
    import os
    assert not os.path.exists(seen["path"])  # the temporary file is gone


def test_vision_turns_missing_checkpoint_into_model_unavailable_and_bad_images_into_value_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_weights(path, radius, label):
        raise FileNotFoundError("No checkpoint at /x/checkpoints/best.pth")

    fake_predictor(monkeypatch, no_weights)
    with pytest.raises(vision.ModelUnavailable, match="checkpoint"):
        vision.measure_free_percent(PHOTO)

    def bad_image(path, radius, label):
        raise FileNotFoundError(path)

    fake_predictor(monkeypatch, bad_image)
    with pytest.raises(ValueError, match="readable image"):
        vision.measure_free_percent(PHOTO)


def test_without_torch_the_real_route_answers_501() -> None:
    try:
        import torch  # noqa: F401
    except ImportError:
        response = client.post("/api/camera/analyze", content=PHOTO)
        assert response.status_code == 501 and "camera machine" in response.json()["detail"]
    else:
        pytest.skip("torch is installed here")
