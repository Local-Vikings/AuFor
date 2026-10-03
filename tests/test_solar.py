"""Tests for the C++ solar core (T12-T14), called through ctypes.

pvlib is the oracle for geometry and the Erbs decomposition; 19.3 cases A-D are
hand-calculated. The library is built with native/build.sh if it is missing.
"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pvlib
import pytest

NATIVE_DIR = Path(__file__).resolve().parent.parent / "native"
LIB_EXT = {"win32": "dll", "darwin": "dylib"}.get(sys.platform, "so")
LIB_PATH = Path(os.environ.get("SOLAR_LIB_PATH") or NATIVE_DIR / f"libsolarsight.{LIB_EXT}")
SS_OK = 0
SS_ERR_BAD_PARAMETER = 3
SOFIA = (42.6977, 23.3219)


def _load_lib() -> ctypes.CDLL:
    if not LIB_PATH.exists():
        if shutil.which("g++") is None:
            pytest.skip("native library missing and g++ not available")
        subprocess.run(["bash", str(NATIVE_DIR / "build.sh")], check=True)
    lib = ctypes.CDLL(str(LIB_PATH))
    dbl = np.ctypeslib.ndpointer(dtype=np.float64, flags="C_CONTIGUOUS")
    c_int, c_dbl = ctypes.c_int, ctypes.c_double
    signatures = {
        "ss_sun_position": [dbl, c_int, c_dbl, c_dbl, dbl, dbl],
        "ss_angle_of_incidence": [dbl, dbl, c_int, c_dbl, c_dbl, dbl],
        "ss_poa_irradiance": [dbl, dbl, dbl, dbl, dbl, c_int, c_dbl, c_dbl, c_dbl, dbl],
        "ss_cell_temperature": [dbl, dbl, c_int, c_dbl, dbl],
        "ss_dc_power": [dbl, dbl, c_int, c_dbl, c_dbl, dbl],
        "ss_ac_power": [dbl, c_int, c_dbl, c_dbl, dbl],
        "ss_energy_kwh": [dbl, c_int, c_dbl, ctypes.POINTER(c_dbl)],
        "ss_clearsky_haurwitz": [dbl, c_int, dbl],
        "ss_kasten_czeplak": [dbl, dbl, c_int, dbl],
        "ss_erbs": [dbl, dbl, dbl, c_int, dbl, dbl],
        "ss_irradiance_from_clouds": [dbl, dbl, dbl, c_int, dbl, dbl, dbl],
    }
    for name, argtypes in signatures.items():
        func = getattr(lib, name)
        func.argtypes = argtypes
        func.restype = c_int
    return lib


@pytest.fixture(scope="module")
def lib() -> ctypes.CDLL:
    return _load_lib()


def _arr(values: object) -> np.ndarray:
    return np.ascontiguousarray(values, dtype=np.float64)


def _sun(lib: ctypes.CDLL, times: pd.DatetimeIndex) -> tuple[np.ndarray, np.ndarray]:
    t_utc = _arr(times.tz_convert("UTC").view("int64") / 1e9)
    zenith, azimuth = np.empty(len(t_utc)), np.empty(len(t_utc))
    assert lib.ss_sun_position(t_utc, len(t_utc), *SOFIA, zenith, azimuth) == SS_OK
    return zenith, azimuth


def _poa(
    lib: ctypes.CDLL,
    times: pd.DatetimeIndex,
    ghi: float,
    dni: float,
    dhi: float,
    tilt: float,
    azimuth: float,
) -> np.ndarray:
    zen, azi = _sun(lib, times)
    n = len(times)
    out = np.empty(n)
    status = lib.ss_poa_irradiance(
        zen, azi, _arr([ghi] * n), _arr([dni] * n), _arr([dhi] * n), n, tilt, azimuth, 0.2, out
    )
    assert status == SS_OK
    return out


def _pv_chain(
    lib: ctypes.CDLL, g_poa: float, t_air: float, inverter_max_w: float = 4000.0
) -> tuple[float, float, float]:
    t_cell, p_dc, p_ac = np.empty(1), np.empty(1), np.empty(1)
    assert lib.ss_cell_temperature(_arr([t_air]), _arr([g_poa]), 1, 45.0, t_cell) == SS_OK
    assert lib.ss_dc_power(_arr([g_poa]), t_cell, 1, 4000.0, -0.004, p_dc) == SS_OK
    assert lib.ss_ac_power(p_dc, 1, 0.80, inverter_max_w, p_ac) == SS_OK
    return t_cell[0], p_dc[0], p_ac[0]


# ---------- T12: sun position and POA ----------


def test_sun_position_matches_pvlib_analytical(lib: ctypes.CDLL) -> None:
    times = pd.date_range("2026-01-01", "2026-12-31 23:00", freq="7h", tz="UTC")
    zenith, azimuth = _sun(lib, times)
    doy = times.dayofyear.to_numpy()
    dec = pvlib.solarposition.declination_spencer71(doy)
    eot = pvlib.solarposition.equation_of_time_spencer71(doy)
    ha = pvlib.solarposition.hour_angle(times, SOFIA[1], eot)
    lat = np.radians(SOFIA[0])
    zen_ref = pvlib.solarposition.solar_zenith_analytical(lat, np.radians(ha), dec)
    azi_ref = pvlib.solarposition.solar_azimuth_analytical(lat, np.radians(ha), dec, zen_ref)
    np.testing.assert_allclose(zenith, np.degrees(zen_ref), atol=1e-6)
    np.testing.assert_allclose(azimuth, np.degrees(azi_ref), atol=1e-6)


def test_sun_position_close_to_spa(lib: ctypes.CDLL) -> None:
    times = pd.date_range("2026-10-03 06:00", "2026-10-03 18:00", freq="1h", tz="Europe/Sofia")
    zenith, azimuth = _sun(lib, times)
    spa = pvlib.solarposition.get_solarposition(times, *SOFIA)
    np.testing.assert_allclose(zenith, spa["zenith"].to_numpy(), atol=1.0)
    np.testing.assert_allclose(azimuth, spa["azimuth"].to_numpy(), atol=1.5)


def test_azimuth_convention_180_is_south(lib: ctypes.CDLL) -> None:
    times = pd.DatetimeIndex(["2026-06-21 07:00", "2026-06-21 10:27", "2026-06-21 14:00"], tz="UTC")
    _, azimuth = _sun(lib, times)
    assert azimuth[0] < 180.0 < azimuth[2]
    assert azimuth[1] == pytest.approx(180.0, abs=2.0)


def test_poa_matches_pvlib_isotropic(lib: ctypes.CDLL) -> None:
    times = pd.date_range("2026-10-03 07:00", "2026-10-03 16:00", freq="1h", tz="UTC")
    zen, azi = _sun(lib, times)
    poa = _poa(lib, times, 600.0, 800.0, 90.0, 35.0, 180.0)
    ref = pvlib.irradiance.get_total_irradiance(
        35.0, 180.0, zen, azi, 800.0, 600.0, 90.0, albedo=0.2, model="isotropic"
    )
    np.testing.assert_allclose(poa, ref["poa_global"], rtol=1e-9)


def test_night_poa_is_zero(lib: ctypes.CDLL) -> None:
    night = pd.DatetimeIndex(["2026-10-03 00:00"], tz="Europe/Sofia")
    assert _poa(lib, night, 0.0, 0.0, 0.0, 35.0, 180.0)[0] == 0.0


def test_no_beam_when_sun_below_horizon(lib: ctypes.CDLL) -> None:
    night = pd.DatetimeIndex(["2026-10-03 00:00"], tz="Europe/Sofia")
    assert _poa(lib, night, 0.0, 500.0, 0.0, 35.0, 0.0)[0] == 0.0


def test_south_facing_noon_beats_north_facing(lib: ctypes.CDLL) -> None:
    noon = pd.DatetimeIndex(["2026-10-03 13:00"], tz="Europe/Sofia")
    south = _poa(lib, noon, 610.0, 840.0, 88.0, 35.0, 180.0)[0]
    north = _poa(lib, noon, 610.0, 840.0, 88.0, 35.0, 0.0)[0]
    assert south > north > 0


def test_bad_panel_parameters_rejected(lib: ctypes.CDLL) -> None:
    one = _arr([10.0])
    out = np.empty(1)
    assert lib.ss_poa_irradiance(one, one, one, one, one, 1, 120.0, 180.0, 0.2, out) == 3
    assert lib.ss_sun_position(one, 1, 95.0, 0.0, out, np.empty(1)) == SS_ERR_BAD_PARAMETER


# ---------- T13: cell temperature, DC, AC (bible 19.3 cases A-D) ----------


@pytest.mark.parametrize(
    ("g_poa", "t_air", "inverter_max_w", "t_cell", "p_dc", "p_ac"),
    [
        (800.0, 25.0, 4000.0, 50.0, 2880.0, 2304.0),
        (1000.0, 20.0, 4000.0, 51.25, 3580.0, 2864.0),
        (0.0, 20.0, 4000.0, 20.0, 0.0, 0.0),
        (1000.0, 20.0, 2000.0, 51.25, 3580.0, 2000.0),
    ],
    ids=["A", "B", "C", "D-clipped"],
)
def test_hand_calculated_cases(
    lib: ctypes.CDLL,
    g_poa: float,
    t_air: float,
    inverter_max_w: float,
    t_cell: float,
    p_dc: float,
    p_ac: float,
) -> None:
    got = _pv_chain(lib, g_poa, t_air, inverter_max_w)
    assert got == pytest.approx((t_cell, p_dc, p_ac), rel=1e-6, abs=1e-9)


def test_hotter_cell_gives_lower_power(lib: ctypes.CDLL) -> None:
    assert _pv_chain(lib, 800.0, 35.0)[1] < _pv_chain(lib, 800.0, 10.0)[1]


def test_energy_kwh(lib: ctypes.CDLL) -> None:
    out = ctypes.c_double()
    assert lib.ss_energy_kwh(_arr([1000.0, 2000.0, 500.0]), 3, 1.0, ctypes.byref(out)) == SS_OK
    assert out.value == pytest.approx(3.5)


# ---------- T14: clear-sky and Kasten-Czeplak / Erbs fallback ----------


def test_haurwitz_matches_pvlib(lib: ctypes.CDLL) -> None:
    zenith = _arr(np.linspace(0, 100, 51))
    out = np.empty(len(zenith))
    assert lib.ss_clearsky_haurwitz(zenith, len(zenith), out) == SS_OK
    ref = pvlib.clearsky.haurwitz(pd.Series(zenith))["ghi"].to_numpy()
    np.testing.assert_allclose(out, ref, rtol=1e-9, atol=1e-9)
    assert out[-1] == 0.0


def test_kasten_czeplak_bounds(lib: ctypes.CDLL) -> None:
    out = np.empty(3)
    assert lib.ss_kasten_czeplak(_arr([800.0] * 3), _arr([0.0, 50.0, 100.0]), 3, out) == SS_OK
    assert out[0] == pytest.approx(800.0)
    assert out[1] == pytest.approx(800.0 * (1 - 0.75 * 0.5**3.4))
    assert out[2] == pytest.approx(200.0)


def test_erbs_matches_pvlib(lib: ctypes.CDLL) -> None:
    times = pd.date_range("2026-10-03 05:00", "2026-10-03 17:00", freq="1h", tz="UTC")
    zen, _ = _sun(lib, times)
    ghi = _arr(np.linspace(0, 700, len(times)))
    t_utc = _arr(times.view("int64") / 1e9)
    dni, dhi = np.empty(len(times)), np.empty(len(times))
    assert lib.ss_erbs(ghi, zen, t_utc, len(times), dni, dhi) == SS_OK
    ref = pvlib.irradiance.erbs(ghi, zen, times.dayofyear.to_numpy())
    np.testing.assert_allclose(dni, ref["dni"], rtol=1e-9, atol=1e-9)
    np.testing.assert_allclose(dhi, ref["dhi"], rtol=1e-9, atol=1e-9)


def test_missing_irradiance_still_gives_forecast(lib: ctypes.CDLL) -> None:
    times = pd.date_range("2026-10-03 00:00", periods=24, freq="1h", tz="Europe/Sofia")
    zen, azi = _sun(lib, times)
    n = len(times)
    t_utc = _arr(times.tz_convert("UTC").view("int64") / 1e9)
    ghi, dni, dhi, poa = np.empty(n), np.empty(n), np.empty(n), np.empty(n)
    status = lib.ss_irradiance_from_clouds(zen, _arr([60.0] * n), t_utc, n, ghi, dni, dhi)
    assert status == SS_OK
    assert lib.ss_poa_irradiance(zen, azi, ghi, dni, dhi, n, 35.0, 180.0, 0.2, poa) == SS_OK
    assert poa[13] > 100.0
    assert poa[0] == 0.0
    np.testing.assert_allclose(
        dhi + dni * np.clip(np.cos(np.radians(zen)), 0, None), ghi, atol=1e-6
    )
