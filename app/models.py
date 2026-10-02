"""Pydantic schemas for the SolarSight API.

This module validates API data and describes response shapes. It does not perform
forecast calculations, access the network, or write to SQLite.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ReadingSource = Literal["battery", "panel", "camera", "simulated"]
ReadingType = Literal["soc_kwh", "power_w", "cloud_fraction"]
Subtopic = Literal["use", "direct", "optimize", "store"]


class PanelConfig(BaseModel):
    """Describe a photovoltaic panel array using the bible's API units."""

    model_config = ConfigDict(extra="forbid")

    count: int = Field(gt=0)
    watt_peak: float = Field(gt=0)
    tilt: float = Field(ge=0, le=90)
    azimuth: float = Field(ge=0, le=360)
    noct: float = Field(gt=0)
    gamma: float = Field(lt=0, ge=-0.1)
    inverter_max_w: float = Field(gt=0)


class BatteryConfig(BaseModel):
    """Describe a battery using kWh, kW, and fractional efficiency units."""

    model_config = ConfigDict(extra="forbid")

    capacity_kwh: float = Field(ge=0)
    dod: float = Field(ge=0, le=1)
    eta_c: float = Field(gt=0, le=1)
    eta_d: float = Field(gt=0, le=1)
    max_power_kw: float = Field(ge=0)
    initial_soc_kwh: float = Field(ge=0)

    @field_validator("initial_soc_kwh")
    @classmethod
    def initial_soc_must_fit_capacity(cls, value: float, info: object) -> float:
        """Reject an initial state of charge above the configured capacity."""
        capacity = info.data.get("capacity_kwh")
        if capacity is not None and value > capacity:
            raise ValueError("initial_soc_kwh must be less than or equal to capacity_kwh")
        return value


class LoadConfig(BaseModel):
    """Describe the average daily household load in kWh."""

    model_config = ConfigDict(extra="forbid")

    daily_kwh: float = Field(gt=0)


class ForecastRequest(BaseModel):
    """Validate the forecast request from the API contract."""

    model_config = ConfigDict(extra="forbid")

    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    panel: PanelConfig
    battery: BatteryConfig
    load: LoadConfig
    days: int = Field(default=3, ge=1, le=7)


class ReadingCreate(BaseModel):
    """Validate a plug-in measurement and require a timezone-aware timestamp."""

    model_config = ConfigDict(extra="forbid")

    source: ReadingSource
    type: ReadingType
    value: float = Field(ge=0)
    timestamp: datetime

    @field_validator("timestamp")
    @classmethod
    def timestamp_must_have_timezone(cls, value: datetime) -> datetime:
        """Reject ambiguous local timestamps; stored times must include an offset."""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must include a timezone offset")
        return value

    @field_validator("value")
    @classmethod
    def cloud_fraction_must_be_fraction(cls, value: float, info: object) -> float:
        """Keep cloud fractions within their documented zero-to-one range."""
        if info.data.get("type") == "cloud_fraction" and value > 1:
            raise ValueError("cloud_fraction value must be between 0 and 1")
        return value


class HourlyForecast(BaseModel):
    """Represent one timezone-aware forecast hour."""

    time: datetime
    ghi: float = Field(ge=0)
    g_poa: float = Field(ge=0)
    t_cell: float
    p_ac_w: float = Field(ge=0)
    load_w: float = Field(ge=0)
    soc_kwh: float = Field(ge=0)
    grid_import_w: float = Field(ge=0)
    grid_export_w: float = Field(ge=0)


class DailyForecast(BaseModel):
    """Represent one daily production summary."""

    date: str
    kwh: float = Field(ge=0)
    self_consumption_pct: float = Field(ge=0, le=100)


class Recommendation(BaseModel):
    """Represent a number-derived energy recommendation."""

    subtopic: Subtopic
    hour: datetime
    title: str
    reason: str
    kwh_effect: float


class ForecastMeta(BaseModel):
    """Describe the data sources and calibration state of a forecast."""

    pr_used: float = Field(ge=0, le=1.05)
    calibrated: bool
    data_sources: list[str]


class ForecastResponse(BaseModel):
    """Represent the complete forecast response consumed by the frontend."""

    hourly: list[HourlyForecast]
    daily: list[DailyForecast]
    recommendations: list[Recommendation]
    explanation: str | None
    meta: ForecastMeta
