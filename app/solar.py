"""Solar physics API: ctypes wrapper over native/libsolarsight with a Python fallback.

The C++ core (bible 9.0) is loaded once. If the library is missing or fails to
load, every function uses app/solar_python.py and ENGINE is "python", so the app
never breaks because a compile failed. Pass engine="python" or engine="native"
to force one side (used by the parity tests).
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
from pathlib import Path

import numpy as np

from app import solar_python
from app.config import DEFAULT_ALBEDO, OPT_AZIMUTH_STEP_DEG, OPT_TILT_STEP_DEG

logger = logging.getLogger(__name__)

_EXTENSION = {"win32": "dll", "darwin": "dylib"}.get(sys.platform, "so")
_DEFAULT_LIB = Path(__file__).resolve().parent.parent / "native" / f"libsolarsight.{_EXTENSION}"
_ARRAY = np.ctypeslib.ndpointer(dtype=np.float64, flags="C_CONTIGUOUS")
_DOUBLE = ctypes.c_double
_INT = ctypes.c_int

_STATUS_MESSAGES = {
    1: "null pointer",
    2: "bad length",
    3: "parameter out of range or not finite",
}


class SolarError(ValueError):
    """Raised when the native core rejects its inputs."""


def _load_library() -> ctypes.CDLL | None:
    path = Path(os.environ.get("SOLAR_LIB_PATH") or _DEFAULT_LIB)
    try:
        lib = ctypes.CDLL(str(path))
        _declare(lib)
    except (OSError, AttributeError) as error:
        logger.warning("Native solar core unavailable (%s); using the Python fallback.", error)
        return None
    return lib


def _declare(lib: ctypes.CDLL) -> None:
    signatures = {
        "ss_sun_position": [_ARRAY, _INT, _DOUBLE, _DOUBLE, _ARRAY, _ARRAY],
        "ss_poa_irradiance": [_ARRAY] * 5 + [_INT] + [_DOUBLE] * 3 + [_ARRAY],
        "ss_cell_temperature": [_ARRAY, _ARRAY, _INT, _DOUBLE, _ARRAY],
        "ss_dc_power": [_ARRAY, _ARRAY, _INT, _DOUBLE, _DOUBLE, _ARRAY],
        "ss_ac_power": [_ARRAY, _INT, _DOUBLE, _DOUBLE, _ARRAY],
        "ss_energy_kwh": [_ARRAY, _INT, _DOUBLE, ctypes.POINTER(_DOUBLE)],
        "ss_irradiance_from_clouds": [_ARRAY, _ARRAY, _ARRAY, _INT, _ARRAY, _ARRAY, _ARRAY],
    }
    for name, argtypes in signatures.items():
        function = getattr(lib, name)
        function.argtypes = argtypes
        function.restype = _INT
    _declare_optimizer(lib)
    if hasattr(lib, "ss_metrics"):  
        lib.ss_metrics.argtypes = [_ARRAY, _ARRAY, _ARRAY, _INT] + [ctypes.POINTER(_DOUBLE)] * 3


def _declare_optimizer(lib: ctypes.CDLL) -> None:
    """Declare the T20 functions; an older library without them just disables Optimize."""
    pointer = ctypes.POINTER(_DOUBLE)
    optional = {
        "ss_annual_poa_kwh_m2": [_DOUBLE, _DOUBLE, _INT, _DOUBLE, _DOUBLE, _DOUBLE, _DOUBLE, pointer],
        "ss_optimal_orientation": [_DOUBLE, _DOUBLE, _INT] + [_DOUBLE] * 4 + [pointer] * 3,
    }
    for name, argtypes in optional.items():
        function = getattr(lib, name, None)
        if function is not None:
            function.argtypes = argtypes
            function.restype = _INT


_LIB = _load_library()
ENGINE = "native" if _LIB is not None else "python"


def _use_native(engine: str | None) -> bool:
    chosen = engine or ENGINE
    if chosen == "native" and _LIB is None:
        raise RuntimeError("native engine requested but libsolarsight is not loaded")
    return chosen == "native"


def _check(status: int, name: str) -> None:
    if status != 0:
        reason = _STATUS_MESSAGES.get(status, f"status {status}")
        raise SolarError(f"{name} failed: {reason}")


def _f64(values: np.ndarray) -> np.ndarray:
    return np.ascontiguousarray(values, dtype=np.float64)


def _out(n: int) -> np.ndarray:
    return np.zeros(n, dtype=np.float64)


def sun_position(
    t_utc: np.ndarray, lat_deg: float, lon_deg: float, engine: str | None = None
) -> tuple[np.ndarray, np.ndarray]:
    """Return (zenith_deg, azimuth_deg). t_utc is UTC epoch seconds (bible 9.1)."""
    t = _f64(t_utc)
    if not _use_native(engine):
        return solar_python.sun_position(t, lat_deg, lon_deg)
    zenith, azimuth = _out(len(t)), _out(len(t))
    _check(_LIB.ss_sun_position(t, len(t), lat_deg, lon_deg, zenith, azimuth), "sun_position")
    return zenith, azimuth


def poa_irradiance(
    zenith_deg: np.ndarray,
    azimuth_deg: np.ndarray,
    ghi: np.ndarray,
    dni: np.ndarray,
    dhi: np.ndarray,
    tilt_deg: float,
    surface_azimuth_deg: float,
    albedo: float,
    engine: str | None = None,
) -> np.ndarray:
    """Plane-of-array irradiance in W/m2 (bible 9.2); azimuth 180 = south."""
    z, a, g, n_, d = (_f64(x) for x in (zenith_deg, azimuth_deg, ghi, dni, dhi))
    if not _use_native(engine):
        return solar_python.poa_irradiance(
            z, a, g, n_, d, tilt_deg, surface_azimuth_deg, albedo
        )
    poa = _out(len(z))
    status = _LIB.ss_poa_irradiance(
        z, a, g, n_, d, len(z), tilt_deg, surface_azimuth_deg, albedo, poa
    )
    _check(status, "poa_irradiance")
    return poa


def cell_temperature(
    temp_air_c: np.ndarray, poa_w_m2: np.ndarray, noct_c: float, engine: str | None = None
) -> np.ndarray:
    """Cell temperature in C (bible 9.3)."""
    air, poa = _f64(temp_air_c), _f64(poa_w_m2)
    if not _use_native(engine):
        return solar_python.cell_temperature(air, poa, noct_c)
    out = _out(len(air))
    _check(_LIB.ss_cell_temperature(air, poa, len(air), noct_c, out), "cell_temperature")
    return out


def dc_power(
    poa_w_m2: np.ndarray,
    t_cell_c: np.ndarray,
    p_stc_w: float,
    gamma_per_c: float,
    engine: str | None = None,
) -> np.ndarray:
    """DC power in W (bible 9.4). p_stc_w = panel_count * watt_peak."""
    poa, cell = _f64(poa_w_m2), _f64(t_cell_c)
    if not _use_native(engine):
        return solar_python.dc_power(poa, cell, p_stc_w, gamma_per_c)
    out = _out(len(poa))
    _check(_LIB.ss_dc_power(poa, cell, len(poa), p_stc_w, gamma_per_c, out), "dc_power")
    return out


def ac_power(
    p_dc_w: np.ndarray, pr: float, inverter_max_w: float, engine: str | None = None
) -> np.ndarray:
    """AC power in W for one inverter (bible 9.5); sum panel groups in the caller."""
    dc = _f64(p_dc_w)
    if not _use_native(engine):
        return solar_python.ac_power(dc, pr, inverter_max_w)
    out = _out(len(dc))
    _check(_LIB.ss_ac_power(dc, len(dc), pr, inverter_max_w, out), "ac_power")
    return out


def energy_kwh(p_ac_w: np.ndarray, dt_h: float, engine: str | None = None) -> float:
    """Energy in kWh from power samples dt_h hours apart (bible 9.6)."""
    power = _f64(p_ac_w)
    if not _use_native(engine):
        return solar_python.energy_kwh(power, dt_h)
    out = _DOUBLE(0.0)
    _check(_LIB.ss_energy_kwh(power, len(power), dt_h, ctypes.byref(out)), "energy_kwh")
    return out.value


def irradiance_from_clouds(
    zenith_deg: np.ndarray,
    cloud_cover_pct: np.ndarray,
    t_utc: np.ndarray,
    engine: str | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Fallback (ghi, dni, dhi) from cloud cover when irradiance is missing (bible 9.7)."""
    z, c, t = _f64(zenith_deg), _f64(cloud_cover_pct), _f64(t_utc)
    if not _use_native(engine):
        return solar_python.irradiance_from_clouds(z, c, t)
    ghi, dni, dhi = _out(len(z)), _out(len(z)), _out(len(z))
    status = _LIB.ss_irradiance_from_clouds(z, c, t, len(z), ghi, dni, dhi)
    _check(status, "irradiance_from_clouds")
    return ghi, dni, dhi



def _optimizer_available() -> bool:
    return _LIB is not None and hasattr(_LIB, "ss_optimal_orientation")


def annual_poa(
    lat_deg: float, lon_deg: float, year: int, tilt_deg: float, azimuth_deg: float, cloud_pct: float
) -> float | None:
    """Clear-sky yearly plane-of-array energy in kWh/m2; None without the native core (T20)."""
    if not _optimizer_available():
        return None
    result = _DOUBLE()
    status = _LIB.ss_annual_poa_kwh_m2(
        lat_deg, lon_deg, year, tilt_deg, azimuth_deg, DEFAULT_ALBEDO, cloud_pct, ctypes.byref(result)
    )
    _check(status, "annual_poa")
    return result.value


def best_orientation(
    lat_deg: float, lon_deg: float, year: int, cloud_pct: float
) -> tuple[float, float, float] | None:
    """Best (tilt_deg, azimuth_deg, kWh/m2) over a yearly grid search; None without the native core."""
    if not _optimizer_available():
        return None
    tilt, azimuth, kwh = _DOUBLE(), _DOUBLE(), _DOUBLE()
    status = _LIB.ss_optimal_orientation(
        lat_deg, lon_deg, year, DEFAULT_ALBEDO, cloud_pct, OPT_TILT_STEP_DEG, OPT_AZIMUTH_STEP_DEG,
        ctypes.byref(tilt), ctypes.byref(azimuth), ctypes.byref(kwh),
    )
    _check(status, "best_orientation")
    return tilt.value, azimuth.value, kwh.value


def metrics(
    pred: np.ndarray, obs: np.ndarray, ref: np.ndarray, engine: str | None = None
) -> tuple[float, float, float]:
    """RMSE, MAE and skill = 1 - RMSE / RMSE_ref of ``pred`` against ``obs`` (bible 8).

    ``ref`` is the baseline forecast (persistence); skill is 0 when the baseline is perfect.
    Uses the C++ core when it has ss_metrics, otherwise the Python version.

    Raises:
        ValueError: If the arrays differ in length or are empty.
    """
    p, o, r = _f64(pred), _f64(obs), _f64(ref)
    if not len(p) == len(o) == len(r) or len(p) == 0:
        raise ValueError("pred, obs and ref must be non-empty and the same length")
    if _LIB is None or not hasattr(_LIB, "ss_metrics") or engine == "python":
        if engine == "native":
            raise RuntimeError("native engine requested but libsolarsight has no ss_metrics")
        return solar_python.metrics(p, o, r)
    rmse, mae, skill = _DOUBLE(), _DOUBLE(), _DOUBLE()
    _check(_LIB.ss_metrics(p, o, r, len(p), ctypes.byref(rmse), ctypes.byref(mae), ctypes.byref(skill)), "metrics")
    return rmse.value, mae.value, skill.value
