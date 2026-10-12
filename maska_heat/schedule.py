"""Smart schedule (no price shifting yet): which comfort mode applies to a zone at a given time, and which
setpoint to send now — including pre-heating before a warmer period starts.

Pure functions over a snapshot of the control settings, so they are easy to test.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .control import Away, Comfort, HouseSchedule, School
from .tariff import estonian_holidays

# Cautious heating rates (°C per hour) used until phase 2 learns the real ones. Low values = start earlier.
DEFAULT_HEATING_RATE = {"resistive": 1.0, "heat_pump": 1.5, "floor": 0.5}
LOOKAHEAD = timedelta(hours=8)
STEP = timedelta(minutes=15)
MAX_LEAD_HOURS = 6.0
LEAD_MARGIN_HOURS = 0.25

MODE_LABEL = {"day": "Day", "night": "Night", "out": "Out", "away": "Away"}


@dataclass(frozen=True)
class Snapshot:
    comfort: Comfort
    house: HouseSchedule
    school: School
    away: Away
    home_today: dict[str, date]


@dataclass(frozen=True)
class Slot:
    mode: str
    setpoint: float


@dataclass(frozen=True)
class SmartTarget:
    mode: str
    setpoint: float
    preheating: bool
    reason: str
    next_change: tuple[datetime, Slot] | None


def is_weekend_like(d: date) -> bool:
    return d.weekday() >= 5 or d in estonian_holidays(d.year)


def is_school_day(zone: str, d: date, snap: Snapshot) -> bool:
    kid = snap.school.zones.get(zone)
    if not kid or not kid.enabled or is_weekend_like(d) or snap.school.is_holiday(d):
        return False
    if snap.home_today.get(zone) == d:
        return False
    day = kid.days.get(d.weekday())
    return bool(day and day.active)


def slot_at(zone: str, t: datetime, snap: Snapshot) -> Slot:
    """Comfort mode and setpoint for a zone at local time t."""
    c = snap.comfort
    if snap.away.active_at(t):
        return Slot("away", c.away.preferred)

    wake = snap.house.weekend_wake if is_weekend_like(t.date()) else snap.house.weekday_wake
    now = t.time()
    if now < wake or now >= snap.house.night_start:
        return Slot("night", c.night.preferred)

    if is_school_day(zone, t.date(), snap):
        day = snap.school.zones[zone].days[t.weekday()]
        if day.leave <= now < day.back:
            return Slot("out", c.out.preferred)
        if now < day.leave:  # short morning before school
            return Slot("day", c.day.min if snap.school.morning_target == "min" else c.day.preferred)
    return Slot("day", c.day.preferred)


def smart_target(zone: str, now: datetime, current_temp: float | None, kind: str, snap: Snapshot) -> SmartTarget:
    """Setpoint to apply now. Starts heating early when a warmer slot is coming up."""
    current = slot_at(zone, now, snap)
    rate = DEFAULT_HEATING_RATE.get(kind, 1.0)
    start_temp = current_temp if current_temp is not None else current.setpoint

    next_change = None
    preheat: tuple[datetime, Slot] | None = None
    t = now + STEP
    while t <= now + LOOKAHEAD:
        s = slot_at(zone, t, snap)
        if next_change is None and s != current:
            next_change = (t, s)
        if preheat is None and s.setpoint > current.setpoint + 0.05:
            need = max(s.setpoint - start_temp, 0.0)
            lead_h = min(need / rate + LEAD_MARGIN_HOURS, MAX_LEAD_HOURS)
            if (t - now).total_seconds() / 3600 <= lead_h:
                preheat = (t, s)
        t += STEP

    if preheat:
        at, s = preheat
        reason = f"pre-heating for {MODE_LABEL[s.mode]} at {at:%H:%M}"
        return SmartTarget(s.mode, s.setpoint, True, reason, next_change)
    return SmartTarget(current.mode, current.setpoint, False, MODE_LABEL[current.mode], next_change)
