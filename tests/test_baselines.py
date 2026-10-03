"""app/baselines.py and scripts/validate.py: clear-sky bound, persistence, metrics, validation report (bible 8, T42)."""

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pvlib
import pytest

from app import baselines, pipeline, solar
from tests.fixtures.mock_data import make_request

SPEC = importlib.util.spec_from_file_location("validate", Path(__file__).resolve().parent.parent / "scripts" / "validate.py")
validate = importlib.util.module_from_spec(SPEC)
sys.modules["validate"] = validate
SPEC.loader.exec_module(validate)


def synthetic_weather(request, factor, days=3, start="2026-06-20"):
    """Physically consistent weather: a typical-air clear sky (Linke turbidity 3.1) scaled by ``factor``."""
    index = pd.date_range(f"{start} 00:00", periods=24 * days, freq="h", tz="Europe/Sofia")
    instants = (index - pd.Timedelta(minutes=30)).tz_convert("UTC")
    clear = pvlib.location.Location(request.lat, request.lon).get_clearsky(instants, model="ineichen", linke_turbidity=3.1)
    frame = pd.DataFrame({"ghi": clear["ghi"].to_numpy() * factor, "dni": clear["dni"].to_numpy() * factor,
                          "dhi": clear["dhi"].to_numpy() * factor, "temp_air": 22.0, "cloud_cover": 20.0, "wind": 2.0}, index=index)
    frame.attrs["sources"] = ["synthetic"]
    return frame


# ---- clear-sky bound ----

def test_the_bound_is_zero_at_night_peaks_at_midday_and_grows_with_the_array() -> None:
    request = make_request("default")
    weather = synthetic_weather(request, 1.0)
    bound = baselines.clear_sky(request, weather)
    assert isinstance(bound, np.ndarray) and bound.shape == (72,)
    assert (bound[:4] == 0).all() and 11 <= weather.index[int(bound[:24].argmax())].hour <= 14
    small = baselines.clear_sky(make_request("small"), weather)
    assert 0 < small.max() < bound.max()


@pytest.mark.parametrize("factor", [0.3, 0.6, 1.0])
def test_a_physically_consistent_forecast_stays_under_the_bound(factor) -> None:
    request = make_request("default")
    weather = synthetic_weather(request, factor)
    forecast = pipeline.build_forecast(request, weather).hourly["p_ac_w"].to_numpy()
    assert baselines.over(forecast, baselines.clear_sky(request, weather)) == 0


def test_an_absurdly_bright_forecast_is_caught() -> None:
    request = make_request("default")
    weather = synthetic_weather(request, 2.2)
    forecast = pipeline.build_forecast(request, weather).hourly["p_ac_w"].to_numpy()
    bound = baselines.clear_sky(request, weather)
    flagged = baselines.exceedances(forecast, bound)
    assert flagged and baselines.over(forecast, bound) == len(flagged)
    assert all(bound[i] > 0 for i in flagged)  # daylight hours, not the night


def test_exceedances_use_a_5_percent_tolerance_and_a_small_slack_at_the_edges() -> None:
    bound = np.array([0.0, 100.0, 1000.0, 4000.0, 0.0])
    assert baselines.exceedances([0, 100, 1000, 4000, 0], bound) == []
    assert baselines.exceedances([0, 100, 1049, 4200, 0], bound) == []  # 4.9% and exactly 5%
    assert baselines.exceedances([0, 100, 1100, 4400, 0], bound) == [3]  # 1100 is inside 1000*1.05 + 80 W slack
    assert baselines.exceedances([0, 100, 1200, 4400, 0], bound) == [2, 3]
    assert baselines.exceedances([60, 100, 1000, 4000, 0], bound) == []  # 60 W at the horizon: inside the 2% slack
    assert baselines.exceedances([200, 100, 1000, 4000, 0], bound) == [0]
    assert baselines.over([], []) == 0


def test_the_bound_uses_clean_air_so_it_is_higher_than_the_typical_sky() -> None:
    request = make_request("default")
    weather = synthetic_weather(request, 1.0)
    typical = baselines.clear_sky_weather(request, weather, linke_turbidity=3.1)["ghi"].max()
    assert baselines.clear_sky_weather(request, weather)["ghi"].max() > typical


# ---- persistence and metrics ----

def test_persistence_is_the_same_hour_a_day_earlier() -> None:
    assert baselines.persistence(list(range(30))).tolist() == [0, 1, 2, 3, 4, 5]
    assert baselines.persistence([1, 2, 3, 4], lag=2).tolist() == [1, 2]


def test_metrics_hand_example() -> None:
    rmse, mae, skill = solar.metrics(np.array([2.0, 4.0]), np.array([1.0, 5.0]), np.array([0.0, 0.0]))
    assert (rmse, mae) == (pytest.approx(1.0), pytest.approx(1.0))
    assert skill == pytest.approx(1 - 1 / 13**0.5)  # persistence errors 1 and 5, RMSE sqrt(13)


def test_evaluate_uses_persistence_as_the_reference() -> None:
    rmse, mae, skill = baselines.evaluate([0, 2, 4], [0, 1, 5], lag=1)
    assert rmse == pytest.approx(1.0) and mae == pytest.approx(1.0) and skill == pytest.approx(1 - 1 / 8.5**0.5)


def test_skill_is_one_for_a_perfect_forecast_and_zero_when_it_equals_persistence() -> None:
    obs = np.random.default_rng(3).uniform(0, 1000, 120)
    assert baselines.evaluate(obs, obs)[2] == pytest.approx(1.0)
    copy_of_yesterday = np.concatenate([obs[:24], obs[:-24]])
    assert baselines.evaluate(copy_of_yesterday, obs)[2] == pytest.approx(0.0, abs=1e-9)
    assert baselines.evaluate(obs * 0.0, obs)[2] < 0  # a forecast of zero is worse than persistence


def test_a_perfect_persistence_reference_gives_skill_zero_not_nan() -> None:
    assert solar.metrics(np.array([1.0, 2.0]), np.array([1.0, 3.0]), np.array([1.0, 3.0]))[2] == 0.0


def test_metrics_reject_bad_input_with_a_clear_error() -> None:
    with pytest.raises(ValueError):
        baselines.evaluate([1, 2, 3], [1, 2], lag=1)
    with pytest.raises(ValueError, match="more than 24"):
        baselines.evaluate(range(24), range(24))
    with pytest.raises(ValueError):
        solar.metrics(np.array([]), np.array([]), np.array([]))


def test_python_and_native_metrics_agree_and_the_python_one_works_alone() -> None:
    rng = np.random.default_rng(9)
    pred, obs, ref = (rng.uniform(0, 1000, 200) for _ in range(3))
    python = solar.metrics(pred, obs, ref, engine="python")
    assert python == pytest.approx(solar.metrics(pred, obs, ref))
    if solar.ENGINE == "native" and hasattr(solar._LIB, "ss_metrics"):
        assert python == pytest.approx(solar.metrics(pred, obs, ref, engine="native"), rel=1e-9)


# ---- measurements ----

def hourly_table(days=4):
    index = pd.date_range("2026-10-03 00:00", periods=24 * days, freq="h", tz="Europe/Sofia")
    shape = np.clip(np.sin((index.hour - 6) / 12 * np.pi), 0, None) * 2500
    return pd.DataFrame({"p_ac_w": shape}, index=index)


def readings_for(hourly, factor, source="panel", minutes=(50, 30, 10)):
    rows = []
    for stamp, power in hourly["p_ac_w"].items():
        if power > 0:
            for m in minutes:
                rows.append({"source": source, "type": "power_w", "value": power * factor, "timestamp": (stamp - pd.Timedelta(minutes=m)).to_pydatetime().astimezone(timezone.utc)})
    return rows


def test_only_real_readings_are_measurements() -> None:
    hourly = hourly_table()
    mixed = readings_for(hourly, 0.9, "panel") + readings_for(hourly, 5.0, "simulated")
    measured = baselines.measured_hourly(hourly, mixed)
    sunny = hourly["p_ac_w"] > 0
    assert measured[sunny].round(3).eq((hourly["p_ac_w"] * 0.9)[sunny].round(3)).all() and measured[~sunny].isna().all()
    assert baselines.measured_hourly(hourly, readings_for(hourly, 5.0, "simulated")).isna().all()


def test_accuracy_needs_enough_aligned_hours_and_reports_skill_against_persistence() -> None:
    hourly = hourly_table(4)
    result = baselines.accuracy(hourly["p_ac_w"], baselines.measured_hourly(hourly, readings_for(hourly, 0.9)))
    assert result["points"] >= 24 and result["rmse_w"] > 0 and result["mae_w"] > 0 and "skill" in result
    assert baselines.accuracy(hourly["p_ac_w"], baselines.measured_hourly(hourly_table(1), readings_for(hourly_table(1), 0.9))) is None


# ---- validate.py ----

def test_percent_difference_and_monthly_comparison() -> None:
    assert validate.percent_difference(110, 100) == pytest.approx(10.0)
    rows = validate.compare_months([100.0] * 12, [80.0] * 12)
    assert rows[0] == {"month": "Jan", "model_kwh": 100.0, "pvgis_kwh": 80.0, "diff_pct": pytest.approx(25.0)} and len(rows) == 12


def test_monthly_energy_sums_hourly_watts_into_kwh_per_month() -> None:
    index = pd.date_range("2025-01-01", "2025-12-31 23:00", freq="h", tz="Europe/Sofia")
    months = validate.monthly_energy(pd.DataFrame({"p_ac_w": 1000.0}, index=index))
    assert months[0] == pytest.approx(31 * 24) and months[1] == pytest.approx(28 * 24) and sum(months) == pytest.approx(365 * 24)


def test_accuracy_check_explains_why_there_is_none() -> None:
    nothing = validate.check_measured_accuracy(pd.DataFrame(), [])
    assert not nothing["available"] and "no power readings" in nothing["reason"] and "no accuracy is claimed" in nothing["reason"]
    fake = [{"source": "simulated", "type": "power_w", "value": 1.0, "timestamp": datetime.now(timezone.utc)}] * 30
    simulated = validate.check_measured_accuracy(pd.DataFrame(), fake)
    assert not simulated["available"] and "not a measurement" in simulated["reason"]


def test_clear_sky_check_on_consistent_weather() -> None:
    request = make_request("default")
    ok = validate.check_clear_sky_bound(request, synthetic_weather(request, 0.7))
    assert ok["passed"] and ok["over"] == 0 and ok["max_ratio"] < 1.0 and ok["producing_hours"] > 20
    assert not validate.check_clear_sky_bound(request, synthetic_weather(request, 2.2))["passed"]


def test_report_sections_show_the_numbers_and_the_honest_gaps() -> None:
    assert "PASS" in validate.section_clear_sky({"hours": 384, "producing_hours": 190, "over": 0, "max_ratio": 0.96, "passed": True})
    assert "FAIL" in validate.section_clear_sky({"hours": 10, "producing_hours": 5, "over": 3, "max_ratio": 1.3, "passed": False})
    assert "Skipped" in validate.section_clear_sky("offline mode")
    rows = validate.compare_months([100.0] * 11 + [200.0], [100.0] * 12)
    text = validate.section_pvgis(2025, rows, 1300.0, 1200.0, 4.0, 14.0)
    assert "+8.3%" in text and "PASS" in text and "Dec (+100%)" in text and "individual months can differ" in text
    assert "FAIL" in validate.section_pvgis(2025, rows, 1500.0, 1000.0, 4.0, 14.0)
    assert "inside" in validate.section_yield(5363, 4969, 4.0) and "OUTSIDE" in validate.section_yield(4000, 4969, 4.0)
    no_data = validate.section_accuracy({"available": False, "reason": "no power readings are stored; no accuracy is claimed"})
    assert "Not available" in no_data and "no accuracy is claimed" in no_data


def test_offline_run_writes_an_honest_report_and_succeeds(tmp_path, capsys) -> None:
    output = tmp_path / "report.md"
    assert validate.main(["--offline", "--output", str(output)]) == 0
    text = output.read_text()
    assert "# Validation report" in text and "Skipped" in text and "no accuracy is claimed" in text
    assert capsys.readouterr().out.startswith("# Validation report")


def test_a_full_run_with_the_network_faked(monkeypatch, tmp_path) -> None:
    request = make_request("default")
    archive = synthetic_weather(request, 0.8, days=6, start="2025-06-20")
    model_total = sum(validate.monthly_energy(pipeline.build_forecast(request, archive).hourly))
    monkeypatch.setattr(validate, "fetch_weather", lambda lat, lon, days: synthetic_weather(request, 0.8, days=16))
    monkeypatch.setattr(validate, "fetch_archive_weather", lambda *a: archive)
    monkeypatch.setattr(validate, "fetch_pvgis", lambda *a: {"monthly": [model_total / 12] * 12, "annual": model_total / 1.10, "kwp": 4.0})
    out = tmp_path / "r.md"
    assert validate.main(["--year", "2025", "--output", str(out)]) == 0  # +10% against PVGIS is inside the 20% target
    text = out.read_text()
    assert "## 1. Clear-sky bound: PASS" in text and "## 3. PVGIS cross-check: PASS" in text and "+10.0%" in text
    assert "## 4. Yearly specific yield" in text and "no accuracy is claimed" in text

    monkeypatch.setattr(validate, "fetch_pvgis", lambda *a: {"monthly": [model_total / 12] * 12, "annual": model_total / 1.40, "kwp": 4.0})
    assert validate.main(["--year", "2025"]) == 1  # +40%: the run fails


def test_network_trouble_is_reported_not_raised(monkeypatch, capsys) -> None:
    def down(*args, **kwargs):
        raise ConnectionError("no internet")

    monkeypatch.setattr(validate, "fetch_weather", down)
    monkeypatch.setattr(validate, "fetch_archive_weather", down)
    assert validate.main([]) == 0
    out = capsys.readouterr().out
    assert "could not get the forecast" in out and "Could not run: ConnectionError" in out
