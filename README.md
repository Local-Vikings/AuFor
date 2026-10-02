# SolarSight

SolarSight is a hackathon prototype for planning solar production, battery use,
and household energy decisions. The project bible is `bible.md` in the parent
workspace and defines the API, physics, testing, and scope rules.

## Current status

Phase 0 is implemented: the FastAPI service serves the dashboard shell, exposes
`GET /api/health`, validates the forecast and readings contracts, and provides a
deterministic `POST /api/forecast` stub. The forecast response is explicitly
marked as simulated until weather and solar modules are connected.

## Run locally

Use Python 3.11 or newer:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload
```

Open <http://127.0.0.1:8000>.

## Test

```bash
pytest -q
```

Tests are designed to run offline. Do not commit `.env`, the SQLite database,
or real credentials. Use `.env.example` as the starting point for local settings.
