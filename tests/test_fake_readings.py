"""scripts/fake_readings.py: SIMULATED readings with a fixed bias, so calibration can be shown (T52)."""

import importlib.util
import json
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.test_pi_agent import Server

SPEC = importlib.util.spec_from_file_location("fake_readings", Path(__file__).resolve().parent.parent / "scripts" / "fake_readings.py")
fake = importlib.util.module_from_spec(SPEC)
sys.modules["fake_readings"] = fake
SPEC.loader.exec_module(fake)

LONG_AGO = datetime(2030, 1, 1, tzinfo=timezone.utc)  # "now" far after the sample forecast


def forecast_rows(*rows, pr_used=0.8):
    return {"meta": {"pr_used": pr_used}, "hourly": [{"time": t, "p_ac_w": p} for t, p in rows]}


def build(forecast, noise=0.0, bias=0.92, step=10, now=LONG_AGO, request=fake.DEFAULT_REQUEST):
    return fake.build_readings(forecast, request, fake.DEFAULT_PR * bias, noise, step, now, random.Random(1))


@pytest.fixture
def server():
    s = Server()
    s.start()
    yield s
    s.stop()


# ---- how the readings are built ----

def test_readings_come_from_dc_power_not_from_the_calibrated_forecast() -> None:
    # the forecast already runs at PR 0.70, so its DC power is 1400 / 0.70 = 2000 W: the fake system makes 2000 * 0.736
    readings = build(forecast_rows(("2026-10-03T12:00:00+03:00", 1400.0), pr_used=0.70))
    assert {r["value"] for r in readings} == {1472.0}
    # the same hour forecast at PR 0.80 gives the same readings: the bias cannot compound
    assert {r["value"] for r in build(forecast_rows(("2026-10-03T12:00:00+03:00", 1600.0), pr_used=0.80))} == {1472.0}


def test_readings_are_timed_inside_the_hour_that_ends_at_the_row_label() -> None:
    readings = build(forecast_rows(("2026-10-03T12:00:00+03:00", 1600.0)), step=15)
    stamps = [datetime.fromisoformat(r["timestamp"]) for r in readings]
    assert [s.astimezone(timezone(timedelta(hours=3))).strftime("%H:%M") for s in stamps] == ["11:15", "11:30", "11:45", "12:00"]


def test_night_and_clipped_hours_are_skipped() -> None:
    readings = build(forecast_rows(("2026-10-03T03:00:00+03:00", 0.0), ("2026-10-03T12:00:00+03:00", 3950.0), ("2026-10-03T13:00:00+03:00", 1000.0)))
    assert {r["timestamp"][:13] for r in readings} <= {"2026-10-03T09", "2026-10-03T10"}  # only the 13:00 row (UTC 10:00) survives


def test_future_readings_are_not_generated() -> None:
    row = ("2026-10-03T12:00:00+03:00", 1600.0)
    noon_utc = datetime(2026, 10, 3, 9, 0, tzinfo=timezone.utc)
    readings = build(forecast_rows(row), now=noon_utc - timedelta(minutes=20), step=10)
    assert all(datetime.fromisoformat(r["timestamp"]) <= noon_utc - timedelta(minutes=20) for r in readings) and len(readings) == 4


def test_every_reading_is_simulated_power_and_never_negative() -> None:
    readings = build(forecast_rows(("2026-10-03T12:00:00+03:00", 1600.0)), noise=3.0)  # absurd noise
    assert all(r["source"] == "simulated" and r["type"] == "power_w" and r["value"] >= 0 for r in readings)


def test_noise_is_seeded_and_averages_out() -> None:
    rows = forecast_rows(*[(f"2026-10-03T{h:02d}:00:00+03:00", 1600.0) for h in range(8, 18)])
    a = fake.build_readings(rows, fake.DEFAULT_REQUEST, 0.736, 0.08, 1, LONG_AGO, random.Random(7))
    b = fake.build_readings(rows, fake.DEFAULT_REQUEST, 0.736, 0.08, 1, LONG_AGO, random.Random(7))
    assert a == b and len(a) == 600
    assert sum(r["value"] for r in a) / len(a) == pytest.approx(1472.0, rel=0.01)


def test_several_inverters_add_up_for_the_clipping_limit() -> None:
    request = {**fake.DEFAULT_REQUEST, "panel": None, "panels": [{"inverter_max_w": 1000}, {"inverter_max_w": 1000}]}
    assert fake.total_inverter_w(request) == 2000 and fake.total_inverter_w(fake.DEFAULT_REQUEST) == 4000


# ---- the whole path against the real app ----

def test_running_the_script_calibrates_the_forecast_toward_the_biased_pr(server, capsys) -> None:
    assert fake.main(["--server", server.url, "--step-minutes", "5", "--seed", "1"]) == 0
    first = json.load(__import__("urllib.request").request.urlopen(server.url + "/api/calibration"))
    out = capsys.readouterr().out
    assert "simulated readings" in out and "calibrated from" in out
    assert len(first) == 1 and first[0]["source"] == "simulated" and 0.74 < first[0]["pr"] < 0.79


def test_running_it_twice_does_not_move_the_pr_without_new_readings(server, capsys) -> None:
    fake.main(["--server", server.url, "--step-minutes", "10", "--seed", "1"])
    capsys.readouterr()
    fake.main(["--server", server.url, "--step-minutes", "10", "--seed", "1"])
    assert "0 new simulated readings" in capsys.readouterr().out
    states = json.load(__import__("urllib.request").request.urlopen(server.url + "/api/calibration"))
    assert states[0]["points"] > 0
    pr = states[0]["pr"]
    fake.main(["--server", server.url, "--step-minutes", "10", "--seed", "1"])
    assert json.load(__import__("urllib.request").request.urlopen(server.url + "/api/calibration"))[0]["pr"] == pr


def test_more_readings_move_the_pr_closer_and_reset_starts_again(server, capsys) -> None:
    get = lambda: json.load(__import__("urllib.request").request.urlopen(server.url + "/api/calibration"))[0]["pr"]
    fake.main(["--server", server.url, "--step-minutes", "30", "--seed", "1"])
    first = get()
    fake.main(["--server", server.url, "--step-minutes", "10", "--seed", "2"])
    second = get()
    assert 0.736 < second < first < 0.80
    fake.main(["--server", server.url, "--reset", "--step-minutes", "60", "--seed", "3"])
    assert "calibration reset (1 removed)" in capsys.readouterr().out


def test_wrong_key_and_missing_server_have_clear_exit_codes(server, monkeypatch, capsys) -> None:
    monkeypatch.setattr("app.config.READINGS_API_KEY", "s3cret")
    assert fake.main(["--server", server.url, "--step-minutes", "60"]) == fake.EXIT_AUTH  # no key at all
    assert fake.main(["--server", server.url, "--api-key", "wrong", "--step-minutes", "60", "--reset"]) == fake.EXIT_AUTH
    assert "refused the API key" in capsys.readouterr().err
    assert fake.main(["--server", "http://127.0.0.1:1", "--step-minutes", "60"]) == 1
    assert "cannot reach" in capsys.readouterr().err
