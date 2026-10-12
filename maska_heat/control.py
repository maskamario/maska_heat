"""UI-editable control settings: comfort bands, schedules, away period and per-zone control mode.

Stored as JSON documents in the `kv` table, so changes from the UI apply without a restart.
Defaults follow docs/DESIGN.md §5.
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Literal

from pydantic import BaseModel, field_validator, model_validator

from .db import Database

ControlMode = Literal["handsoff", "manual", "smart"]
COMFORT_MODES = ("day", "night", "out", "away")
SETPOINT_RANGE = (5.0, 30.0)


class Band(BaseModel):
    min: float
    preferred: float
    max: float

    @model_validator(mode="after")
    def _ordered(self) -> "Band":
        lo, hi = SETPOINT_RANGE
        if not (lo <= self.min <= self.preferred <= self.max <= hi):
            raise ValueError(f"need {lo} ≤ min ≤ preferred ≤ max ≤ {hi}")
        return self


class Comfort(BaseModel):
    day: Band = Band(min=20, preferred=22, max=24)
    night: Band = Band(min=18, preferred=19, max=21)
    out: Band = Band(min=18, preferred=19, max=22)
    away: Band = Band(min=14, preferred=15, max=18)

    def band(self, mode: str) -> Band:
        return getattr(self, mode)


class HouseSchedule(BaseModel):
    weekday_wake: time = time(6, 30)
    weekend_wake: time = time(8, 0)
    night_start: time = time(23, 0)

    @model_validator(mode="after")
    def _order(self) -> "HouseSchedule":
        if not (self.weekday_wake < self.night_start and self.weekend_wake < self.night_start):
            raise ValueError("wake-up times must be before night start")
        return self


class SchoolDay(BaseModel):
    leave: time | None = None
    back: time | None = None

    @model_validator(mode="after")
    def _order(self) -> "SchoolDay":
        if (self.leave is None) != (self.back is None):
            raise ValueError("set both leave and return time, or neither")
        if self.leave and self.back and self.leave >= self.back:
            raise ValueError("leave time must be before return time")
        return self

    @property
    def active(self) -> bool:
        return self.leave is not None


def _default_week() -> dict[int, SchoolDay]:
    return {d: SchoolDay(leave=time(7, 0), back=time(13, 0)) for d in range(5)}


class KidSchool(BaseModel):
    enabled: bool = True
    days: dict[int, SchoolDay] = {}

    @field_validator("days")
    @classmethod
    def _weekdays(cls, v: dict[int, SchoolDay]) -> dict[int, SchoolDay]:
        if any(d not in range(7) for d in v):
            raise ValueError("weekday must be 0–6")
        return v


class DateRange(BaseModel):
    start: date
    end: date  # inclusive

    @model_validator(mode="after")
    def _order(self) -> "DateRange":
        if self.end < self.start:
            raise ValueError("end date before start date")
        return self


class School(BaseModel):
    zones: dict[str, KidSchool] = {}
    holidays: list[DateRange] = []
    morning_target: Literal["min", "preferred"] = "min"  # kids' rooms at wake-up on school days

    def is_holiday(self, d: date) -> bool:
        return any(r.start <= d <= r.end for r in self.holidays)


class Away(BaseModel):
    start: datetime | None = None
    end: datetime | None = None

    def active_at(self, t: datetime) -> bool:
        return self.start is not None and self.end is not None and self.start <= t < self.end


class ZoneControl(BaseModel):
    mode: ControlMode = "handsoff"
    manual_setpoint: float | None = None
    # Set while the app is in charge of the device; restore_setpoint is sent back when returning to hands-off.
    engaged: bool = False
    restore_setpoint: float | None = None


class ControlStore:
    """Typed access to the control documents in the database."""

    def __init__(self, db: Database, kid_zones: list[str] | None = None):
        self.db = db
        self._kid_zones = kid_zones or []

    def _get(self, key: str, model: type[BaseModel], default: BaseModel) -> BaseModel:
        raw = self.db.get_kv(key)
        return model.model_validate_json(raw) if raw else default

    def _set(self, key: str, value: BaseModel) -> None:
        self.db.set_kv(key, value.model_dump_json())

    def comfort(self) -> Comfort:
        return self._get("comfort", Comfort, Comfort())

    def set_comfort(self, c: Comfort) -> None:
        self._set("comfort", c)

    def house(self) -> HouseSchedule:
        return self._get("house_schedule", HouseSchedule, HouseSchedule())

    def set_house(self, h: HouseSchedule) -> None:
        self._set("house_schedule", h)

    def school(self) -> School:
        default = School(zones={z: KidSchool(days=_default_week()) for z in self._kid_zones})
        return self._get("school", School, default)

    def set_school(self, s: School) -> None:
        self._set("school", s)

    def away(self) -> Away:
        return self._get("away", Away, Away())

    def set_away(self, a: Away) -> None:
        self._set("away", a)

    def home_today(self) -> dict[str, date]:
        raw = self.db.get_kv("home_today")
        if not raw:
            return {}
        from json import loads

        return {k: date.fromisoformat(v) for k, v in loads(raw).items()}

    def set_home_today(self, zone: str, day: date | None) -> None:
        from json import dumps

        current = {k: v.isoformat() for k, v in self.home_today().items()}
        if day is None:
            current.pop(zone, None)
        else:
            current[zone] = day.isoformat()
        self.db.set_kv("home_today", dumps(current))

    def zone(self, zone: str) -> ZoneControl:
        return self._get(f"zone:{zone}", ZoneControl, ZoneControl())

    def set_zone(self, zone: str, c: ZoneControl) -> None:
        self._set(f"zone:{zone}", c)
