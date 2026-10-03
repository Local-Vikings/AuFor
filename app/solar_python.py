"""Pure-Python solar physics: the fallback and parity oracle for native/ (bible 9.0).

Same signatures and units as app/solar.py. Pure functions: no network, no disk.
Time is UTC epoch seconds, angles are degrees, azimuth 180 = south.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pvlib

SOLAR_CONSTANT_CLEARSKY_W_M2 = 1098.0  # Haurwitz (bible 9.7)
HAURWITZ_EXPONENT = 0.059
KASTEN_COEFFICIENT = 0.75
KASTEN_EXPONENT = 3.4


def _index(t_utc: np.ndarray) -> pd.DatetimeIndex:
    return pd.to_datetime(t_utc, unit="s", utc=True)


def sun_position(
    t_utc: np.ndarray, lat_deg: float, lon_deg: float
) -> tuple[np.ndarray, np.ndarray]:
    """Return (zenith_deg, azimuth_deg) with the analytical Spencer formulas.

    Same model as native/solarsight.cpp, so the two engines agree to rounding
    error (bible 9.0 parity rule). SPA is more precise (about 0.5 deg apart).
    """
    times = _index(t_utc)
    day_of_year = times.dayofyear
    declination = pvlib.solarposition.declination_spencer71(day_of_year)
    eot = pvlib.solarposition.equation_of_time_spencer71(day_of_year)
    hour_angle = np.radians(pvlib.solarposition.hour_angle(times, lon_deg, eot))
    lat = np.radians(lat_deg)
    zenith = pvlib.solarposition.solar_zenith_analytical(lat, hour_angle, declination)
    azimuth = pvlib.solarposition.solar_azimuth_analytical(lat, hour_angle, declination, zenith)
    return np.degrees(zenith), np.degrees(azimuth)


def poa_irradiance(
    zenith_deg: np.ndarray,
    azimuth_deg: np.ndarray,
    ghi: np.ndarray,
    dni: np.ndarray,
    dhi: np.ndarray,
    tilt_deg: float,
    surface_azimuth_deg: float,
    albedo: float,
) -> np.ndarray:
    """Isotropic-sky plane-of-array irradiance in W/m2 (bible 9.2)."""
    zenith = np.radians(zenith_deg)
    tilt = np.radians(tilt_deg)
    cos_aoi = np.cos(zenith) * np.cos(tilt) + np.sin(zenith) * np.sin(tilt) * np.cos(
        np.radians(azimuth_deg - surface_azimuth_deg)
    )
    direct = np.where(zenith_deg < 90, dni * np.maximum(cos_aoi, 0.0), 0.0)
    diffuse = dhi * (1 + np.cos(tilt)) / 2
    ground = ghi * albedo * (1 - np.cos(tilt)) / 2
    return direct + diffuse + ground


def cell_temperature(temp_air_c: np.ndarray, poa_w_m2: np.ndarray, noct_c: float) -> np.ndarray:
    """NOCT cell temperature in C (bible 9.3)."""
    return temp_air_c + (noct_c - 20.0) / 800.0 * poa_w_m2


def dc_power(
    poa_w_m2: np.ndarray, t_cell_c: np.ndarray, p_stc_w: float, gamma_per_c: float
) -> np.ndarray:
    """DC power in W, clamped at zero (bible 9.4)."""
    power = p_stc_w * poa_w_m2 / 1000.0 * (1 + gamma_per_c * (t_cell_c - 25.0))
    return np.maximum(power, 0.0)


def ac_power(p_dc_w: np.ndarray, pr: float, inverter_max_w: float) -> np.ndarray:
    """AC power in W with inverter clipping (bible 9.5)."""
    return np.minimum(p_dc_w * pr, inverter_max_w)


def energy_kwh(p_ac_w: np.ndarray, dt_h: float) -> float:
    """Energy in kWh from power samples spaced dt_h hours apart (bible 9.6)."""
    return float(np.sum(p_ac_w * dt_h) / 1000.0)


def irradiance_from_clouds(
    zenith_deg: np.ndarray, cloud_cover_pct: np.ndarray, t_utc: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Haurwitz -> Kasten-Czeplak -> Erbs; returns (ghi, dni, dhi) (bible 9.7)."""
    cos_z = np.cos(np.radians(zenith_deg))
    safe_cos = np.where(cos_z > 0, cos_z, 1.0)
    clear = np.where(
        cos_z > 0,
        SOLAR_CONSTANT_CLEARSKY_W_M2 * safe_cos * np.exp(-HAURWITZ_EXPONENT / safe_cos),
        0.0,
    )
    oktas = np.clip(cloud_cover_pct, 0.0, 100.0) / 100.0 * 8.0
    ghi = clear * (1 - KASTEN_COEFFICIENT * (oktas / 8.0) ** KASTEN_EXPONENT)
    split = pvlib.irradiance.erbs(ghi, zenith_deg, _index(t_utc))
    return ghi, np.asarray(split["dni"]), np.asarray(split["dhi"])
