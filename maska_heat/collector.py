"""Background jobs of phase 1: poll devices and sensors, fetch prices and weather, watch source health.

Read-only — nothing here ever writes to a heating device.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from .adapters.base import DeviceAdapter
from .db import Database, Reading
from .notify import Notifier
from .providers.prices import fetch_prices
from .providers.weather import fetch_forecast
from .sensors.dirigera import DirigeraHub, DirigeraPoller, DirigeraSensor, HubError
from .sensors.esphome import EsphomeSensor
from .settings import DeviceSettings, Settings

log = logging.getLogger(__name__)

SENSOR_LOG_SECONDS = 60
SENSOR_STALE_SECONDS = 300
PRICE_CHECK_SECONDS = 15 * 60
WEATHER_SECONDS = 60 * 60
HEALTH_SECONDS = 60
ENERGY_SECONDS = 60 * 60
ENERGY_DAYS = 31  # daily totals kept up to date; MELCloud answers per day up to 31 days
ENERGY_HOURLY_DAYS = 13  # MELCloud keeps hourly data for about two weeks
NEXT_DAY_PRICES_AFTER = (13, 30)  # local time; day-ahead results are published ~14:00


def build_adapter(name: str, cfg: DeviceSettings, secrets: dict[str, Any]) -> DeviceAdapter:
    """Create the adapter for a configured device. Raises ValueError when configuration is incomplete."""
    if cfg.type == "mill_local":
        from .adapters.mill_local import MillLocalAdapter

        if not cfg.host:
            raise ValueError(f"device {name}: host missing")
        return MillLocalAdapter(cfg.host)
    if cfg.type == "adax_cloud":
        from .adapters.adax_cloud import AdaxCloudAdapter

        adax = secrets.get("adax") or {}
        if not adax.get("account_id") or not adax.get("api_secret"):
            raise ValueError(f"device {name}: adax account_id/api_secret missing in secrets.yaml")
        return AdaxCloudAdapter(adax["account_id"], adax["api_secret"], room_name=cfg.room_name)
    if cfg.type == "melcloud":
        from .adapters.melcloud import MelcloudAdapter

        mel = secrets.get("melcloud") or {}
        if not mel.get("email") or not mel.get("password"):
            raise ValueError(f"device {name}: melcloud email/password missing in secrets.yaml")
        return MelcloudAdapter(mel["email"], mel["password"], device_name=cfg.device_name)
    if cfg.type == "tuya_thermostat":
        from .adapters.tuya_thermostat import TuyaThermostatAdapter

        key = (secrets.get("tuya") or {}).get(name)
        if not cfg.host or not cfg.device_id:
            raise ValueError(f"device {name}: host and device_id are needed")
        if not key:
            raise ValueError(f"device {name}: local key missing in secrets.yaml → tuya → {name}")
        return TuyaThermostatAdapter(cfg.device_id, cfg.host, key, version=cfg.protocol)
    if cfg.type == "cozytouch":
        from .adapters.cozytouch import CozytouchWaterHeaterAdapter

        cz = secrets.get("cozytouch") or {}
        if not cz.get("email") or not cz.get("password"):
            raise ValueError(f"device {name}: cozytouch email/password missing in secrets.yaml")
        return CozytouchWaterHeaterAdapter(cz["email"], cz["password"], device_name=cfg.device_name)
    raise ValueError(f"device {name}: unknown type {cfg.type}")


@dataclass
class SourceHealth:
    label: str
    last_ok: float
    failing: bool = False
    last_error: str | None = None
    alerted: bool = False


class Collector:
    def __init__(self, settings: Settings, secrets: dict[str, Any], db: Database, notifier: Notifier):
        self.settings = settings
        self.db = db
        self.notifier = notifier
        self.tz = ZoneInfo(settings.location.timezone)
        self.devices: dict[str, DeviceAdapter] = {}
        self.sensors: dict[str, EsphomeSensor | DirigeraSensor] = {}
        self.dirigera: DirigeraPoller | None = None
        self.health: dict[str, SourceHealth] = {}
        self._tasks: list[asyncio.Task] = []
        self._energy_done: set[tuple[str, date]] = set()  # past days whose energy report is stored
        now = time.time()

        for name, cfg in settings.devices.items():
            if not cfg.enabled:
                continue
            try:
                self.devices[name] = build_adapter(name, cfg, secrets)
                self.health[f"device:{name}"] = SourceHealth(name, now)
            except ValueError as e:
                log.warning("%s", e)
                db.add_event("warning", f"device:{name}", str(e))

        keys = secrets.get("esphome") or {}
        for name, cfg in settings.sensors.items():
            if cfg.enabled and cfg.type == "esphome":
                self.sensors[name] = EsphomeSensor(name, cfg.host, keys.get(name), cfg.port)
                self.health[f"sensor:{name}"] = SourceHealth(name, now)
        self._setup_dirigera(secrets, now)

    def _setup_dirigera(self, secrets: dict[str, Any], now: float) -> None:
        hub_sensors = [
            DirigeraSensor(name, cfg.device)
            for name, cfg in self.settings.sensors.items()
            if cfg.enabled and cfg.type == "dirigera"
        ]
        doors = {name: d.device for name, d in self.settings.doors.items() if d.enabled}
        if not hub_sensors and not doors:
            return
        token = (secrets.get("dirigera") or {}).get("token")
        if not token:
            msg = "dirigera: token missing in secrets.yaml — hub sensors and doors are not read"
            log.warning("%s", msg)
            self.db.add_event("warning", "dirigera", msg)
            return
        cfg = self.settings.dirigera
        self.dirigera = DirigeraPoller(DirigeraHub(cfg.host, token), hub_sensors, doors, self._door_changed, cfg.poll_seconds)
        self.health["dirigera"] = SourceHealth("DIRIGERA hub", now)
        for s in hub_sensors:
            self.sensors[s.name] = s
            self.health[f"sensor:{s.name}"] = SourceHealth(s.name, now)
        for name in doors:
            self.health[f"door:{name}"] = SourceHealth(f"{name} door sensor", now)

    # jobs ---------------------------------------------------------------------

    async def poll_device(self, name: str) -> None:
        source = f"device:{name}"
        try:
            s = await self.devices[name].read()
        except Exception as e:
            self._failed(source, e)
            return
        power_w = s.power_w
        rated = self.settings.devices[name].power_w
        if power_w is None and rated and s.is_heating is not None:  # on/off heater: rated power while heating
            power_w = rated if s.is_heating else 0.0
        self.db.add_readings(
            [
                Reading(
                    int(time.time()), source, s.room_temp, s.setpoint, s.power_on, s.is_heating, power_w, s.energy_kwh,
                    mode=s.mode, floor_temp=s.floor_temp, level_pct=s.level_pct,
                )
            ]
        )
        self._ok(source)

    async def update_energy(self) -> None:
        """Energy from devices with an energy report (MELCloud): daily totals for the last month (MELCloud keeps
        correcting recent days, so they are refetched each time) and the hourly profile for the days it still has.
        """
        today = datetime.now(self.tz).date()
        for name, adapter in self.devices.items():
            if not hasattr(adapter, "energy_days"):
                continue
            source, health = f"device:{name}", f"energy:{name}"
            try:
                days = await adapter.energy_days(today - timedelta(days=ENERGY_DAYS - 1), today, self.tz)
                self.db.upsert_energy(source, [(int(t.timestamp()), 24 * 60, kwh) for t, kwh in days])
                for d in self._hourly_days_to_fetch(source, today):
                    hours = await adapter.energy_hours(d, self.tz)
                    self.db.upsert_energy(source, [(int(t.timestamp()), 60, kwh) for t, kwh in hours])
                    if d < today - timedelta(days=1):
                        self._energy_done.add((source, d))  # even if empty — don't ask again
            except Exception as e:
                self._failed(health, e)
                continue
            self._ok(health)

    def _hourly_days_to_fetch(self, source: str, today: date) -> list[date]:
        """Today and yesterday (still being corrected), plus earlier days in MELCloud's hourly window with no data."""
        days = [today, today - timedelta(days=1)]
        for back in range(2, ENERGY_HOURLY_DAYS):
            d = today - timedelta(days=back)
            if (source, d) in self._energy_done:
                continue
            start = datetime.combine(d, datetime.min.time(), self.tz)
            rows = self.db.energy(source, int(start.timestamp()), int((start + timedelta(days=1)).timestamp()))
            if any(r["minutes"] == 60 for r in rows):
                self._energy_done.add((source, d))
            else:
                days.append(d)
        return days

    async def poll_dirigera(self) -> None:
        try:
            await self.dirigera.poll()
        except HubError as e:
            self._failed("dirigera", e)
            return
        self._ok("dirigera")
        for name, error in self.dirigera.door_errors.items():
            if error:
                self._failed(f"door:{name}", error)
            else:
                self._ok(f"door:{name}")

    def _door_changed(self, name: str, ts: int, is_open: bool) -> None:
        self.db.add_door_state(f"door:{name}", ts, is_open)

    async def log_sensors(self) -> None:
        now = time.time()
        rows = []
        for name, sensor in self.sensors.items():
            source = f"sensor:{name}"
            if sensor.updated_at and now - sensor.updated_at < SENSOR_STALE_SECONDS:
                rows.append(Reading(int(now), source, temp=sensor.temp, humidity=sensor.humidity))
                self._ok(source)
            elif not sensor.connected and sensor.last_error:  # not yet tried is not a failure
                self._failed(source, sensor.last_error or "not connected")
        self.db.add_readings(rows)

    async def update_prices(self, force: bool = False) -> None:
        now = datetime.now(self.tz)
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        tomorrow, day_after = today + timedelta(days=1), today + timedelta(days=2)
        known_until = self.db.last_price_ts() or 0
        if not force:
            if known_until >= day_after.timestamp():
                return
            if known_until >= tomorrow.timestamp() and (now.hour, now.minute) < NEXT_DAY_PRICES_AFTER:
                return
        try:
            points = await fetch_prices(today, day_after, area=self.settings.price.area)
        except Exception as e:
            self._failed("prices", e)
            return
        self.db.upsert_prices([(int(p.start.timestamp()), p.minutes, p.eur_mwh) for p in points])
        self._ok("prices")
        new_until = self.db.last_price_ts() or 0
        if new_until > known_until and known_until:
            until_local = datetime.fromtimestamp(new_until, self.tz)
            self.db.add_event("info", "prices", f"prices known until {until_local:%a %d.%m %H:%M}")

    async def update_weather(self) -> None:
        loc = self.settings.location
        try:
            points = await fetch_forecast(loc.latitude, loc.longitude, days=2)
        except Exception as e:
            self._failed("weather", e)
            return
        issued = int(time.time())
        self.db.upsert_weather(
            [(int(p.start.timestamp()), issued, p.temp_c, p.wind_ms, p.radiation_wm2, p.cloud_pct) for p in points]
        )
        self._ok("weather")

    async def check_health(self) -> None:
        limit = self.settings.notify.offline_alert_minutes * 60
        now = time.time()
        for source, h in self.health.items():
            if h.failing and not h.alerted and now - h.last_ok > limit:
                h.alerted = True
                minutes = int((now - h.last_ok) // 60)
                msg = f"{h.label} has not answered for {minutes} min ({h.last_error})"
                self.db.add_event("error", source, msg)
                await self.notifier.send("offline", f"{h.label} offline", msg, priority="high", tags="warning")

    # health bookkeeping -------------------------------------------------------

    def _ok(self, source: str) -> None:
        h = self.health.setdefault(source, SourceHealth(source.split(":")[-1], time.time()))
        if h.failing:
            self.db.add_event("info", source, f"{h.label} is answering again")
            if h.alerted:
                asyncio.get_running_loop().create_task(
                    self.notifier.send("online", f"{h.label} back online", f"{h.label} is answering again", tags="white_check_mark")
                )
        h.last_ok, h.failing, h.last_error, h.alerted = time.time(), False, None, False

    def _failed(self, source: str, error: Exception | str) -> None:
        h = self.health.setdefault(source, SourceHealth(source.split(":")[-1], time.time()))
        text = str(error) or type(error).__name__
        if not h.failing:
            log.warning("%s failed: %s", source, text)
            self.db.add_event("warning", source, f"{h.label}: {text}")
        h.failing, h.last_error = True, text

    # lifecycle ----------------------------------------------------------------

    def start(self) -> None:
        def every(seconds: float, job: Callable[[], Awaitable[None]], name: str) -> asyncio.Task:
            async def loop() -> None:
                while True:
                    try:
                        await job()
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        log.exception("job %s crashed", name)
                    await asyncio.sleep(seconds)

            return asyncio.create_task(loop(), name=name)

        for name, cfg in self.settings.devices.items():
            if name in self.devices:
                self._tasks.append(every(cfg.interval, lambda n=name: self.poll_device(n), f"poll:{name}"))
        for name, sensor in self.sensors.items():
            if isinstance(sensor, EsphomeSensor):
                self._tasks.append(asyncio.create_task(sensor.run(), name=f"sensor:{name}"))
        if self.dirigera:
            self._tasks.append(every(self.dirigera.interval, self.poll_dirigera, "dirigera"))
        if self.sensors:
            self._tasks.append(every(SENSOR_LOG_SECONDS, self.log_sensors, "log_sensors"))
        self._tasks.append(every(PRICE_CHECK_SECONDS, self.update_prices, "prices"))
        self._tasks.append(every(WEATHER_SECONDS, self.update_weather, "weather"))
        self._tasks.append(every(HEALTH_SECONDS, self.check_health, "health"))
        if any(hasattr(a, "energy_days") for a in self.devices.values()):
            self._tasks.append(every(ENERGY_SECONDS, self.update_energy, "energy"))
        doors = len(self.dirigera.doors) if self.dirigera else 0
        self.db.add_event("info", "app", f"started: {len(self.devices)} devices, {len(self.sensors)} sensors, {doors} doors")

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        for adapter in self.devices.values():
            close = getattr(adapter, "aclose", None)
            if close:
                await close()
        if self.dirigera:
            await self.dirigera.hub.aclose()
