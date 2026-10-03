"""Application defaults and environment-backed settings.

This module stores shared configuration only. It does not fetch weather, perform
solar calculations, or persist readings.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

SOFIA_LAT = 42.6977
SOFIA_LON = 23.3219
DEFAULT_PANEL_COUNT = 10
DEFAULT_WATT_PEAK = 400.0
DEFAULT_TILT_DEG = 35.0
DEFAULT_AZIMUTH_DEG = 180.0
DEFAULT_NOCT_C = 45.0
DEFAULT_TEMPERATURE_COEFFICIENT = -0.004
DEFAULT_INVERTER_MAX_W = 4000.0
DEFAULT_BATTERY_CAPACITY_KWH = 10.0
DEFAULT_BATTERY_DOD = 0.9
DEFAULT_CHARGE_EFFICIENCY = 0.95
DEFAULT_DISCHARGE_EFFICIENCY = 0.95
DEFAULT_BATTERY_MAX_POWER_KW = 5.0
DEFAULT_INITIAL_SOC_KWH = 5.0
DEFAULT_DAILY_LOAD_KWH = 12.0
DEFAULT_PERFORMANCE_RATIO = 0.8
WEATHER_TIMEOUT_SECONDS = 10.0
CARTO_API_KEY = os.getenv("CARTO_API_KEY", "")

USE_MOCK_WEATHER = os.getenv("USE_MOCK_WEATHER", "0") == "1"
WEATHER_PROVIDER = os.getenv("WEATHER_PROVIDER", "auto")
TOMORROW_API_KEY = os.getenv("TOMORROW_API_KEY", "")
LLM_ENABLED = os.getenv("LLM_ENABLED", "0") == "1"
DATABASE_PATH = os.getenv("DATABASE_PATH", "data/readings.db")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
