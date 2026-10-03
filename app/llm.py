"""Plain-language explanation of a forecast (bible 15).

The LLM only explains numbers it is given; it never produces forecast numbers. Everything
here fails soft: without a key, or when the call fails, times out, answers badly or
invents a number, the deterministic template text is returned instead. explain() never raises.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Any

import httpx

from app import config

logger = logging.getLogger(__name__)

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
SYSTEM_PROMPT = (
    "You explain a solar-energy forecast to a home owner in 3 to 5 plain sentences. "
    "Do not invent numbers. Use only the data given. Do not give forecasts of your own. "
    "Mention the main number, what the weather does to it, and the most useful action."
)
MAX_RECOMMENDATIONS = 4
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
_MEMORY: "OrderedDict[str, tuple[float, dict]]" = OrderedDict()
_ANSWERS: "OrderedDict[str, str]" = OrderedDict()  # LLM text per summary: the same forecast is paid for once
_CALL_TIMES: "deque[float]" = deque()  # model calls in the last hour, for LLM_MAX_CALLS_PER_HOUR


@dataclass(frozen=True)
class Explanation:
    """The text and where it came from: "llm" or "template"."""

    text: str
    source: str


def available() -> bool:
    """True when an LLM is switched on and has a key."""
    return config.LLM_ENABLED and bool(config.LLM_API_KEY)


# ---- summary: the only thing the LLM ever sees ----


def _best_worst(rows: list[Any], label: str) -> tuple[dict, dict]:
    best = max(rows, key=lambda r: r.kwh)
    worst = min(rows, key=lambda r: r.kwh)
    return ({"label": getattr(best, label), "kwh": round(best.kwh, 1)},
            {"label": getattr(worst, label), "kwh": round(worst.kwh, 1)})


def build_summary(request: Any, response: Any) -> dict:
    """Small JSON describing a forecast response; every number in an explanation comes from here."""
    monthly = bool(response.monthly)
    rows = response.monthly if monthly else response.daily
    total = sum(r.kwh for r in rows)
    clear = sum(r.clear_sky_kwh for r in rows)
    days = sum(r.days for r in rows) if monthly else len(rows)
    best, worst = _best_worst(rows, "month" if monthly else "date")
    cloud = sum(r.avg_cloud_cover * r.clear_sky_kwh for r in rows) / clear if clear > 0 else 0.0
    kept = sum(r.self_consumption_pct * r.kwh for r in rows) / total if total > 0 else 0.0
    return {
        "period": {"days": days, "resolution": request.resolution},
        "system": {
            "solar_kwp": round(sum(g.count * g.watt_peak for g in request.panel_groups) / 1000, 1),
            "battery_kwh": request.battery.capacity_kwh,
            "daily_load_kwh": request.load.daily_kwh,
        },
        "energy": {
            "total_kwh": round(total, 1),
            "average_kwh_per_day": round(total / days, 1) if days else 0.0,
            "best": best,
            "worst": worst,
            "clear_sky_kwh": round(clear, 1),
            "lost_to_clouds_kwh": round(max(clear - total, 0.0), 1),
            "lost_to_clouds_pct": round(100 * max(clear - total, 0.0) / clear) if clear > 0 else 0,
            "average_cloud_cover_pct": round(cloud),
            "kept_on_site_pct": round(kept),
        },
        "recommendations": [
            {"type": r.subtopic, "title": r.title, "kwh_effect": r.kwh_effect, "reason": r.reason}
            for r in response.recommendations[:MAX_RECOMMENDATIONS]
        ],
        "data_sources": response.meta.data_sources,
    }


# ---- template: always available, deterministic ----


def _sky(cloud_pct: float) -> str:
    return "mostly clear" if cloud_pct < 20 else "partly cloudy" if cloud_pct < 60 else "mostly cloudy"


def _effect(recommendation: dict) -> str:
    """One short phrase for what a recommendation is worth, from its kWh number."""
    kwh = abs(recommendation["kwh_effect"])
    return {
        "warning": f"about {kwh} kWh at risk",
        "optimize": f"up to {kwh} kWh a year with clear skies",
        "store": f"avoids about {kwh} kWh of grid import",
    }.get(recommendation["type"], f"about {kwh} kWh of solar surplus")


def _top_steps(recommendations: list[dict]) -> list[dict]:
    """The first timely action, plus the long-term Optimize card when there is one."""
    timely = [r for r in recommendations if r["type"] != "optimize"]
    optimize = [r for r in recommendations if r["type"] == "optimize"]
    return timely[:1] + optimize[:1] if optimize else timely[:2]


def template_explanation(summary: dict) -> str:
    """3 to 5 sentences built only from the summary numbers."""
    period, system, energy = summary["period"], summary["system"], summary["energy"]
    unit = "month" if period["resolution"] == "monthly" else "day"
    sentences = [
        f"Over {period['days']} day{'s' if period['days'] != 1 else ''} your {system['solar_kwp']} kWp system "
        f"is forecast to produce {energy['total_kwh']} kWh, about {energy['average_kwh_per_day']} kWh a day.",
        f"The best {unit} is {energy['best']['label']} with {energy['best']['kwh']} kWh and the weakest is "
        f"{energy['worst']['label']} with {energy['worst']['kwh']} kWh.",
    ]
    if energy["lost_to_clouds_pct"] >= 5:
        sentences.append(
            f"Skies are {_sky(energy['average_cloud_cover_pct'])} (about {energy['average_cloud_cover_pct']}% cloud in daylight), "
            f"which costs {energy['lost_to_clouds_kwh']} kWh, {energy['lost_to_clouds_pct']}% less than a clear sky would give."
        )
    else:
        sentences.append(f"Skies are {_sky(energy['average_cloud_cover_pct'])}, so clouds barely reduce the output.")
    sentences.append(
        f"About {energy['kept_on_site_pct']}% of the solar energy stays on site against a household use of "
        f"{system['daily_load_kwh']} kWh a day."
    )
    steps = _top_steps(summary["recommendations"])
    if steps:
        listed = "; ".join(f"{step['title']} ({_effect(step)})" for step in steps)
        sentences.append(f"Most useful next step{'s' if len(steps) > 1 else ''}: {listed}.")
    return " ".join(sentences)


# ---- the model call, guarded ----


def _numbers(text: str) -> list[float]:
    return [float(token.replace(",", ".")) for token in _NUMBER.findall(text)]


def is_grounded(text: str, summary: dict) -> bool:
    """True when every number in ``text`` matches a number in the summary (within rounding)."""
    allowed = _numbers(json.dumps(summary))
    return all(any(abs(n - a) <= max(0.06, 0.012 * abs(a)) for a in allowed) for n in _numbers(text))


def _call_model(summary: dict) -> str:
    """Ask the model for an explanation. Raises on any failure; the caller falls back."""
    with httpx.Client(timeout=config.LLM_TIMEOUT_SECONDS) as client:
        response = client.post(
            ANTHROPIC_URL,
            headers={"x-api-key": config.LLM_API_KEY, "anthropic-version": ANTHROPIC_VERSION},
            json={
                "model": config.LLM_MODEL,
                "max_tokens": config.LLM_MAX_TOKENS,
                "system": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": json.dumps(summary)}],
            },
        )
        response.raise_for_status()
        return str(response.json()["content"][0]["text"]).strip()


def _within_budget() -> bool:
    """Count a model call against LLM_MAX_CALLS_PER_HOUR; False when the hour's budget is spent."""
    now = time.monotonic()
    while _CALL_TIMES and now - _CALL_TIMES[0] > 3600:
        _CALL_TIMES.popleft()
    if len(_CALL_TIMES) >= config.LLM_MAX_CALLS_PER_HOUR:
        return False
    _CALL_TIMES.append(now)
    return True


def explain(summary: dict) -> Explanation:
    """Explain a forecast summary; falls back to the template. Never raises."""
    fallback = Explanation(template_explanation(summary), "template")
    if not available():
        return fallback
    key = json.dumps(summary, sort_keys=True)
    if key in _ANSWERS:
        return Explanation(_ANSWERS[key], "llm")
    if not _within_budget():
        logger.warning("LLM budget of %d calls an hour is spent; using the template.", config.LLM_MAX_CALLS_PER_HOUR)
        return fallback
    try:
        text = str(_call_model(summary)).strip()
    except Exception as error:  # noqa: BLE001 - any failure must fall back, never crash the page
        logger.warning("LLM explanation failed (%s: %s); using the template.", type(error).__name__, error)
        return fallback
    if not text or not is_grounded(text, summary):
        logger.warning("LLM explanation was empty or contained numbers that are not in the data; using the template.")
        return fallback
    _ANSWERS[key] = text
    while len(_ANSWERS) > config.EXPLAIN_CACHE_SIZE:
        _ANSWERS.popitem(last=False)
    return Explanation(text, "llm")


# ---- remember a summary so the page can ask for the AI text after the forecast ----


def remember(summary: dict) -> str:
    """Keep a summary for a while and return its id."""
    now = time.monotonic()
    for key in [k for k, (stamp, _) in _MEMORY.items() if now - stamp > config.EXPLAIN_CACHE_SECONDS]:
        del _MEMORY[key]
    key = uuid.uuid4().hex
    _MEMORY[key] = (now, summary)
    while len(_MEMORY) > config.EXPLAIN_CACHE_SIZE:
        _MEMORY.popitem(last=False)
    return key


def recall(key: str) -> dict | None:
    """The summary stored under ``key``, or None if unknown or expired."""
    entry = _MEMORY.get(key)
    if entry is None or time.monotonic() - entry[0] > config.EXPLAIN_CACHE_SECONDS:
        _MEMORY.pop(key, None)
        return None
    return entry[1]
