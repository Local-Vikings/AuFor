#!/usr/bin/env python3
"""Validate the forecast against baselines and an external source (bible 8, T42/T43/T73).

Prints a markdown report you can paste into the README and the validation slide:

1. Clear-sky bound: the forecast must not exceed a pvlib Ineichen clear sky (x1.05).
2. Measured accuracy: RMSE, MAE and skill against persistence, but ONLY from real (battery or
   panel) power readings. Simulated readings are not measurements. With no real data it says so
   and claims no accuracy.
3. PVGIS cross-check: monthly and yearly energy of the same system for a reference year, with
   Open-Meteo's historical weather, against the European Commission's PVGIS.
4. Yearly specific yield against the plausible range for Sofia.

Needs the internet for 1, 3 and 4 (use --offline to skip them). Exit code 1 if a check fails.

    python scripts/validate.py
    python scripts/validate.py --year 2025 --output validation.md
    python scripts/validate.py --lat 42.6977 --lon 23.3219 --count 10 --watt-peak 400 --tilt 35 --azimuth 180
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from app import baselines, pipeline, readings  # noqa: E402
from app.config import DEFAULT_PERFORMANCE_RATIO  # noqa: E402
from app.models import ForecastRequest  # noqa: E402
from app.weather import OPEN_METEO_ARCHIVE_URL, HOURLY_FIELDS, fetch_weather, parse_weather  # noqa: E402

PVGIS_URL = "https://re.jrc.ec.europa.eu/api/v5_2/PVcalc"
PVGIS_TOLERANCE_PCT = 20.0  # bible 8: within about 15-20%
SPECIFIC_YIELD_RANGE = (1200.0, 1500.0)  # kWh per kWp per year, Sofia (bible 8)
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
NETWORK_TIMEOUT_S = 60.0


# ---- pure helpers (tested) ----


def percent_difference(model: float, reference: float) -> float:
    """(model - reference) / reference in percent."""
    return 100.0 * (model - reference) / reference


def monthly_energy(hourly: pd.DataFrame) -> list[float]:
    """kWh per calendar month (Jan to Dec) from hourly AC power in W."""
    kwh = hourly["p_ac_w"] / 1000.0
    by_month = kwh.groupby(kwh.index.month).sum()
    return [float(by_month.get(m, 0.0)) for m in range(1, 13)]


def compare_months(model: list[float], reference: list[float]) -> list[dict]:
    """Per-month comparison rows with the difference in percent."""
    return [
        {"month": MONTHS[i], "model_kwh": model[i], "pvgis_kwh": reference[i], "diff_pct": percent_difference(model[i], reference[i])}
        for i in range(12)
    ]


def check_clear_sky_bound(request: ForecastRequest, weather: pd.DataFrame, pr: float = DEFAULT_PERFORMANCE_RATIO) -> dict:
    """Run the forecast and the Ineichen bound over the same hours and count the exceedances."""
    result = pipeline.build_forecast(request, weather, pr=pr)
    power = result.hourly["p_ac_w"].to_numpy()
    bound = baselines.clear_sky(request, weather, pr)
    daylight = bound > 0.1 * bound.max()
    ratio = power[daylight] / bound[daylight] if daylight.any() else np.array([0.0])
    over = baselines.over(power, bound)
    return {"hours": len(power), "producing_hours": int((power > 0).sum()), "over": over,
            "max_ratio": float(ratio.max()), "passed": over == 0}


def check_measured_accuracy(hourly: pd.DataFrame, rows: list[dict]) -> dict:
    """Accuracy against real power readings, or an explanation of why there is none."""
    power_rows = [r for r in rows if r["type"] == "power_w"]
    real = [r for r in power_rows if r["source"] in ("battery", "panel")]
    if not real:
        fake = len(power_rows)
        reason = (f"{fake} simulated power readings exist, but simulated data is not a measurement"
                  if fake else "no power readings are stored")
        return {"available": False, "reason": f"{reason}; no accuracy is claimed"}
    found = baselines.accuracy(hourly["p_ac_w"], baselines.measured_hourly(hourly, real))
    if found is None:
        return {"available": False, "reason": f"{len(real)} real readings exist, but fewer than {baselines.MIN_ACCURACY_POINTS} "
                                              "forecast hours also have a reading 24 h earlier; not enough for a persistence comparison"}
    return {"available": True, **found}


# ---- network ----


def fetch_archive_weather(lat: float, lon: float, start: date, end: date) -> pd.DataFrame:
    """Hourly historical weather (Open-Meteo archive, ERA5) in the forecast's column format."""
    params = {"latitude": lat, "longitude": lon, "start_date": start.isoformat(), "end_date": end.isoformat(),
              "hourly": ",".join(HOURLY_FIELDS), "timezone": "auto", "wind_speed_unit": "ms"}
    response = httpx.get(OPEN_METEO_ARCHIVE_URL, params=params, timeout=NETWORK_TIMEOUT_S)
    response.raise_for_status()
    return parse_weather(response.json())


def fetch_pvgis(lat: float, lon: float, request: ForecastRequest, loss_pct: float) -> dict:
    """PVGIS PVcalc for the (first) panel group: monthly and yearly kWh."""
    group = request.panel_groups[0]
    params = {"lat": lat, "lon": lon, "peakpower": group.count * group.watt_peak / 1000.0, "loss": loss_pct,
              "angle": group.tilt, "aspect": group.azimuth - 180.0,  # PVGIS: 0 = south, 90 = west, -90 = east
              "mountingplace": "free", "pvtechchoice": "crystSi", "outputformat": "json"}
    response = httpx.get(PVGIS_URL, params=params, timeout=NETWORK_TIMEOUT_S)
    response.raise_for_status()
    outputs = response.json()["outputs"]
    return {"monthly": [m["E_m"] for m in outputs["monthly"]["fixed"]], "annual": outputs["totals"]["fixed"]["E_y"],
            "kwp": params["peakpower"]}


# ---- report ----


def render(sections: list[str], request: ForecastRequest, generated: datetime) -> str:
    group = request.panel_groups[0]
    head = (f"# Validation report\n\nSystem: {group.count} x {group.watt_peak:.0f} W, tilt {group.tilt:.0f} deg, azimuth "
            f"{group.azimuth:.0f} deg (180 = south), PR {DEFAULT_PERFORMANCE_RATIO}. Site {request.lat:.4f}, {request.lon:.4f}. "
            f"Generated {generated:%Y-%m-%d %H:%M} UTC.\n")
    return head + "\n" + "\n\n".join(sections) + "\n"


def section_clear_sky(result: dict | str) -> str:
    if isinstance(result, str):
        return f"## 1. Clear-sky bound\n\nSkipped: {result}"
    status = "PASS" if result["passed"] else "FAIL"
    return (f"## 1. Clear-sky bound: {status}\n\nForecast power vs a pvlib Ineichen clear sky (Linke turbidity "
            f"{baselines.CLEAR_SKY_LINKE_TURBIDITY}, the clearest realistic air, so it is a true upper bound), tolerance x"
            f"{baselines.CLEAR_SKY_TOLERANCE} plus a 2% slack at sunrise and sunset.\n\n"
            f"- Hours checked: {result['hours']} ({result['producing_hours']} producing)\n"
            f"- Hours above the bound: **{result['over']}**\n"
            f"- Highest forecast / bound ratio in daylight: **{result['max_ratio']:.2f}**")


def section_accuracy(result: dict) -> str:
    if not result["available"]:
        return f"## 2. Accuracy against measurements\n\n**Not available:** {result['reason']}."
    return (f"## 2. Accuracy against measurements\n\nOver {result['points']} hours of real readings, against persistence "
            f"(the same hour 24 h earlier):\n\n- RMSE: **{result['rmse_w']:.0f} W**\n- MAE: **{result['mae_w']:.0f} W**\n"
            f"- Persistence RMSE: {result['persistence_rmse_w']:.0f} W\n- Skill = 1 - RMSE / RMSE_persistence: **{result['skill']:.2f}**")


def section_pvgis(year: int, rows: list[dict], model_total: float, pvgis_total: float, kwp: float, loss: float) -> str:
    diff = percent_difference(model_total, pvgis_total)
    status = "PASS" if abs(diff) <= PVGIS_TOLERANCE_PCT else "FAIL"
    lines = [f"## 3. PVGIS cross-check: {status}\n",
             f"Same system, our model with Open-Meteo historical weather for {year} (PR {DEFAULT_PERFORMANCE_RATIO}) against "
             f"PVGIS PVcalc (system loss {loss:.0f}%, long-term average weather). Weather differs from year to year, so some "
             f"scatter is expected. Target: within about {PVGIS_TOLERANCE_PCT:.0f}%.\n",
             "| Month | Model kWh | PVGIS kWh | Difference |", "|---|---:|---:|---:|"]
    lines += [f"| {r['month']} | {r['model_kwh']:.0f} | {r['pvgis_kwh']:.0f} | {r['diff_pct']:+.1f}% |" for r in rows]
    lines.append(f"| **Year** | **{model_total:.0f}** | **{pvgis_total:.0f}** | **{diff:+.1f}%** |")
    outside = [f"{r['month']} ({r['diff_pct']:+.0f}%)" for r in rows if abs(r["diff_pct"]) > PVGIS_TOLERANCE_PCT]
    lines.append("\nThe pass is for the year. " + (f"Single months outside +/-{PVGIS_TOLERANCE_PCT:.0f}%: {', '.join(outside)}. "
                 "One year of weather is compared with a long-term average, so individual months can differ more than the year does."
                 if outside else f"Every month is within +/-{PVGIS_TOLERANCE_PCT:.0f}%."))
    return "\n".join(lines)


def section_yield(model_total: float, pvgis_total: float, kwp: float) -> str:
    ours, theirs = model_total / kwp, pvgis_total / kwp
    low, high = SPECIFIC_YIELD_RANGE
    verdict = "inside" if low <= ours <= high else "OUTSIDE"
    return (f"## 4. Yearly specific yield\n\n- Our model: **{ours:.0f} kWh/kWp/year**\n- PVGIS: **{theirs:.0f} kWh/kWp/year**\n"
            f"- Plausible range for Sofia (bible 8): {low:.0f}-{high:.0f}. Our value is {verdict} that range.")


def build_request(args: argparse.Namespace) -> ForecastRequest:
    panel = {"count": args.count, "watt_peak": args.watt_peak, "tilt": args.tilt, "azimuth": args.azimuth,
             "noct": 45, "gamma": -0.004, "inverter_max_w": args.inverter_w}
    return ForecastRequest.model_validate({
        "lat": args.lat, "lon": args.lon, "panel": panel, "days": 3, "load": {"daily_kwh": 12},
        "battery": {"capacity_kwh": 10, "dod": 0.9, "eta_c": 0.95, "eta_d": 0.95, "max_power_kw": 5, "initial_soc_kwh": 5}})


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate the forecast against baselines and PVGIS.")
    parser.add_argument("--lat", type=float, default=42.6977)
    parser.add_argument("--lon", type=float, default=23.3219)
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--watt-peak", type=float, default=400.0)
    parser.add_argument("--tilt", type=float, default=35.0)
    parser.add_argument("--azimuth", type=float, default=180.0)
    parser.add_argument("--inverter-w", type=float, default=4000.0)
    parser.add_argument("--year", type=int, default=datetime.now().year - 1, help="reference year for the PVGIS comparison")
    parser.add_argument("--pvgis-loss", type=float, default=14.0, help="PVGIS system loss in percent (its default)")
    parser.add_argument("--offline", action="store_true", help="skip everything that needs the internet")
    parser.add_argument("--output", help="also write the report to this file")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    request = build_request(args)
    failed = False
    sections: list[str] = []

    # 1. clear-sky bound on the real 16-day forecast
    if args.offline:
        sections.append(section_clear_sky("offline mode (needs a real weather forecast)"))
        forecast_weather = None
    else:
        try:
            forecast_weather = fetch_weather(args.lat, args.lon, 16)
            result = check_clear_sky_bound(request, forecast_weather)
            failed |= not result["passed"]
            sections.append(section_clear_sky(result))
        except Exception as error:  # noqa: BLE001 - report and carry on with the other checks
            forecast_weather = None
            sections.append(section_clear_sky(f"could not get the forecast ({type(error).__name__}: {error})"))

    # 2. accuracy from real readings only, hindcast with historical weather over the readings' dates
    rows = readings.recent("power_w", 24 * 30)
    real = [r for r in rows if r["source"] in ("battery", "panel")]
    if real and not args.offline:
        try:
            start = min(r["timestamp"] for r in real).date() - timedelta(days=1)
            end = max(r["timestamp"] for r in real).date()
            weather = fetch_archive_weather(args.lat, args.lon, start, min(end, date.today() - timedelta(days=5)))
            hourly = pipeline.build_forecast(request, weather).hourly
            sections.append(section_accuracy(check_measured_accuracy(hourly, rows)))
        except Exception as error:  # noqa: BLE001
            sections.append(f"## 2. Accuracy against measurements\n\nCould not run: {type(error).__name__}: {error}")
    elif real:
        sections.append("## 2. Accuracy against measurements\n\nSkipped: offline mode (needs historical weather for the readings' dates).")
    else:
        sections.append(section_accuracy(check_measured_accuracy(pd.DataFrame(), rows)))

    # 3 + 4. PVGIS and yearly yield
    if args.offline:
        sections.append("## 3. PVGIS cross-check\n\nSkipped: offline mode.")
    else:
        try:
            weather = fetch_archive_weather(args.lat, args.lon, date(args.year, 1, 1), date(args.year, 12, 31))
            hourly = pipeline.build_forecast(request, weather).hourly
            model_months = monthly_energy(hourly)
            pvgis = fetch_pvgis(args.lat, args.lon, request, args.pvgis_loss)
            total = sum(model_months)
            sections.append(section_pvgis(args.year, compare_months(model_months, pvgis["monthly"]), total, pvgis["annual"], pvgis["kwp"], args.pvgis_loss))
            sections.append(section_yield(total, pvgis["annual"], pvgis["kwp"]))
            failed |= abs(percent_difference(total, pvgis["annual"])) > PVGIS_TOLERANCE_PCT
        except Exception as error:  # noqa: BLE001
            sections.append(f"## 3. PVGIS cross-check\n\nCould not run: {type(error).__name__}: {error}")

    report = render(sections, request, datetime.now(timezone.utc))
    print(report)
    if args.output:
        Path(args.output).write_text(report, encoding="utf-8")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
