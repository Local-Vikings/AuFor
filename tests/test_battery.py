"""app/battery.py: hand-calculated cases E-I and the invariants (bible 9.8, 19.3)."""

import numpy as np
import pytest

from app.battery import simulate

DEFAULT = dict(capacity_kwh=10.0, dod=0.9, eta_c=0.95, eta_d=0.95, max_power_kw=5.0)


def run(pv, load, soc, **overrides):
    config = {**DEFAULT, **overrides}
    return simulate(np.array(pv, float), np.array(load, float), initial_soc_kwh=soc, **config)


def test_case_e_charges_from_surplus() -> None:
    result = run([4], [1], 5.0)
    assert result.charge_kw[0] == pytest.approx(3.0)
    assert result.soc_kwh[0] == pytest.approx(7.85)
    assert result.export_kw[0] == pytest.approx(0.0)


def test_case_f_limited_by_capacity() -> None:
    result = run([4], [1], 7.85)
    assert result.charge_kw[0] == pytest.approx(2.2632, rel=1e-4)
    assert result.soc_kwh[0] == pytest.approx(10.0)
    assert result.export_kw[0] == pytest.approx(0.7368, rel=1e-4)


def test_case_g_limited_by_minimum_charge() -> None:
    result = run([0], [2], 1.5)
    assert result.discharge_kw[0] == pytest.approx(0.475)
    assert result.soc_kwh[0] == pytest.approx(1.0)
    assert result.import_kw[0] == pytest.approx(1.525)


def test_case_h_nothing_happens() -> None:
    result = run([0], [0], 10.0)
    assert result.soc_kwh[0] == 10.0 and result.charge_kw[0] == 0 and result.discharge_kw[0] == 0


def test_case_i_no_battery_exports_surplus_and_imports_deficit() -> None:
    result = run([3, 0], [1, 2], 0.0, capacity_kwh=0.0)
    np.testing.assert_allclose(result.export_kw, [2.0, 0.0])
    np.testing.assert_allclose(result.import_kw, [0.0, 2.0])
    assert (result.soc_kwh == 0).all()


def test_power_limit_applies_to_charge_and_discharge() -> None:
    assert run([20], [0], 2.0).charge_kw[0] == pytest.approx(5.0)
    assert run([0], [20], 9.0).discharge_kw[0] == pytest.approx(5.0)


def test_initial_soc_is_clamped_into_the_allowed_range() -> None:
    assert run([0], [0], 0.0).soc_kwh[0] == pytest.approx(1.0)
    assert run([0], [0], 99.0).soc_kwh[0] == pytest.approx(10.0)


def test_invariants_over_a_random_week() -> None:
    rng = np.random.default_rng(7)
    pv = rng.uniform(0, 6, 24 * 7)
    load = rng.uniform(0, 3, 24 * 7)
    result = run(pv, load, 5.0)
    assert (result.soc_kwh >= 1.0 - 1e-9).all() and (result.soc_kwh <= 10.0 + 1e-9).all()
    # terminal balance every hour: pv + discharge + import = load + charge + export
    balance = pv + result.discharge_kw + result.import_kw - load - result.charge_kw - result.export_kw
    np.testing.assert_allclose(balance, 0.0, atol=1e-9)
    # the stored energy changes exactly by what went in and out through the efficiencies
    delta = result.soc_kwh[-1] - 5.0
    expected = (result.charge_kw * 0.95).sum() - (result.discharge_kw / 0.95).sum()
    assert delta == pytest.approx(expected)
