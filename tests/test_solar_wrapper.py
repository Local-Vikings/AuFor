"""app/solar.py: ctypes wrapper, Python fallback, and native/Python parity (bible 9.0, 19.3)."""

import subprocess
import sys

import numpy as np
import pytest

from app import solar

ENGINES = ["python"] + (["native"] if solar.ENGINE == "native" else [])
SOFIA = (42.6977, 23.3219)


@pytest.mark.parametrize("engine", ENGINES)
@pytest.mark.parametrize(
    "poa, t_air, inverter_max, expected_tcell, expected_dc, expected_ac",
    [
        (800.0, 25.0, 4000.0, 50.0, 2880.0, 2304.0),  # case A
        (1000.0, 20.0, 4000.0, 51.25, 3580.0, 2864.0),  # case B
        (0.0, 20.0, 4000.0, 20.0, 0.0, 0.0),  # case C
        (1000.0, 20.0, 2000.0, 51.25, 3580.0, 2000.0),  # case D, clipped
    ],
)
def test_hand_calculated_cases(
    engine, poa, t_air, inverter_max, expected_tcell, expected_dc, expected_ac
) -> None:
    g, air = np.array([poa]), np.array([t_air])
    t_cell = solar.cell_temperature(air, g, 45.0, engine=engine)
    p_dc = solar.dc_power(g, t_cell, 4000.0, -0.004, engine=engine)
    p_ac = solar.ac_power(p_dc, 0.8, inverter_max, engine=engine)
    assert t_cell[0] == pytest.approx(expected_tcell, rel=1e-6, abs=1e-9)
    assert p_dc[0] == pytest.approx(expected_dc, rel=1e-6, abs=1e-9)
    assert p_ac[0] == pytest.approx(expected_ac, rel=1e-6, abs=1e-9)


@pytest.mark.skipif(solar.ENGINE != "native", reason="native library not built")
def test_native_and_python_agree_on_a_full_chain() -> None:
    t = np.arange(1.79e9, 1.79e9 + 86400 * 10, 3600.0)
    outputs = {}
    for engine in ("native", "python"):
        zenith, azimuth = solar.sun_position(t, *SOFIA, engine=engine)
        ghi, dni, dhi = solar.irradiance_from_clouds(
            zenith, np.full(len(t), 40.0), t, engine=engine
        )
        poa = solar.poa_irradiance(zenith, azimuth, ghi, dni, dhi, 35, 180, 0.2, engine=engine)
        tc = solar.cell_temperature(np.full(len(t), 15.0), poa, 45.0, engine=engine)
        dc = solar.dc_power(poa, tc, 4000.0, -0.004, engine=engine)
        outputs[engine] = (zenith, azimuth, poa, solar.ac_power(dc, 0.8, 3000.0, engine=engine))
    for native_values, python_values in zip(outputs["native"], outputs["python"], strict=True):
        np.testing.assert_allclose(native_values, python_values, atol=1e-6)


@pytest.mark.skipif(solar.ENGINE != "native", reason="native library not built")
def test_native_rejects_bad_parameters() -> None:
    with pytest.raises(solar.SolarError):
        solar.sun_position(np.array([1.79e9]), 95.0, 0.0, engine="native")


def test_missing_library_falls_back_to_python() -> None:
    code = "from app import solar; print(solar.ENGINE)"
    result = subprocess.run(
        [sys.executable, "-c", code],
        env={"SOLAR_LIB_PATH": "/nonexistent/libsolarsight.so", "PATH": ""},
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "python"
