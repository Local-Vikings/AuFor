"""Rule-based recommendations (bible 10).

Every rule is a pure function over the forecast tables. All numbers in the text
come from the data; thresholds live in app/config.py.
"""

from __future__ import annotations

import logging
from datetime import datetime

import numpy as np
import pandas as pd

from app.config import (
    DEFAULT_PERFORMANCE_RATIO,
    DIRECT_FULL_BEFORE_HOUR,
    DIRECT_MIN_EXPORT_KWH,
    RECOMMEND_DAYS,
    STORE_AVERAGE_DAYS,
    STORE_WEAK_FRACTION,
    USE_MIN_SURPLUS_KWH,
    USE_WINDOW_HOURS,
    WARN_DIP_FRACTION,
    WARN_MIN_HOURS,
    WARN_MIN_LOSS_KWH,
    WARN_PEAK_FRACTION,
)
from app.models import BatteryConfig, ForecastRequest, PanelConfig, Recommendation
from app.solar import SolarError, optimize

logger = logging.getLogger(__name__)



MIN_GAIN = 0.05 


def optimize_rule(lat, lon, panels, hour, pr=DEFAULT_PERFORMANCE_RATIO):

    res = []
    for p in panels:
        r = optimize(lat, lon, hour.year, p.tilt, p.azimuth)
        if r is None:
            return []  
        now, tilt, az, best = r
        if now <= 0 or best / now - 1 <= MIN_GAIN:
            continue
        gain = (best / now - 1) * 100
        kwp = p.count * p.watt_peak / 1000
        res.append(Recommendation(
            subtopic="optimize",
            hour=hour,
            title=f"Turn panels to tilt {tilt:.0f} deg, azimuth {az:.0f} deg",
            reason=f"Panels at tilt {p.tilt:.0f} deg, azimuth {p.azimuth:.0f} deg get {now:.0f} "
                   f"kWh/m2 in a clear-sky year. With tilt {tilt:.0f} deg and azimuth {az:.0f} "
                   f"deg they would get {best:.0f} kWh/m2 (+{gain:.0f}%).",
            kwh_effect=round(kwp * pr * (best - now), 1),  
        ))
    return res



def _day_label(timestamp: pd.Timestamp, now: pd.Timestamp) -> str:
    offset = (timestamp.date() - now.date()).days
    if offset == 0:
        return "today"
    return "tomorrow" if offset == 1 else timestamp.strftime("%a %d %b")


def _near_term_days(hourly: pd.DataFrame, now: pd.Timestamp) -> list[pd.DataFrame]:
    """Hourly rows from now onward, split per local day, for the next RECOMMEND_DAYS days."""
    upcoming = hourly[hourly.index >= now.floor("h")]
    return [chunk for _, chunk in upcoming.groupby(upcoming.index.date)][:RECOMMEND_DAYS]


def use_rule(hourly: pd.DataFrame, now: pd.Timestamp) -> list[Recommendation]:
    """Use: the 3-hour window with the most surplus is the time to run flexible loads (bible 10.1)."""
    for chunk in _near_term_days(hourly, now):
        surplus = np.maximum(chunk["p_ac_w"] - chunk["load_w"], 0.0).to_numpy() / 1000.0
        if len(surplus) < USE_WINDOW_HOURS:
            continue
        window_sums = np.convolve(surplus, np.ones(USE_WINDOW_HOURS), mode="valid")
        start = int(np.argmax(window_sums))
        total = float(window_sums[start])
        if total < USE_MIN_SURPLUS_KWH:
            continue
        rows = chunk.iloc[start : start + USE_WINDOW_HOURS]
        first = rows.index[0]
        end = first + pd.Timedelta(hours=USE_WINDOW_HOURS)
        return [
            Recommendation(
                subtopic="use",
                hour=first.to_pydatetime(),
                title=f"Run flexible loads {_day_label(first, now)} {first:%H:%M}-{end:%H:%M}",
                reason=(
                    f"Solar output is forecast to exceed your load by "
                    f"{total / USE_WINDOW_HOURS:.1f} kW on average in this window "
                    f"({total:.1f} kWh of surplus, {rows['cloud_cover'].mean():.0f}% cloud cover). "
                    "Move laundry, the dishwasher or EV charging here."
                ),
                kwh_effect=round(total, 1),
            )
        ]
    return []


def store_rule(
    hourly: pd.DataFrame,
    daily: pd.DataFrame,
    battery: BatteryConfig,
    daily_load_kwh: float,
    now: pd.Timestamp,
) -> list[Recommendation]:
    """Store: weak sun tomorrow, so enter tomorrow with a full battery (bible 10.2)."""
    if battery.capacity_kwh <= 0:
        return []
    days = list(daily.index)
    today = next((i for i, day in enumerate(days) if day >= now.date().isoformat()), None)
    if today is None or today + 1 >= len(days):
        return []
    window = daily["kwh"].iloc[today : today + STORE_AVERAGE_DAYS]
    average = float(window.mean())
    tomorrow = float(daily["kwh"].iloc[today + 1])
    if average <= 0 or tomorrow >= STORE_WEAK_FRACTION * average:
        return []

    today_rows = hourly[(hourly.index.date.astype(str) == days[today]) & (hourly.index >= now.floor("h"))]
    soc_tonight = float(today_rows["soc_kwh"].iloc[-1]) if len(today_rows) else battery.initial_soc_kwh
    surplus_today = float(np.maximum(today_rows["p_ac_w"] - today_rows["load_w"], 0.0).sum() / 1000.0)
    capacity = battery.capacity_kwh
    sunny = today_rows[today_rows["p_ac_w"] > today_rows["load_w"]]
    first = sunny.index[0] if len(sunny) else today_rows.index[0] if len(today_rows) else now
    cloud_tomorrow = hourly[hourly.index.date.astype(str) == days[today + 1]]["cloud_cover"].mean()
    if soc_tonight >= 0.98 * capacity:
        action = f"The battery is forecast to be full tonight ({soc_tonight:.1f} kWh); keep it full by not draining it for non-essential loads."
    else:
        action = (
            f"The battery is forecast at only {soc_tonight:.1f} of {capacity:.0f} kWh tonight with "
            f"{surplus_today:.1f} kWh of solar surplus today: store that surplus instead of using or exporting it."
        )
    avoidable = min(capacity * battery.dod * battery.eta_d, max(daily_load_kwh - tomorrow, 0.0))
    return [
        Recommendation(
            subtopic="store",
            hour=first.to_pydatetime(),
            title="Fill the battery today, tomorrow is weak",
            reason=(
                f"Tomorrow's forecast is {tomorrow:.1f} kWh ({cloud_tomorrow:.0f}% cloud cover), "
                f"{100 * (1 - tomorrow / average):.0f}% below the {len(window)}-day average of "
                f"{average:.1f} kWh and below your {daily_load_kwh:.0f} kWh daily use. {action}"
            ),
            kwh_effect=round(avoidable, 1),
        )
    ]


def direct_rule(hourly: pd.DataFrame, battery: BatteryConfig, now: pd.Timestamp) -> list[Recommendation]:
    """Direct: battery full before noon and surplus remains, so use or export it now (bible 10.3)."""
    if battery.capacity_kwh <= 0:
        return []
    for chunk in _near_term_days(hourly, now):
        morning = chunk[(chunk.index.hour < DIRECT_FULL_BEFORE_HOUR) & (chunk["p_ac_w"] > 0)]
        full = morning[morning["soc_kwh"] >= battery.capacity_kwh * 0.999]
        if full.empty:
            continue
        first = full.index[0]
        exported = float(chunk.loc[first:, "grid_export_w"].sum() / 1000.0)
        if exported < DIRECT_MIN_EXPORT_KWH:
            continue
        return [
            Recommendation(
                subtopic="direct",
                hour=first.to_pydatetime(),
                title=f"Battery full by {first:%H:%M} {_day_label(first, now)}, use the surplus now",
                reason=(
                    f"The battery reaches {battery.capacity_kwh:.0f} kWh at {first:%H:%M}; "
                    f"{exported:.1f} kWh of solar would be exported after that. "
                    "Run flexible loads now or export it."
                ),
                kwh_effect=round(exported, 1),
            )
        ]
    return []


def _dip_blocks(dip: np.ndarray) -> list[tuple[int, int]]:
    """Return (start, stop) positions of consecutive True runs."""
    blocks, start = [], None
    for position, flag in enumerate(dip):
        if flag and start is None:
            start = position
        elif not flag and start is not None:
            blocks.append((start, position))
            start = None
    if start is not None:
        blocks.append((start, len(dip)))
    return blocks


def warning_rule(hourly: pd.DataFrame, now: pd.Timestamp) -> list[Recommendation]:
    """Warning: clouds hit the peak production hours, so flag the drop (bible 10.5)."""
    result = []
    for chunk in _near_term_days(hourly, now):
        clear = chunk["p_ac_clear_w"].to_numpy()
        if clear.max() <= 0:
            continue
        actual = chunk["p_ac_w"].to_numpy()
        peak = clear >= WARN_PEAK_FRACTION * clear.max()
        dip = peak & (actual < WARN_DIP_FRACTION * clear)
        best = None
        for start, stop in _dip_blocks(dip):
            lost = float((clear[start:stop] - actual[start:stop]).sum() / 1000.0)
            if stop - start >= WARN_MIN_HOURS and lost >= WARN_MIN_LOSS_KWH:
                if best is None or lost > best[2]:
                    best = (start, stop, lost)
        if best is None:
            continue
        start, stop, lost = best
        first = chunk.index[start]
        end = first + pd.Timedelta(hours=stop - start)
        result.append(
            Recommendation(
                subtopic="warning",
                hour=first.to_pydatetime(),
                title=f"Cloud dip {_day_label(first, now)} {first:%H:%M}-{end:%H:%M}",
                reason=(
                    f"{chunk['cloud_cover'].iloc[start:stop].mean():.0f}% cloud cover is forecast in your peak "
                    f"hours, cutting output from {clear[start:stop].mean() / 1000:.1f} kW to "
                    f"{actual[start:stop].mean() / 1000:.1f} kW and losing {lost:.1f} kWh."
                ),
                kwh_effect=-round(lost, 1),
            )
        )
    return result


def no_production_rule(hourly: pd.DataFrame, now: pd.Timestamp) -> list[Recommendation]:
    """Say so when the forecast has no solar production in the next days."""
    days = _near_term_days(hourly, now)
    if not days or sum(float(chunk["p_ac_w"].sum()) for chunk in days) > 1.0:
        return []
    first = days[0].index[0]
    return [
        Recommendation(
            subtopic="use",
            hour=first.to_pydatetime(),
            title="No solar production forecast",
            reason=f"The weather forecast shows no usable sunshine for the next {len(days)} day(s), "
            "so the system will not produce energy and the load is covered by the battery and the grid.",
            kwh_effect=0.0,
        )
    ]


def recommend(
    request: ForecastRequest,
    hourly: pd.DataFrame,
    daily: pd.DataFrame,
    now: datetime,
    pr: float = DEFAULT_PERFORMANCE_RATIO,
) -> list[Recommendation]:
    """Run every rule and return the recommendations, soonest first, Optimize last."""
    clock = pd.Timestamp(now)
    clock = clock.tz_convert(hourly.index.tz) if clock.tzinfo else clock.tz_localize(hourly.index.tz)
    timely = no_production_rule(hourly, clock) or [
        *warning_rule(hourly, clock),
        *store_rule(hourly, daily, request.battery, request.load.daily_kwh, clock),
        *direct_rule(hourly, request.battery, clock),
        *use_rule(hourly, clock),
    ]
    timely.sort(key=lambda recommendation: recommendation.hour)
    try:
        long_term = optimize_rule(request.lat, request.lon, request.panel_groups, clock.to_pydatetime(), pr)
    except SolarError as error:
        logger.warning("Optimize rule skipped: %s", error)
        long_term = []
    return [*timely, *long_term]