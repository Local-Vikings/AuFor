#!/usr/bin/env python3
"""Sky-camera agent for the Raspberry Pi: photo -> cloud fraction -> AuFor server.

Runs on the machine that has the camera and tx's PyTorch model. It takes a sky photo,
measures the cloud fraction with cloud_predictor.run (the same code tx wrote), and POSTs
{"source": "camera", "type": "cloud_fraction", "value": 0..1} to /api/readings.

The network is allowed to fail: a reading that cannot be delivered is appended to a
buffer file and sent on a later cycle, oldest first. Only the standard library is used
for networking, so the agent runs with torch and OpenCV as its only extra installs.

Examples (run from the repository root so cloud_predictor can be imported):
    python scripts/pi_cloud_agent.py --server http://192.168.1.20:8000 --api-key SECRET \\
        --capture-cmd "rpicam-still -n -t 500 -o {path}" --interval 120
    python scripts/pi_cloud_agent.py --image test_images/blue-sky.png --once
    python scripts/pi_cloud_agent.py --free-percent 9.1 --once      # no model, no camera: just post a number
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

DEFAULT_RADIUS_PX = 150
DEFAULT_BUFFER = "pending_readings.jsonl"
MAX_BUFFERED = 5000
EXIT_AUTH = 3


def local_api_key() -> str | None:
    """The key for writes: AUFOR_API_KEY, else READINGS_API_KEY from the environment or the repository's .env
    or data/.env (the server writes a generated key there at start-up, so scripts on the same machine just work)."""
    for name in ("AUFOR_API_KEY", "READINGS_API_KEY"):
        if os.environ.get(name, "").strip():
            return os.environ[name].strip()
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for env_file in (os.path.join(root, ".env"), os.path.join(root, "data", ".env")):  # data/.env: the Docker setup
        try:
            with open(env_file) as handle:
                for line in handle:
                    name, _, value = line.strip().partition("=")
                    if name == "READINGS_API_KEY" and value.strip() and value.strip().lower() != "off":
                        return value.strip()
        except OSError:
            continue
    return None


class AuthError(RuntimeError):
    """The server refused the API key. This is a configuration problem, not a network one."""


def free_to_cloud_fraction(free_percent: float) -> float:
    """The model reports the clear-sky percentage; the API stores the cloud fraction (0 = clear, 1 = overcast)."""
    if not 0.0 <= free_percent <= 100.0:
        raise ValueError(f"free percent must be between 0 and 100, got {free_percent}")
    return round(1.0 - free_percent / 100.0, 3)


def make_reading(cloud_fraction: float, when: datetime | None = None) -> dict:
    """The JSON body for POST /api/readings, timestamped in UTC."""
    moment = (when or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0)
    return {"source": "camera", "type": "cloud_fraction", "value": cloud_fraction, "timestamp": moment.isoformat()}


def post_reading(server: str, reading: dict, api_key: str | None = None, timeout: float = 10.0) -> str:
    """Send one reading. Returns "sent", "duplicate", "rejected" or "retry".

    "retry" means the reading should be kept and sent later (network trouble, server error, rate limit).
    Raises AuthError when the server refuses the key.
    """
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    request = urllib.request.Request(
        server.rstrip("/") + "/api/readings", data=json.dumps(reading).encode(), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return "sent" if response.status == 201 else "retry"
    except urllib.error.HTTPError as error:
        if error.code == 409:
            return "duplicate"  # the server already has this reading
        if error.code in (401, 403):
            raise AuthError("the server refused the API key (check --api-key / AUFOR_API_KEY)") from error
        if error.code == 429 or error.code >= 500:
            return "retry"
        detail = error.read().decode(errors="replace")[:200]
        print(f"  server rejected this reading ({error.code}): {detail}", file=sys.stderr)
        return "rejected"  # bad data (for example a wrong Pi clock): keeping it would never help
    except (urllib.error.URLError, TimeoutError, OSError):
        return "retry"


def buffer_reading(path: str, reading: dict) -> None:
    """Append a reading to the buffer file, keeping at most MAX_BUFFERED (oldest dropped)."""
    lines = _read_buffer(path) + [json.dumps(reading)]
    _write_buffer(path, lines[-MAX_BUFFERED:])


def _read_buffer(path: str) -> list[str]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as file:
        return [line.strip() for line in file if line.strip()]


def _write_buffer(path: str, lines: list[str]) -> None:
    if not lines:
        if os.path.exists(path):
            os.remove(path)
        return
    with open(path, "w", encoding="utf-8") as file:
        file.write("\n".join(lines) + "\n")


def flush_buffer(path: str, server: str, api_key: str | None = None) -> tuple[int, int]:
    """Send buffered readings oldest first, stopping at the first one that needs a retry.

    Returns (delivered, still_buffered).
    """
    pending = _read_buffer(path)
    delivered = 0
    while pending:
        try:
            reading = json.loads(pending[0])
        except json.JSONDecodeError:
            pending.pop(0)  # a corrupt line can never be sent
            continue
        outcome = post_reading(server, reading, api_key)
        if outcome == "retry":
            break
        pending.pop(0)
        delivered += outcome in ("sent", "duplicate")
    _write_buffer(path, pending)
    return delivered, len(pending)


def capture(command: str, timeout: float = 60.0) -> str:
    """Run a camera command (with {path} in it) and return the path of the photo it wrote."""
    handle, path = tempfile.mkstemp(suffix=".jpg")
    os.close(handle)
    parts = [part.replace("{path}", path) for part in shlex.split(command)]
    subprocess.run(parts, check=True, timeout=timeout, capture_output=True)
    if not os.path.getsize(path):
        raise RuntimeError("the camera command produced an empty photo")
    return path


def measure(image_path: str, radius: int) -> float:
    """Free-sky percentage of a photo, using tx's model. Needs torch, OpenCV and checkpoints/best.pth."""
    sys.path.insert(0, os.getcwd())
    try:
        import cloud_predictor  # noqa: PLC0415 - heavy import, only needed when a photo is measured
    except ImportError as error:
        raise RuntimeError(f"cannot load the model ({error}); run from the repository root with torch and OpenCV installed") from error
    return float(cloud_predictor.run(image_path, radius, "latest")[0])


def cycle(args: argparse.Namespace) -> str:
    """One round: flush the buffer, take and measure a photo, send the reading (or buffer it)."""
    delivered, left = flush_buffer(args.buffer, args.server, args.api_key)
    if delivered or left:
        print(f"  buffer: {delivered} delivered, {left} still waiting")

    temporary = None
    try:
        if args.free_percent is not None:
            free = args.free_percent
        elif args.image:
            free = measure(args.image, args.radius)
        else:
            temporary = capture(args.capture_cmd)
            free = measure(temporary, args.radius)
        fraction = free_to_cloud_fraction(free)
        reading = make_reading(fraction)
        print(f"  free sky {free:.1f}% -> cloud_fraction {fraction}")
        if args.dry_run:
            return "dry-run"
        outcome = post_reading(args.server, reading, args.api_key)
        if outcome == "retry":
            buffer_reading(args.buffer, reading)
            print("  server not reachable: reading buffered for later")
        else:
            print(f"  server: {outcome}")
        return outcome
    finally:
        if temporary and os.path.exists(temporary):
            os.remove(temporary)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Measure cloud cover from a sky photo and send it to AuFor.")
    parser.add_argument("--server", default=os.environ.get("AUFOR_SERVER", "http://127.0.0.1:8000"))
    parser.add_argument("--api-key", default=local_api_key(), help="default: AUFOR_API_KEY or READINGS_API_KEY from .env")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--image", help="measure this photo instead of taking one")
    source.add_argument("--capture-cmd", help='camera command with {path}, e.g. "rpicam-still -n -t 500 -o {path}"')
    source.add_argument("--free-percent", type=float, help="skip the model and send this free-sky percentage (testing)")
    parser.add_argument("--radius", type=int, default=DEFAULT_RADIUS_PX, help="circle radius in px (keep it the same every time)")
    parser.add_argument("--interval", type=float, default=120.0, help="seconds between photos")
    parser.add_argument("--buffer", default=DEFAULT_BUFFER, help="file for readings that could not be sent")
    parser.add_argument("--once", action="store_true", help="do one round and exit")
    parser.add_argument("--dry-run", action="store_true", help="measure but do not send")
    args = parser.parse_args(argv)
    if args.free_percent is None and not (args.image or args.capture_cmd):
        parser.error("give --image, --capture-cmd or --free-percent")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    print(f"AuFor camera agent -> {args.server} (every {args.interval:.0f}s, radius {args.radius}px)")
    while True:
        print(time.strftime("%H:%M:%S"))
        try:
            cycle(args)
        except AuthError as error:
            print(f"  ERROR: {error}", file=sys.stderr)
            return EXIT_AUTH
        except Exception as error:  # noqa: BLE001 - a bad photo or a camera hiccup must not kill the agent
            print(f"  skipped this round: {type(error).__name__}: {error}", file=sys.stderr)
            if args.once:
                return 1
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
