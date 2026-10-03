#!/usr/bin/env python3
"""Take the README screenshots from the running app (development helper, not needed to run AuFor).

Starts the server on a temporary database, drives it with a headless Chromium, posts SIMULATED readings
and saves the pictures to docs/screenshots. Needs Playwright, which is NOT in requirements.txt:

    pip install playwright && python -m playwright install chromium
    python scripts/make_screenshots.py            # real weather (needs Open-Meteo quota and internet)
    python scripts/make_screenshots.py --mock     # bundled mock weather: works offline, clearly labelled MOCK in the page

Every map fetch costs about 150 Open-Meteo calls out of the free 10,000 a day, so do not run this in a loop.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = 8114


def wait_for_server(url: str) -> None:
    for _ in range(100):
        try:
            urllib.request.urlopen(url + "/api/health", timeout=1)
            return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError("the server did not start")


def take(url: str, out: Path, env: dict) -> None:
    from playwright.sync_api import sync_playwright  # noqa: PLC0415 - only needed by this script

    jpeg = {"type": "jpeg", "quality": 88}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(url + "/")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(900)
        page.screenshot(path=str(out / "intro.jpg"), **jpeg)

        page.goto(url + "/calculator")
        page.wait_for_selector("#map-timeline:not([hidden])", timeout=30000)
        page.wait_for_timeout(2500)
        cloudiest = page.evaluate(
            "(() => { let best = 0, top = -1; for (let i = 0; i < cloudField.times.length; i++) "
            "{ const s = conditionsAtSite(i); if (s && s.cloud > top) { top = s.cloud; best = i; } } return best; })()"
        )
        page.evaluate(f"setTimeIndex({cloudiest})")
        page.wait_for_timeout(2200)
        print("cloudiest hour:", page.inner_text("#tl-time"), "|", page.inner_text("#tl-cloud"))
        page.screenshot(path=str(out / "map-clouds-wind.jpg"), **jpeg)
        page.click("#toggle-flow")
        page.wait_for_timeout(2800)
        page.screenshot(path=str(out / "map-wind-flow.jpg"), **jpeg)
        page.click("#toggle-flow")

        subprocess.run([sys.executable, str(ROOT / "scripts" / "fake_readings.py"), "--server", url, "--step-minutes", "10", "--seed", "1"],
                       env=env, capture_output=True, cwd=ROOT)
        page.click("#forecast-button")
        page.wait_for_function("document.getElementById('kpi-energy').textContent !== '—'", timeout=30000)
        page.wait_for_function("document.getElementById('readings-tag').textContent.startsWith('SIMULATED')", timeout=15000)
        page.wait_for_timeout(1500)
        page.evaluate("document.getElementById('results').scrollIntoView()")
        page.wait_for_timeout(1500)
        page.screenshot(path=str(out / "results.png"))
        page.locator("#readings-card").screenshot(path=str(out / "live-readings.png"))
        page.evaluate("document.querySelector('.recommendations').scrollIntoView()")
        page.wait_for_timeout(900)
        box = page.evaluate(
            "(() => { const a = document.querySelector('.recommendations').getBoundingClientRect(), "
            "z = document.querySelector('.explanation').getBoundingClientRect(); return {y: a.top, h: z.bottom - a.top}; })()"
        )
        page.screenshot(path=str(out / "recommendations.png"),
                        clip={"x": 60, "y": max(0, box["y"] - 20), "width": 1320, "height": min(box["h"] + 40, 880)})

        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(500)
        page.click("#theme-toggle")
        page.wait_for_timeout(2500)
        page.screenshot(path=str(out / "map-dark.jpg"), **jpeg)
        browser.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Take the README screenshots.")
    parser.add_argument("--mock", action="store_true", help="use the bundled mock weather (offline)")
    parser.add_argument("--out", default=str(ROOT / "docs" / "screenshots"))
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temp:
        env = {**os.environ, "DATABASE_PATH": str(Path(temp) / "shots.db"), "LLM_ENABLED": "0", "READINGS_API_KEY": ""}
        env["USE_MOCK_WEATHER"] = "1" if args.mock else "0"
        url = f"http://127.0.0.1:{PORT}"
        server = subprocess.Popen([sys.executable, "-m", "uvicorn", "main:app", "--port", str(PORT), "--log-level", "warning"], env=env, cwd=ROOT)
        try:
            wait_for_server(url)
            take(url, out, env)
        finally:
            server.terminate()
            server.wait(timeout=10)
    print("saved:", ", ".join(sorted(p.name for p in out.iterdir())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
