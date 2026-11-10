"""Validated application settings, built from config.yaml (see config.example.yaml)."""

from __future__ import annotations

from datetime import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, model_validator

from .config import ROOT, load_config

DEFAULT_POLL_SECONDS = {"mill_local": 60, "adax_cloud": 300, "melcloud": 300, "tuya_thermostat": 60, "cozytouch": 120}


class Location(BaseModel):
    latitude: float
    longitude: float
    timezone: str = "Europe/Tallinn"


class PriceSettings(BaseModel):
    area: str = "ee"
    slot_minutes: int = 60


class NetworkTariff(BaseModel):
    """Day/night network fee. Defaults match Elektrilevi Võrk 4 time windows."""

    day_eur_kwh: float = 0.0
    night_eur_kwh: float = 0.0
    day_weekdays: list[int] = [0, 1, 2, 3, 4]  # Monday = 0
    day_start: time = time(7, 0)
    day_end: time = time(22, 0)
    holidays_are_night: bool = True


class TariffSettings(BaseModel):
    """All amounts in €/kWh without VAT; VAT is applied to the sum."""

    vat: float = 0.24
    seller_margin_eur_kwh: float = 0.0
    excise_eur_kwh: float = 0.0
    renewable_fee_eur_kwh: float = 0.0
    security_of_supply_fee_eur_kwh: float = 0.0
    balancing_capacity_eur_kwh: float = 0.0
    network: NetworkTariff = NetworkTariff()

    @property
    def configured(self) -> bool:
        return self.network.day_eur_kwh > 0 and self.network.night_eur_kwh > 0


class DeviceSettings(BaseModel):
    type: Literal["mill_local", "adax_cloud", "melcloud", "tuya_thermostat", "cozytouch"]
    enabled: bool = True
    host: str | None = None
    room_name: str | None = None
    device_name: str | None = None  # melcloud / cozytouch: device name in the app; null = first one
    device_id: str | None = None  # tuya: device id (local key in secrets.yaml → tuya → <device name>)
    protocol: float = 3.5  # tuya: local protocol version
    poll_seconds: int | None = None
    power_w: float | None = None  # rated power, used for energy estimates

    @property
    def interval(self) -> int:
        return self.poll_seconds or DEFAULT_POLL_SECONDS[self.type]


class SensorSettings(BaseModel):
    type: Literal["esphome", "dirigera"] = "esphome"
    enabled: bool = True
    host: str | None = None  # esphome
    port: int = 6053  # esphome
    device: str | None = None  # dirigera: the sensor's name in the IKEA Home app

    @model_validator(mode="after")
    def _required(self) -> "SensorSettings":
        if self.type == "esphome" and not self.host:
            raise ValueError("esphome sensor needs host")
        if self.type == "dirigera" and not self.device:
            raise ValueError("dirigera sensor needs device (its name in the IKEA Home app)")
        return self


class DirigeraSettings(BaseModel):
    host: str
    poll_seconds: int = 15


class DoorSettings(BaseModel):
    """Door sensor on a zone's door (open = the room is coupled to the next space, docs/DESIGN.md §1.1)."""

    device: str  # name in the IKEA Home app
    zone: str
    enabled: bool = True


class ZoneSettings(BaseModel):
    name: str
    device: str | None = None
    # Temperature sources in priority order: "sensor:<name>" or "device:<name>". Defaults to the device.
    temp_sources: list[str] = []
    # Default for the UI: does this zone follow a school timetable (kids' rooms)?
    school: bool = False
    # "room": targets are room temperatures from the zone's room sensor; the heater's setpoint is shifted by the
    # current gap between its own sensor and the room sensor. "heater": targets go to the heater as they are.
    control_by: Literal["heater", "room"] = "heater"

    @property
    def room_sensor(self) -> str | None:
        """The room sensor source ("sensor:<name>") a room-controlled zone follows."""
        return next((s for s in self.temp_sources if s.startswith("sensor:")), None)


class HotWaterSettings(BaseModel):
    """Price-aware water heater (Cozytouch): heats only in the day's cheapest hours (maska_heat/hotwater.py)."""

    zone: str = "hot_water"
    hours_per_day: int = 6
    morning_by: time = time(6, 30)  # tank full for the morning: at least morning_hours before this
    morning_hours: int = 3
    evening_from: time = time(12, 0)  # top-up for the evening: at least evening_hours in this span
    evening_by: time = time(19, 0)
    evening_hours: int = 1
    boost_below_pct: float = 30.0  # outside the windows, boost once when hot water drops below this


class SpikeSettings(BaseModel):
    """Pause heating during price spikes (maska_heat/spike.py)."""

    enabled: bool = True
    zones: list[str] = []  # zones that pause: rooms in Smart mode, floor thermostats, the water heater
    ratio: float = 1.4  # a spike hour costs at least this × the day's average price …
    min_price_c: float = 35.0  # … and at least this many cents per kWh (incl. network, fees, VAT)
    max_hours: int = 3  # at most this many spike hours a day (the most expensive)
    preheat: bool = True  # rooms warm up to their comfort max in the (cheaper) hour before
    safety_min_temp: float = 18.0  # a paused room below this heats to its comfort min again
    notify_before_min: int = 60


class NotifySettings(BaseModel):
    enabled: bool = True
    app_url: str | None = None  # how the phones reach the app, for buttons in notifications
    ntfy_server: str = "https://ntfy.sh"
    offline_alert_minutes: int = 30
    quiet_start: time = time(23, 0)
    quiet_end: time = time(6, 30)


class WebSettings(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000


class Settings(BaseModel):
    location: Location
    price: PriceSettings = PriceSettings()
    tariff: TariffSettings = TariffSettings()
    devices: dict[str, DeviceSettings] = {}
    sensors: dict[str, SensorSettings] = {}
    dirigera: DirigeraSettings | None = None
    doors: dict[str, DoorSettings] = {}
    outdoor_sensor: str | None = None
    zones: dict[str, ZoneSettings] = {}
    hot_water: HotWaterSettings | None = None
    spike: SpikeSettings | None = None
    notify: NotifySettings = NotifySettings()
    web: WebSettings = WebSettings()
    database: str = "data/maska_heat.db"

    @model_validator(mode="after")
    def _check_references(self) -> "Settings":
        for key, zone in self.zones.items():
            if zone.device and zone.device not in self.devices:
                raise ValueError(f"zone {key!r}: unknown device {zone.device!r}")
            if not zone.temp_sources and zone.device:
                zone.temp_sources = [f"device:{zone.device}"]
            if zone.control_by == "room" and not (zone.device and any(s.startswith("sensor:") for s in zone.temp_sources)):
                raise ValueError(f"zone {key!r}: control_by room needs a device and a sensor in temp_sources")
            for src in zone.temp_sources:
                kind, _, name = src.partition(":")
                pool = {"device": self.devices, "sensor": self.sensors}.get(kind)
                if pool is None or name not in pool:
                    raise ValueError(f"zone {key!r}: unknown temperature source {src!r}")
        for key, door in self.doors.items():
            if door.zone not in self.zones:
                raise ValueError(f"door {key!r}: unknown zone {door.zone!r}")
        uses_hub = any(s.type == "dirigera" and s.enabled for s in self.sensors.values()) or any(
            d.enabled for d in self.doors.values()
        )
        if uses_hub and self.dirigera is None:
            raise ValueError("dirigera sensors or doors are configured but the dirigera: host is missing")
        if self.hot_water:
            zone = self.zones.get(self.hot_water.zone)
            if zone is None or not zone.device or self.devices[zone.device].type != "cozytouch":
                raise ValueError(f"hot_water: zone {self.hot_water.zone!r} must be a zone with a cozytouch water heater")
            if not 1 <= self.hot_water.hours_per_day <= 24:
                raise ValueError("hot_water: hours_per_day must be 1–24")
        if self.spike:
            for z in self.spike.zones:
                if z not in self.zones:
                    raise ValueError(f"spike: unknown zone {z!r}")
        if self.outdoor_sensor and self.outdoor_sensor not in self.sensors:
            raise ValueError(f"outdoor_sensor: unknown sensor {self.outdoor_sensor!r}")
        return self

    @property
    def database_path(self) -> Path:
        p = Path(self.database)
        return p if p.is_absolute() else ROOT / p


def load_settings(path: Path | None = None) -> Settings:
    return Settings.model_validate(load_config(path))

