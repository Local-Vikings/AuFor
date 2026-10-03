"""Hourly battery dispatch simulation (bible 9.8).

Pure function over arrays: no network, no disk, no globals. Surplus charges the
battery, a deficit discharges it, and whatever the battery cannot take is exported
or imported.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class BatteryResult:
    """Per-hour battery flows in kW and the state of charge at the end of each hour."""

    soc_kwh: np.ndarray
    charge_kw: np.ndarray
    discharge_kw: np.ndarray
    import_kw: np.ndarray
    export_kw: np.ndarray


def simulate(
    pv_kw: np.ndarray,
    load_kw: np.ndarray,
    capacity_kwh: float,
    dod: float,
    eta_c: float,
    eta_d: float,
    max_power_kw: float,
    initial_soc_kwh: float,
    dt_h: float = 1.0,
) -> BatteryResult:
    """Simulate the battery over matching PV and load arrays.

    Args:
        pv_kw: Solar power per step.
        load_kw: Household load per step.
        capacity_kwh: Battery capacity; 0 means no battery.
        dod: Depth of discharge, so the minimum charge is capacity * (1 - dod).
        eta_c: Charge efficiency.
        eta_d: Discharge efficiency.
        max_power_kw: Maximum charge and discharge power.
        initial_soc_kwh: Starting charge, clamped into the allowed range.
        dt_h: Step length in hours.

    Returns:
        BatteryResult. SoC always stays within [capacity * (1 - dod), capacity].
    """
    soc_min = capacity_kwh * (1.0 - dod)
    soc_max = capacity_kwh
    soc = min(max(initial_soc_kwh, soc_min), soc_max)

    n = len(pv_kw)
    soc_out = np.zeros(n)
    charge = np.zeros(n)
    discharge = np.zeros(n)
    grid_import = np.zeros(n)
    export = np.zeros(n)

    for i in range(n):
        surplus = pv_kw[i] - load_kw[i]
        if surplus >= 0:
            charge[i] = min(surplus, max_power_kw, (soc_max - soc) / (eta_c * dt_h))
            soc += charge[i] * eta_c * dt_h
            export[i] = surplus - charge[i]
        else:
            deficit = -surplus
            discharge[i] = min(deficit, max_power_kw, (soc - soc_min) * eta_d / dt_h)
            soc -= discharge[i] / eta_d * dt_h
            grid_import[i] = deficit - discharge[i]
        soc_out[i] = soc

    return BatteryResult(soc_out, charge, discharge, grid_import, export)
