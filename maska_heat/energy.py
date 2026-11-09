"""Electricity use and cost of heating, per room/device and in total.

Each device's use is built per clock hour from the best source it has, then priced with that hour's full
electricity cost (spot + network day/night + fees + VAT, tariff.elec_cost), so the cost reflects *when* it heated:

- MELCloud (heat pump): its energy report. Hourly values are scaled to the daily total of the same day (the hourly
  report reads ~15 % high, the daily one matches the meter); days without an hourly profile are spread evenly.
- Adax: the cloud's per-device counter of energy used so far in the current hour (resets every hour). The last
  reading of an hour is extended to the full hour (checked against Adax's daily log: ~1.5 % low).
- Everything else: power from the readings, held until the next reading — measured for the Mill, estimated as
  rated power × heating on/off for the floor thermostat and the water heater (collector).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .db import Database
from .providers.prices import PricePoint, to_slots
from .settings import Settings
from .tariff import elec_cost

HOUR = 3600
GAP_INTERVALS = 3  # a reading's power is held at most this many poll intervals (longer gaps count as unknown)
ADAX_MAX_EXTEND = 60 / 45  # extend an hour's last counter value at most from minute 45 to 60

PERIODS = {"today": "Today", "yesterday": "Yesterday", "7d": "Last 7 days", "month": "This month"}
METHOD_LABEL = {
    "report": "MELCloud energy report",
    "adax_counter": "Adax hourly meter",
    "power": "measured power",
    "estimate": "on/off × rated power",
}


@dataclass
class DeviceUse:
    zone: str
    name: str
    device: str
    method: str
    hours: dict[int, float] = field(default_factory=dict)  # hour start (unix s) → kWh
    kwh: float = 0.0
    cost: float = 0.0  # € incl. VAT, for hours with a known price
    unpriced_kwh: float = 0.0
    has_data: bool = True  # False: nothing logged for this device in the period

    @property
    def avg_price(self) -> float | None:
        priced = self.kwh - self.unpriced_kwh
        return self.cost / priced if priced > 0.001 else None


@dataclass
class EnergySummary:
    period: str
    label: str
    start: datetime
    end: datetime
    devices: list[DeviceUse]
    kwh: float
    cost: float
    unpriced_kwh: float
    avg_price: float | None  # what heating paid per kWh on average
    period_price: float | None  # plain average of the hourly prices in the period
    buckets: list[dict]  # chart: one entry per hour (1-day periods) or day


def period_bounds(period: str, now: float, tz: ZoneInfo) -> tuple[datetime, datetime]:
    local = datetime.fromtimestamp(now, tz)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "today":
        return midnight, local
    if period == "yesterday":
        return _local_midnight(midnight - timedelta(hours=12), tz), midnight
    if period == "7d":
        return _local_midnight(midnight - timedelta(days=6, hours=-12), tz), local
    if period == "month":
        return midnight.replace(day=1), local
    raise ValueError(f"unknown period {period!r}")


def _local_midnight(dt: datetime, tz: ZoneInfo) -> datetime:
    """Midnight of dt's local date (re-resolved, so DST changes in between are handled)."""
    d = dt.astimezone(tz).date()
    return datetime(d.year, d.month, d.day, tzinfo=tz)


def _hour(ts: float) -> int:
    return int(ts // HOUR * HOUR)


def _add_power(hours: dict[int, float], t0: float, t1: float, watts: float) -> None:
    """Spread `watts` held from t0 to t1 over clock hours."""
    t = t0
    while t < t1:
        h = _hour(t)
        seg_end = min(t1, h + HOUR)
        hours[h] = hours.get(h, 0.0) + watts * (seg_end - t) / 3.6e6
        t = seg_end


def power_hours(db: Database, source: str, start: float, end: float, interval: float) -> dict[int, float]:
    max_hold = GAP_INTERVALS * interval
    rows = [r for r in db.readings(source, int(start - max_hold), int(end)) if r["power_w"] is not None]
    hours: dict[int, float] = {}
    for i, r in enumerate(rows):
        nxt = rows[i + 1]["ts"] if i + 1 < len(rows) else end  # latest reading: held until now (within the gap limit)
        t0, t1 = max(r["ts"], start), min(nxt, r["ts"] + max_hold, end)
        if r["power_w"] > 0 and t1 > t0:
            _add_power(hours, t0, t1, r["power_w"])
    return hours


def adax_hours(db: Database, source: str, start: float, end: float, now: float) -> dict[int, float]:
    last: dict[int, tuple[int, float]] = {}  # hour → (ts, kWh so far) of the hour's last reading
    for r in db.readings(source, int(start), int(end)):
        if r["energy_kwh"] is not None:
            last[_hour(r["ts"])] = (r["ts"], r["energy_kwh"])
    hours = {}
    for h, (ts, kwh) in last.items():
        minutes = (ts - h) / 60
        running = h + HOUR > now  # the current hour is not over yet
        factor = 1.0 if running or minutes <= 0 else min(60 / minutes, ADAX_MAX_EXTEND)
        hours[h] = kwh * factor
    return hours


def report_hours(db: Database, source: str, start: float, end: float, now: float, tz: ZoneInfo) -> dict[int, float]:
    first_day = _local_midnight(datetime.fromtimestamp(start, tz), tz)
    rows = db.energy(source, int(first_day.timestamp()), int(end))
    hourly = {r["ts"]: r["kwh"] for r in rows if r["minutes"] == 60}
    daily = {r["ts"]: r["kwh"] for r in rows if r["minutes"] == 24 * 60}
    hours = dict(hourly)
    for day_ts, total in daily.items():
        day_end = _local_midnight(datetime.fromtimestamp(day_ts, tz) + timedelta(hours=36), tz).timestamp()
        in_day = [h for h in hourly if day_ts <= h < day_end]
        profile = sum(hourly[h] for h in in_day)
        if profile > 0:
            for h in in_day:
                hours[h] = hourly[h] * total / profile
        elif total > 0:  # no hourly profile: spread evenly over the day's (elapsed) hours
            spread = [h for h in range(int(day_ts), int(min(day_end, now)), HOUR)]
            for h in spread:
                hours[h] = total / len(spread)
    return {h: v for h, v in hours.items() if start <= h < end}


def device_hours(settings: Settings, db: Database, device: str, start: float, end: float, now: float) -> tuple[str, dict[int, float]]:
    cfg = settings.devices[device]
    source = f"device:{device}"
    tz = ZoneInfo(settings.location.timezone)
    if cfg.type == "melcloud":
        return "report", report_hours(db, source, start, end, now, tz)
    if cfg.type == "adax_cloud":
        return "adax_counter", adax_hours(db, source, start, end, now)
    method = "power" if cfg.type == "mill_local" else "estimate"
    return method, power_hours(db, source, start, end, cfg.interval)


def _has_data(db: Database, device: str, method: str, start: float, end: float) -> bool:
    source = f"device:{device}"
    if method == "report":
        return bool(db.energy(source, int(start) - 86400, int(end)))
    return db.count_readings(source, int(start), int(end)) > 0


def hour_prices(settings: Settings, db: Database, start: float, end: float) -> dict[int, float]:
    """€/kWh incl. network, fees and VAT per clock hour."""
    tz = ZoneInfo(settings.location.timezone)
    raw = [
        PricePoint(datetime.fromtimestamp(r["ts"], timezone.utc), r["minutes"], r["eur_mwh"])
        for r in db.prices(_hour(start), int(end) + HOUR)
    ]
    return {
        int(p.start.timestamp()): elec_cost(p.eur_mwh, p.start.astimezone(tz), settings.tariff).total
        for p in to_slots(raw, 60)
    }


def summarize(settings: Settings, db: Database, period: str, now: float) -> EnergySummary:
    tz = ZoneInfo(settings.location.timezone)
    start_dt, end_dt = period_bounds(period, now, tz)
    start, end = start_dt.timestamp(), end_dt.timestamp()
    prices = hour_prices(settings, db, start, end)

    devices = []
    for key, zone in settings.zones.items():
        if not zone.device or not settings.devices[zone.device].enabled:
            continue
        method, hours = device_hours(settings, db, zone.device, start, end, now)
        use = DeviceUse(key, zone.name, zone.device, method, hours, has_data=_has_data(db, zone.device, method, start, end))
        for h, kwh in hours.items():
            use.kwh += kwh
            if h in prices:
                use.cost += kwh * prices[h]
            else:
                use.unpriced_kwh += kwh
        devices.append(use)

    kwh = sum(d.kwh for d in devices)
    cost = sum(d.cost for d in devices)
    unpriced = sum(d.unpriced_kwh for d in devices)
    in_period = [p for h, p in prices.items() if start - HOUR < h < end]
    return EnergySummary(
        period=period,
        label=PERIODS[period],
        start=start_dt,
        end=end_dt,
        devices=devices,
        kwh=kwh,
        cost=cost,
        unpriced_kwh=unpriced,
        avg_price=cost / (kwh - unpriced) if kwh - unpriced > 0.001 else None,
        period_price=sum(in_period) / len(in_period) if in_period else None,
        buckets=_buckets(devices, prices, start_dt, end_dt, tz, by_hour=period in ("today", "yesterday")),
    )


def _buckets(devices: list[DeviceUse], prices: dict[int, float], start: datetime, end: datetime, tz: ZoneInfo, by_hour: bool) -> list[dict]:
    def key(h: int) -> int:
        if by_hour:
            return h
        return int(_local_midnight(datetime.fromtimestamp(h, tz), tz).timestamp())

    if by_hour:
        keys = list(range(_hour(start.timestamp()), int(end.timestamp()), HOUR))
    else:
        keys, d = [], _local_midnight(start, tz)
        while d < end:
            keys.append(int(d.timestamp()))
            d = _local_midnight(d + timedelta(hours=36), tz)
    kwh: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    cost: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for dev in devices:
        for h, v in dev.hours.items():
            kwh[key(h)][dev.zone] += v
            cost[key(h)][dev.zone] += v * prices.get(h, 0.0)
    return [
        {
            "ts": k,
            "kwh": {d.zone: round(kwh[k][d.zone], 3) for d in devices},
            "cost": {d.zone: round(cost[k][d.zone], 4) for d in devices},
            "price": prices.get(k) if by_hour else None,
        }
        for k in keys
    ]


def as_json(s: EnergySummary) -> dict:
    return {
        "period": s.period,
        "label": s.label,
        "start": int(s.start.timestamp()),
        "end": int(s.end.timestamp()),
        "kwh": round(s.kwh, 3),
        "cost": round(s.cost, 4),
        "unpriced_kwh": round(s.unpriced_kwh, 3),
        "avg_price": s.avg_price,
        "period_price": s.period_price,
        "devices": [
            {
                "zone": d.zone, "name": d.name, "device": d.device, "method": METHOD_LABEL[d.method],
                "kwh": round(d.kwh, 3), "cost": round(d.cost, 4), "avg_price": d.avg_price, "has_data": d.has_data,
            }
            for d in s.devices
        ],
        "by_hour": s.period in ("today", "yesterday"),
        "buckets": s.buckets,
    }
