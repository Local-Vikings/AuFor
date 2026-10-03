from datetime import datetime, timezone

import pytest

from app import advisor
from app.models import PanelConfig

SOFIA = (42.6977, 23.3219)
HOUR = datetime(2026, 10, 3, 7, 0, tzinfo=timezone.utc)

if advisor.best_orientation(*SOFIA, 2026, 0.0) is None:
    pytest.skip("native lib not built", allow_module_level=True)


def panel(tilt, az, count=10):
    return PanelConfig(
        count=count,
        watt_peak=400,
        tilt=tilt,
        azimuth=az,
        noct=45,
        gamma=-0.004,
        inverter_max_w=4000,
    )


def test_north_triggers_optimize():
    res = advisor.optimize_rule(*SOFIA, [panel(35, 0)], HOUR)
    assert len(res) == 1
    assert res[0].subtopic == "optimize"
    assert res[0].kwh_effect > 0
    assert "azimuth 180" in res[0].title


def test_south_default_no_card():
    assert advisor.optimize_rule(*SOFIA, [panel(35, 180)], HOUR) == []


def test_flat_triggers_optimize():
    assert len(advisor.optimize_rule(*SOFIA, [panel(0, 180)], HOUR)) == 1


def test_only_bad_group():
    res = advisor.optimize_rule(*SOFIA, [panel(35, 180), panel(35, 0, count=4)], HOUR)
    assert len(res) == 1
    assert "azimuth 0" in res[0].reason


def test_no_lib_no_crash(monkeypatch):
    monkeypatch.setattr(advisor, "best_orientation", lambda *a: None)
    assert advisor.optimize_rule(*SOFIA, [panel(35, 0)], HOUR) == []


def test_south_beats_north():
    assert advisor.annual_poa(*SOFIA, 2026, 35, 180, 0) > advisor.annual_poa(*SOFIA, 2026, 35, 0, 0)


def test_best_orientation_sofia():
    tilt, az, kwh = advisor.best_orientation(*SOFIA, 2026, 0)
    assert az == pytest.approx(180, abs=10)
    assert 25 <= tilt <= 45


def test_bad_year_raises():
    with pytest.raises(ValueError):
        advisor.annual_poa(*SOFIA, 1900, 35, 180, 0)
