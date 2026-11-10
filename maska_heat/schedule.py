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
    preheat_for: datetime | None = None  # start of the warmer slot being pre-heated for


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


def _change_start(zone: str, before: datetime, after: datetime, snap: Snapshot) -> datetime:
    """First whole minute in (before, after] whose slot differs from the slot at `before`. Whole minutes keep the
    result identical between runs started a few seconds apart, so it can be compared with a remembered value."""
    base = slot_at(zone, before, snap)
    t = before.replace(second=0, microsecond=0) + timedelta(minutes=1)
    while t < after and slot_at(zone, t, snap) == base:
        t += timedelta(minutes=1)
    return t


def smart_target(
    zone: str,
    now: datetime,
    current_temp: float | None,
    kind: str,
    snap: Snapshot,
    preheating_for: datetime | None = None,
) -> SmartTarget:
    """Setpoint to apply now. Starts heating early when a warmer slot is coming up.

    Once pre-heating has started for a slot (`preheating_for`, remembered by the caller), it continues until that
    slot begins, even when the room gets ahead of schedule — otherwise the setpoint flips between the two slots
    every few minutes as the room warms and cools around the lead-time line.
    """
    current = slot_at(zone, now, snap)
    rate = DEFAULT_HEATING_RATE.get(kind, 1.0)
    start_temp = current_temp if current_temp is not None else current.setpoint

    next_change = None
    preheat: tuple[datetime, Slot] | None = None
    prev_t, prev = now, current
    t = now + STEP
    while t <= now + LOOKAHEAD and preheat is None:
        s = slot_at(zone, t, snap)
        if s != prev:
            begins = _change_start(zone, prev_t, t, snap)
            s = slot_at(zone, begins, snap)
            if next_change is None:
                next_change = (begins, s)
            if s.setpoint > current.setpoint + 0.05:
                need = max(s.setpoint - start_temp, 0.0)
                lead_h = min(need / rate + LEAD_MARGIN_HOURS, MAX_LEAD_HOURS)
                # real hours (timestamps): a wall-clock difference is an hour off across a DST change
                if begins == preheating_for or (begins.timestamp() - now.timestamp()) / 3600 <= lead_h:
                    preheat = (begins, s)
        prev_t, prev = t, slot_at(zone, t, snap)
        t += STEP

    if preheat:
        at, s = preheat
        reason = f"pre-heating for {MODE_LABEL[s.mode]} at {at:%H:%M}"
        return SmartTarget(s.mode, s.setpoint, True, reason, next_change, preheat_for=at)
    return SmartTarget(current.mode, current.setpoint, False, MODE_LABEL[current.mode], next_change)
