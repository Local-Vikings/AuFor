"""scripts/pi_cloud_agent.py talking over real HTTP to the real app (the model itself is not needed)."""

import importlib.util
import json
import socket
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import uvicorn

from main import app

SPEC = importlib.util.spec_from_file_location("pi_cloud_agent", Path(__file__).resolve().parent.parent / "scripts" / "pi_cloud_agent.py")
agent = importlib.util.module_from_spec(SPEC)
sys.modules["pi_cloud_agent"] = agent
SPEC.loader.exec_module(agent)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Server:
    """The real FastAPI app on a real port, started and stopped on demand."""

    def __init__(self) -> None:
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.thread = None

    def start(self) -> None:
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="error"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.time() + 10
        while not self.server.started and time.time() < deadline:
            time.sleep(0.02)
        assert self.server.started

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


@pytest.fixture
def server():
    s = Server()
    s.start()
    yield s
    if s.thread.is_alive():
        s.stop()


def run_agent(url, tmp_path, *extra, key=None):
    argv = ["--server", url, "--buffer", str(tmp_path / "pending.jsonl"), "--once", *extra]
    if key:
        argv += ["--api-key", key]
    return agent.main(argv)


def stored(url, **params):
    import urllib.parse
    import urllib.request
    query = urllib.parse.urlencode({"type": "cloud_fraction", **params})
    return json.load(urllib.request.urlopen(f"{url}/api/readings?{query}"))


# ---- pure helpers ----

def test_free_percent_becomes_cloud_fraction() -> None:
    assert agent.free_to_cloud_fraction(9.1) == 0.909
    assert agent.free_to_cloud_fraction(0) == 1.0 and agent.free_to_cloud_fraction(100) == 0.0
    for bad in (-1, 100.5, float("nan")):
        with pytest.raises(ValueError):
            agent.free_to_cloud_fraction(bad)


def test_reading_is_a_camera_cloud_fraction_in_utc() -> None:
    local = datetime(2026, 10, 3, 14, 0, 0, 999, tzinfo=timezone(timedelta(hours=3)))
    assert agent.make_reading(0.909, local) == {"source": "camera", "type": "cloud_fraction", "value": 0.909, "timestamp": "2026-10-03T11:00:00+00:00"}


def test_arguments_need_exactly_one_photo_source() -> None:
    with pytest.raises(SystemExit):
        agent.parse_args(["--once"])
    with pytest.raises(SystemExit):
        agent.parse_args(["--image", "a.jpg", "--free-percent", "10"])
    args = agent.parse_args(["--free-percent", "9.1"])
    assert args.radius == 150 and args.interval == 120.0 and args.buffer == "pending_readings.jsonl"


# ---- the whole path: agent -> HTTP -> app -> SQLite ----

def test_a_reading_travels_from_the_agent_to_the_database(server, tmp_path) -> None:
    assert run_agent(server.url, tmp_path, "--free-percent", "9.1") == 0
    rows = stored(server.url, source="camera")
    assert len(rows) == 1 and rows[0]["value"] == 0.909 and rows[0]["source"] == "camera"
    assert not (tmp_path / "pending.jsonl").exists()  # nothing left to retry


def test_a_wrong_key_is_a_clear_error_and_nothing_is_stored_or_buffered(server, tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr("app.config.READINGS_API_KEY", "s3cret")
    assert run_agent(server.url, tmp_path, "--free-percent", "9.1", key="wrong") == agent.EXIT_AUTH
    assert "refused the API key" in capsys.readouterr().err
    assert not (tmp_path / "pending.jsonl").exists() and stored(server.url) == []
    assert run_agent(server.url, tmp_path, "--free-percent", "9.1", key="s3cret") == 0
    assert len(stored(server.url)) == 1


def test_a_dead_server_buffers_the_reading_and_the_next_round_delivers_it(tmp_path) -> None:
    buffer = tmp_path / "pending.jsonl"
    down = Server()  # never started: nothing listens on this port
    assert run_agent(down.url, tmp_path, "--free-percent", "80") == 0
    time.sleep(1.1)  # readings carry second-resolution timestamps; the agent runs minutes apart in real life
    assert run_agent(down.url, tmp_path, "--free-percent", "60") == 0
    assert len(buffer.read_text().splitlines()) == 2

    time.sleep(1.1)  # the readings carry second-resolution timestamps
    live = Server()
    live.port, live.url = down.port, down.url
    live.start()
    try:
        assert run_agent(live.url, tmp_path, "--free-percent", "40") == 0
        values = [row["value"] for row in stored(live.url)]
        assert values == [0.2, 0.4, 0.6]  # oldest buffered first, then the new one
        assert not buffer.exists()
    finally:
        live.stop()


def test_a_duplicate_counts_as_delivered(server, tmp_path) -> None:
    reading = agent.make_reading(0.5, datetime.now(timezone.utc) - timedelta(minutes=1))
    assert agent.post_reading(server.url, reading) == "sent"
    assert agent.post_reading(server.url, reading) == "duplicate"
    agent.buffer_reading(str(tmp_path / "b.jsonl"), reading)
    assert agent.flush_buffer(str(tmp_path / "b.jsonl"), server.url) == (1, 0)


def test_bad_data_is_rejected_not_retried_forever(server, tmp_path, capsys) -> None:
    future = agent.make_reading(0.5, datetime.now(timezone.utc) + timedelta(hours=2))  # a Pi with a wrong clock
    assert agent.post_reading(server.url, future) == "rejected"
    assert "future" in capsys.readouterr().err
    path = str(tmp_path / "b.jsonl")
    agent.buffer_reading(path, future)
    assert agent.flush_buffer(path, server.url) == (0, 0)  # dropped, buffer cleared


def test_server_errors_are_retried_later(monkeypatch, tmp_path) -> None:
    import urllib.error

    def boom(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 503, "unavailable", {}, None)

    monkeypatch.setattr(agent.urllib.request, "urlopen", boom)
    assert agent.post_reading("http://x", agent.make_reading(0.5)) == "retry"


# ---- buffer file ----

def test_flush_stops_at_the_first_failure_and_keeps_the_rest_in_order(tmp_path) -> None:
    path = str(tmp_path / "b.jsonl")
    for value in (0.1, 0.2, 0.3):
        agent.buffer_reading(path, agent.make_reading(value))
    assert agent.flush_buffer(path, "http://127.0.0.1:1") == (0, 3)  # nobody listens
    assert [json.loads(line)["value"] for line in Path(path).read_text().splitlines()] == [0.1, 0.2, 0.3]


def test_corrupt_buffer_lines_are_skipped_and_the_buffer_is_capped(tmp_path, server) -> None:
    path = tmp_path / "b.jsonl"
    path.write_text("this is not json\n" + json.dumps(agent.make_reading(0.7, datetime.now(timezone.utc) - timedelta(minutes=2))) + "\n")
    assert agent.flush_buffer(str(path), server.url) == (1, 0)
    agent.MAX_BUFFERED = 3
    try:
        for value in range(6):
            agent.buffer_reading(str(path), {"n": value})
        assert [json.loads(line)["n"] for line in path.read_text().splitlines()] == [3, 4, 5]
    finally:
        agent.MAX_BUFFERED = 5000


# ---- camera and model ----

def test_capture_runs_the_command_and_returns_the_photo(tmp_path) -> None:
    script = tmp_path / "cam.py"
    script.write_text("import sys; open(sys.argv[1], 'wb').write(b'JPEG-BYTES')")
    path = agent.capture(f"{sys.executable} {script} {{path}}")
    assert Path(path).read_bytes() == b"JPEG-BYTES"
    Path(path).unlink()


def test_an_empty_photo_is_an_error(tmp_path) -> None:
    with pytest.raises(RuntimeError, match="empty photo"):
        agent.capture(f"{sys.executable} -c pass")


def test_measure_explains_what_is_missing_when_the_model_cannot_load() -> None:
    try:
        import torch  # noqa: F401
    except ImportError:
        with pytest.raises(RuntimeError, match="cannot load the model"):
            agent.measure("x.jpg", 150)
    else:
        pytest.skip("torch is installed here")


def test_cycle_measures_with_the_model_and_cleans_up_the_temporary_photo(monkeypatch, server, tmp_path) -> None:
    script = tmp_path / "cam.py"
    script.write_text("import sys; open(sys.argv[1], 'wb').write(b'x')")
    seen = {}

    def fake_measure(path, radius):
        seen.update(path=path, radius=radius)
        return 25.0

    monkeypatch.setattr(agent, "measure", fake_measure)
    assert run_agent(server.url, tmp_path, "--capture-cmd", f"{sys.executable} {script} {{path}}", "--radius", "200") == 0
    assert seen["radius"] == 200 and not Path(seen["path"]).exists()
    assert stored(server.url)[0]["value"] == 0.75


def test_a_failing_camera_skips_the_round_instead_of_crashing(tmp_path, server, capsys) -> None:
    assert run_agent(server.url, tmp_path, "--capture-cmd", f"{sys.executable} -c pass") == 1
    assert "skipped this round" in capsys.readouterr().err and stored(server.url) == []


def test_dry_run_measures_but_sends_nothing(server, tmp_path, capsys) -> None:
    assert run_agent(server.url, tmp_path, "--free-percent", "9.1", "--dry-run") == 0
    assert "cloud_fraction 0.909" in capsys.readouterr().out and stored(server.url) == []
