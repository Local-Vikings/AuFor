#!/usr/bin/env python3
"""Post SIMULATED power readings so the calibration can be shown without hardware (T52, bible 19.7).

The simulated installation has a fixed true performance ratio: 0.80 * bias (0.92 -> 0.736), so it
produces 8% less than the model assumes. Readings are built from the forecast's DC power (the
forecast power divided by the PR it used), NOT from the calibrated forecast, so the bias does not
compound as the forecast improves. Each reading is  dc_power * true_pr * (1 + N(0, noise)).

Every row is posted with source "simulated" and the page labels it as such. Running this moves the
forecast PR from 0.80 toward 0.736: one update gives about 0.78 and it converges over several
batches of new readings.

Examples:
    python scripts/fake_readings.py                    # post readings for the hours so far, then show the new PR
    python scripts/fake_readings.py --reset            # start again from PR 0.80 first
    python scripts/fake_readings.py --follow           # keep posting a new batch every step
    python scripts/fake_readings.py --config my_request.json --api-key SECRET
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

DEFAULT_PR = 0.80
CLIP_MARGIN = 0.98
EXIT_AUTH = 3

DEFAULT_REQUEST = {
    "lat": 42.6977,
    "lon": 23.3219,
    "panel": {"count": 10, "watt_peak": 400, "tilt": 35, "azimuth": 180, "noct": 45, "gamma": -0.004, "inverter_max_w": 4000},
    "battery": {"capacity_kwh": 10, "dod": 0.9, "eta_c": 0.95, "eta_d": 0.95, "max_power_kw": 5, "initial_soc_kwh": 5},
    "load": {"daily_kwh": 12},
    "days": 3,
    "resolution": "hourly",
}


class AuthError(RuntimeError):
    """The server refused the API key."""


def call(server: str, path: str, body: dict | None, api_key: str | None = None, timeout: float = 30.0) -> tuple[int, dict]:
    """POST (or GET when body is None) and return (status, json). Raises AuthError on 401/403."""
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(server.rstrip("/") + path, data=data, headers=headers, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            raise AuthError("the server refused the API key (use --api-key)") from error
        try:
            return error.code, json.loads(error.read() or b"null")
        except json.JSONDecodeError:
            return error.code, {}


def total_inverter_w(request: dict) -> float:
    groups = request.get("panels") or [request["panel"]]
    return sum(g["inverter_max_w"] for g in groups)


def build_readings(forecast: dict, request: dict, true_pr: float, noise: float, step_minutes: int,
                   now: datetime, rng: random.Random) -> list[dict]:
    """Simulated power readings for every forecast hour that is already over.

    A forecast row labelled T averages the hour BEFORE T, so its readings are timed (T-1h, T].
    """
    used_pr = forecast["meta"]["pr_used"]
    limit = CLIP_MARGIN * total_inverter_w(request)
    steps = max(1, 60 // step_minutes)
    readings: list[dict] = []
    for row in forecast["hourly"]:
        p_ac = row["p_ac_w"]
        if p_ac <= 0 or p_ac >= limit:
            continue  # night, or clipped by the inverter: says nothing about the PR
        dc_power = p_ac / used_pr
        label = datetime.fromisoformat(row["time"]).astimezone(timezone.utc)
        for k in range(1, steps + 1):
            moment = label - timedelta(minutes=60 - k * step_minutes)
            if moment > now:
                continue
            value = max(0.0, dc_power * true_pr * (1.0 + rng.gauss(0.0, noise)))
            readings.append({"source": "simulated", "type": "power_w", "value": round(value, 1), "timestamp": moment.isoformat()})
    return readings


def post_all(server: str, readings: list[dict], api_key: str | None) -> tuple[int, int]:
    """Post readings; returns (new, already_there)."""
    new = known = 0
    for reading in readings:
        status, body = call(server, "/api/readings", reading, api_key)
        if status == 201:
            new += 1
        elif status == 409:
            known += 1
        else:
            raise RuntimeError(f"server rejected a reading ({status}): {body}")
    return new, known


def run_once(args: argparse.Namespace, request: dict, rng: random.Random) -> dict:
    """Post one batch and return the forecast made afterwards (that call performs the calibration update)."""
    _, forecast = call(args.server, "/api/forecast", request, args.api_key)
    before = forecast["meta"]["pr_used"]
    true_pr = DEFAULT_PR * args.bias
    batch = build_readings(forecast, request, true_pr, args.noise, args.step_minutes, datetime.now(timezone.utc), rng)
    new, known = post_all(args.server, batch, args.api_key)
    _, after = call(args.server, "/api/forecast", request, args.api_key)
    meta = after["meta"]
    state = f"calibrated from {meta['calibration_points']} {meta['calibration_source']} readings" if meta["calibrated"] else "not calibrated yet"
    print(f"  posted {new} new simulated readings ({known} already stored); true PR of the fake system {true_pr:.3f}")
    print(f"  forecast PR {before:.4f} -> {meta['pr_used']:.4f} ({state})")
    return after


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Post SIMULATED power readings for the calibration demo.")
    parser.add_argument("--server", default=os.environ.get("AUFOR_SERVER", "http://127.0.0.1:8000"))
    parser.add_argument("--api-key", default=os.environ.get("AUFOR_API_KEY"))
    parser.add_argument("--config", help="JSON file with a forecast request (default: the page's default system)")
    parser.add_argument("--bias", type=float, default=0.92, help="the fake system produces this share of what PR 0.80 predicts")
    parser.add_argument("--noise", type=float, default=0.08, help="standard deviation of the relative noise")
    parser.add_argument("--step-minutes", type=int, default=10, choices=[1, 2, 5, 6, 10, 12, 15, 20, 30, 60], help="minutes between readings")
    parser.add_argument("--follow", action="store_true", help="keep going: post a new batch every step")
    parser.add_argument("--reset", action="store_true", help="forget the learned PR first (start again from 0.80)")
    parser.add_argument("--seed", type=int, default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    request = json.load(open(args.config)) if args.config else DEFAULT_REQUEST
    rng = random.Random(args.seed)
    print(f"SIMULATED readings -> {args.server} (bias {args.bias}, noise {args.noise}, every {args.step_minutes} min)")
    try:
        if args.reset:
            _, body = call(args.server, "/api/calibration/reset", {}, args.api_key)
            print(f"  calibration reset ({body.get('removed', 0)} removed)")
        while True:
            print(time.strftime("%H:%M:%S"))
            run_once(args, request, rng)
            if not args.follow:
                return 0
            time.sleep(args.step_minutes * 60)
    except AuthError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return EXIT_AUTH
    except (urllib.error.URLError, OSError) as error:
        print(f"ERROR: cannot reach {args.server}: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
