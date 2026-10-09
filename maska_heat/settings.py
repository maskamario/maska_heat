"""Validated application settings, built from config.yaml (see config.example.yaml)."""

from __future__ import annotations

from datetime import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, model_validator

from .config import ROOT, load_config

DEFAULT_POLL_SECONDS = {"mill_local": 60, "adax_cloud": 300, "melcloud": 300}


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
    network: NetworkTariff = NetworkTariff()

    @property
    def configured(self) -> bool:
        return self.network.day_eur_kwh > 0 and self.network.night_eur_kwh > 0


class DeviceSettings(BaseModel):
    type: Literal["mill_local", "adax_cloud", "melcloud"]
    enabled: bool = True
    host: str | None = None
    room_name: str | None = None
    device_name: str | None = None
    poll_seconds: int | None = None
    power_w: float | None = None  # rated power, used for energy estimates

    @property
    def interval(self) -> int:
        return self.poll_seconds or DEFAULT_POLL_SECONDS[self.type]


class SensorSettings(BaseModel):
    type: Literal["esphome"] = "esphome"
    enabled: bool = True
    host: str
    port: int = 6053


class ZoneSettings(BaseModel):
    name: str
    device: str | None = None
    # Temperature sources in priority order: "sensor:<name>" or "device:<name>". Defaults to the device.
    temp_sources: list[str] = []


class NotifySettings(BaseModel):
    enabled: bool = True
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
    outdoor_sensor: str | None = None
    zones: dict[str, ZoneSettings] = {}
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
            for src in zone.temp_sources:
                kind, _, name = src.partition(":")
                pool = {"device": self.devices, "sensor": self.sensors}.get(kind)
                if pool is None or name not in pool:
                    raise ValueError(f"zone {key!r}: unknown temperature source {src!r}")
        if self.outdoor_sensor and self.outdoor_sensor not in self.sensors:
            raise ValueError(f"outdoor_sensor: unknown sensor {self.outdoor_sensor!r}")
        return self

    @property
    def database_path(self) -> Path:
        p = Path(self.database)
        return p if p.is_absolute() else ROOT / p


def load_settings(path: Path | None = None) -> Settings:
    return Settings.model_validate(load_config(path))

