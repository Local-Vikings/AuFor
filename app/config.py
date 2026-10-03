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
DEFAULT_ALBEDO = 0.2  # bible 9.2: typical ground reflectance
WEATHER_INTERVAL_SHIFT_S = 1800.0  # Open-Meteo values average the preceding hour (bible 9.1)
FORECAST_DT_H = 1.0
# Hourly household load shape, kWh per hour from 00 to 23 (bible 19.4). It sums to
# 12.0 and is scaled to the requested daily consumption. Evening peak 18-20.
LOAD_PROFILE_KWH = (
    0.25, 0.25, 0.25, 0.25, 0.25, 0.25, 0.40, 0.80, 0.60, 0.40, 0.40, 0.40,
    0.60, 0.50, 0.40, 0.40, 0.40, 0.70, 1.00, 1.10, 1.00, 0.70, 0.40, 0.30,
)
RECOMMEND_DAYS = 3  # rules look at today and the next two days
USE_WINDOW_HOURS = 3  # Use rule: length of the best surplus window
USE_MIN_SURPLUS_KWH = 0.5
STORE_WEAK_FRACTION = 0.7  # Store rule: tomorrow below 70% of the 7-day average
STORE_AVERAGE_DAYS = 7
DIRECT_FULL_BEFORE_HOUR = 12  # Direct rule: battery full before noon
DIRECT_MIN_EXPORT_KWH = 1.0
WARN_PEAK_FRACTION = 0.6  # Warning rule: peak hours = clear-sky power above 60% of the day's peak
WARN_DIP_FRACTION = 0.6  # a dip is output below 60% of the clear-sky output
WARN_MIN_HOURS = 2
WARN_MIN_LOSS_KWH = 0.5
CLOUD_FIELD_COLS = 15  # map cloud overlay: 15 x 9 forecast points over the visible map
CLOUD_FIELD_ROWS = 9
CLOUD_FIELD_SNAP_DEG = 0.25  # bounds snap outward to this grid so small pans reuse the cache
CLOUD_FIELD_MAX_LAT_SPAN_DEG = 40.0
CLOUD_FIELD_MAX_LON_SPAN_DEG = 90.0
CLOUD_FIELD_SITE_SPAN_DEG = 1.2  # lat/lon request: about 130 km each side of the site
CLOUD_FIELD_CACHE_SECONDS = 1800.0
OPT_TILT_STEP_DEG = 5.0  # orientation search grid for the Optimize rule (T20)
OPT_AZIMUTH_STEP_DEG = 10.0
WEATHER_TIMEOUT_SECONDS = 10.0
OPEN_METEO_MAX_FORECAST_DAYS = 16  # Open-Meteo forecast API limit
CLOUD_FIELD_MAX_HOURS = OPEN_METEO_MAX_FORECAST_DAYS * 24  # the whole forecast range
MAX_HORIZON_DAYS = 365
MAX_HOURLY_RESOLUTION_DAYS = 31
CARTO_API_KEY = os.getenv("CARTO_API_KEY", "")

USE_MOCK_WEATHER = os.getenv("USE_MOCK_WEATHER", "0") == "1"
WEATHER_PROVIDER = os.getenv("WEATHER_PROVIDER", "auto")
TOMORROW_API_KEY = os.getenv("TOMORROW_API_KEY", "")
LLM_ENABLED = os.getenv("LLM_ENABLED", "0") == "1"
DATABASE_PATH = os.getenv("DATABASE_PATH", "data/readings.db")
READING_FUTURE_TOLERANCE_S = 300  # a sensor clock may run a few minutes ahead
READINGS_MAX_HOURS = 24 * 30
REAL_READING_SOURCES = ("battery", "panel", "camera")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
SOLAR_LIB_PATH = os.getenv("SOLAR_LIB_PATH", "")
