"""Data shaping for the UI and JSON API. Pure functions over the database — no device access."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .db import Database
from .providers.prices import PricePoint, to_slots
from .settings import Settings
from .tariff import elec_cost

SENSOR_STALE_SECONDS = 300


@dataclass
class ZoneView:
    key: str
    name: str
    temp: float | None
    temp_source: str | None
    humidity: float | None
    setpoint: float | None
    power_on: bool | None
    is_heating: bool | None
    power_w: float | None
    updated_at: int | None
    stale: bool
    device_online: bool | None
    device_enabled: bool


@dataclass
class PriceSlot:
    ts: int
    minutes: int
    eur_mwh: float
    cost_total: float  # €/kWh incl. network, fees, VAT
    network: float
    is_day: bool
    is_now: bool


def _stale_after(settings: Settings, source: str) -> int:
    kind, _, name = source.partition(":")
    if kind == "device":
        return settings.devices[name].interval * 3
    return SENSOR_STALE_SECONDS


def zone_views(settings: Settings, db: Database, now: float) -> list[ZoneView]:
    views = []
    for key, zone in settings.zones.items():
        temp = humidity = temp_source = updated = None
        stale = True
        for source in zone.temp_sources:
            row = db.latest_reading(source)
            if row and row["temp"] is not None and now - row["ts"] <= _stale_after(settings, source):
                temp, humidity, temp_source, updated, stale = row["temp"], row["humidity"], source, row["ts"], False
                break
        enabled = bool(zone.device) and settings.devices[zone.device].enabled
        dev = db.latest_reading(f"device:{zone.device}") if zone.device else None
        dev_fresh = bool(dev) and now - dev["ts"] <= _stale_after(settings, f"device:{zone.device}")
        if temp is None and dev and dev["temp"] is not None:  # show last known value, marked stale
            temp, temp_source, updated = dev["temp"], f"device:{zone.device}", dev["ts"]

        def flag(col: str) -> bool | None:
            return None if not dev or dev[col] is None else bool(dev[col])

        views.append(
            ZoneView(
                key=key,
                name=zone.name,
                temp=temp,
                temp_source=temp_source,
                humidity=humidity,
                setpoint=dev["setpoint"] if dev else None,
                power_on=flag("power_on"),
                is_heating=flag("is_heating"),
                power_w=dev["power_w"] if dev else None,
                updated_at=updated,
                stale=stale,
                device_online=dev_fresh if enabled else None,
                device_enabled=enabled,
            )
        )
    return views


def price_slots(settings: Settings, db: Database, since: datetime, until: datetime, now: float) -> list[PriceSlot]:
    tz = ZoneInfo(settings.location.timezone)
    raw = [
        PricePoint(datetime.fromtimestamp(r["ts"], timezone.utc), r["minutes"], r["eur_mwh"])
        for r in db.prices(int(since.timestamp()), int(until.timestamp()))
    ]
    slots = []
    for p in to_slots(raw, settings.price.slot_minutes):
        ts = int(p.start.timestamp())
        cost = elec_cost(p.eur_mwh, p.start.astimezone(tz), settings.tariff)
        slots.append(
            PriceSlot(
                ts=ts,
                minutes=p.minutes,
                eur_mwh=p.eur_mwh,
                cost_total=round(cost.total, 5),
                network=cost.network,
                is_day=cost.is_day,
                is_now=ts <= now < ts + p.minutes * 60,
            )
        )
    return slots


def upcoming_prices(settings: Settings, db: Database, now: float) -> list[PriceSlot]:
    """From the start of the current slot to the end of known prices."""
    tz = ZoneInfo(settings.location.timezone)
    local = datetime.fromtimestamp(now, tz)
    slot = settings.price.slot_minutes
    start = local.replace(minute=(local.minute // slot) * slot if slot < 60 else 0, second=0, microsecond=0)
    return price_slots(settings, db, start, start + timedelta(days=2), now)


def history(settings: Settings, db: Database, zone_key: str, hours: int, now: float) -> dict:
    zone = settings.zones[zone_key]
    since = int(now - hours * 3600)
    sources = list(dict.fromkeys(zone.temp_sources + ([f"device:{zone.device}"] if zone.device else [])))
    series = {}
    for source in sources:
        rows = db.readings(source, since)
        series[source] = {
            "temp": [[r["ts"] * 1000, r["temp"]] for r in rows if r["temp"] is not None],
            "setpoint": [[r["ts"] * 1000, r["setpoint"]] for r in rows if r["setpoint"] is not None],
            "power_w": [[r["ts"] * 1000, r["power_w"]] for r in rows if r["power_w"] is not None],
            "humidity": [[r["ts"] * 1000, r["humidity"]] for r in rows if r["humidity"] is not None],
        }
    outdoor = [[r["ts"] * 1000, r["temp"]] for r in db.weather(since, int(now) + 3600)]
    if settings.outdoor_sensor:
        rows = db.readings(f"sensor:{settings.outdoor_sensor}", since)
        outdoor_sensor = [[r["ts"] * 1000, r["temp"]] for r in rows if r["temp"] is not None]
    else:
        outdoor_sensor = []
    tz = ZoneInfo(settings.location.timezone)
    start_local = datetime.fromtimestamp(since, tz)
    prices = price_slots(settings, db, start_local, datetime.fromtimestamp(now, tz), now)
    return {
        "zone": zone_key,
        "name": zone.name,
        "series": series,
        "outdoor_forecast": outdoor,
        "outdoor_sensor": outdoor_sensor,
        "prices": [[p.ts * 1000, round(p.cost_total * 100, 2)] for p in prices],
        "fires": [{"ts": f["lit_at"] * 1000, "stove": f["stove"]} for f in db.fires(since)],
    }


def as_dict(obj) -> dict:
    return asdict(obj)
