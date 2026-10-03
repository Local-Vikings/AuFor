"""Rule-based recommendations (bible 10)."""

from __future__ import annotations

from datetime import datetime

from app.config import DEFAULT_PERFORMANCE_RATIO
from app.models import PanelConfig, Recommendation
from app.solar import annual_poa, best_orientation

OPT_MIN_GAIN = 0.05  # >5% yearly gain, bible 10 rule 4
OPT_CLOUD_PCT = 0.0  # clear sky year, only for comparing orientations


def optimize_rule(
    lat: float,
    lon: float,
    panels: list[PanelConfig],
    hour: datetime,
    pr: float = DEFAULT_PERFORMANCE_RATIO,
) -> list[Recommendation]:
    best = best_orientation(lat, lon, hour.year, OPT_CLOUD_PCT)
    if not best:
        return []
    tilt, az, best_kwh = best
    res = []
    for p in panels:
        cur = annual_poa(lat, lon, hour.year, p.tilt, p.azimuth, OPT_CLOUD_PCT)
        if not cur:
            continue
        gain = best_kwh / cur - 1
        if gain <= OPT_MIN_GAIN:
            continue
        kwp = p.count * p.watt_peak / 1000
        res.append(
            Recommendation(
                subtopic="optimize",
                hour=hour,
                title=f"Turn panels to tilt {tilt:.0f} deg, azimuth {az:.0f} deg",
                reason=(
                    f"Panels at tilt {p.tilt:.0f} deg, azimuth {p.azimuth:.0f} deg get "
                    f"{cur:.0f} kWh/m2 per clear-sky year, the best orientation gets "
                    f"{best_kwh:.0f} kWh/m2 (+{gain * 100:.0f}%)."
                ),
                # clear sky -> upper bound
                kwh_effect=round(kwp * pr * (best_kwh - cur), 1),
            )
        )
    return res
