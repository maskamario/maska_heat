"""One-off corrections of logged data (`python -m maska_heat fix-hot-water`).

Hot water, 9.–11.11.2026: on "heats on schedule" the boiler's heat request was logged as heating at rated power
while the schedule was holding it back (fixed in the adapter since). `fix_hot_water` sets those readings to
"not heating, 0 W", so the energy page — which is computed from the readings — shows what the boiler really used.

A reading is corrected only when all of this holds:
- it says heating at exactly the rated power (the collector's fallback, never a power the boiler reported);
- the boiler was on "heats on schedule";
- the app's own event log says which hours that day was scheduled to heat, and the reading is outside them;
- the water did not get warmer during that stretch (a boost or a schedule set by hand heats outside the slots).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from .db import Database
from .hotwater_plan import describe
from .settings import Settings

SCHEDULE_MODE = "heats on schedule"
REAL_HEATING_RISE = 2.0  # °C: the tank warming this much during a stretch means it really heated
RUN_GAP_SECONDS = 15 * 60  # readings further apart than this are separate stretches
_EVENT = re.compile(r"(\d\d)\.(\d\d) heats (.*)$")
_WINDOW = re.compile(r"(\d\d):(\d\d)–(\d\d):(\d\d)")


@dataclass
class Stretch:
    start: int
    end: int
    rows: int
    kwh: float
    temp_change: float | None
    windows: list[tuple[int, int]]
    corrected: bool


@dataclass
class Report:
    source: str
    stretches: list[Stretch] = field(default_factory=list)
    days_without_schedule: set[str] = field(default_factory=set)
    applied: bool = False

    @property
    def rows(self) -> int:
        return sum(s.rows for s in self.stretches if s.corrected)

    @property
    def kwh(self) -> float:
        return sum(s.kwh for s in self.stretches if s.corrected)

    def text(self, tz: ZoneInfo) -> str:
        def at(ts: int) -> str:
            return f"{datetime.fromtimestamp(ts, tz):%a %d.%m %H:%M}"

        lines = []
        for s in self.stretches:
            change = "" if s.temp_change is None else f", water {s.temp_change:+.1f} °C"
            verdict = "phantom" if s.corrected else "kept — the water got warmer, it really heated"
            lines.append(
                f"  {at(s.start)} – {at(s.end):>15}  {s.kwh:5.2f} kWh{change}  (slots {describe(s.windows)})  {verdict}"
            )
        for day in sorted(self.days_without_schedule):
            lines.append(f"  {day}: no schedule in the event log — left alone")
        total = f"{self.rows} readings, {self.kwh:.1f} kWh logged outside the heating slots"
        lines.append(f"{'Corrected' if self.applied else 'Would correct'} {total}.")
        if not self.applied and self.rows:
            lines.append("Nothing was changed. Run again with --apply to correct them.")
        return "\n".join(lines)


def scheduled_windows(db: Database, zone: str, since: int) -> list[tuple[int, int, int, list[tuple[int, int]]]]:
    """(event ts, month, day, windows) for every "… heats 02:00–07:00, …" event of the zone, oldest first."""
    out = []
    for e in db.events_of(f"control:{zone}", since):
        m = _EVENT.search(e["message"])
        if m:
            windows = [(int(a) * 60 + int(b), int(c) * 60 + int(d)) for a, b, c, d in _WINDOW.findall(m.group(3))]
            out.append((e["ts"], int(m.group(2)), int(m.group(1)), windows))
    return out


def fix_hot_water(settings: Settings, db: Database, since: int, until: int, apply: bool = False) -> Report:
    if settings.hot_water is None:
        raise ValueError("no hot_water section in config.yaml")
    zone = settings.hot_water.zone
    device = settings.zones[zone].device
    rated = settings.devices[device].power_w
    source = f"device:{device}"
    tz = ZoneInfo(settings.location.timezone)
    report = Report(source)
    if not rated:
        return report
    schedules = scheduled_windows(db, zone, since - 3 * 86400)

    def windows_at(ts: int, local: datetime) -> list[tuple[int, int]] | None:
        found = None
        for event_ts, month, day, windows in schedules:  # the last schedule written for that day before the reading
            if event_ts <= ts and (month, day) == (local.month, local.day):
                found = windows
        return found

    rows = db.readings(source, since, until)
    runs: list[list[int]] = []  # indexes into rows
    run_windows: list[list[tuple[int, int]]] = []
    for i, r in enumerate(rows):
        if not (r["is_heating"] == 1 and r["power_w"] == rated and SCHEDULE_MODE in (r["mode"] or "")):
            continue
        local = datetime.fromtimestamp(r["ts"], tz)
        windows = windows_at(r["ts"], local)
        if windows is None:
            report.days_without_schedule.add(f"{local:%a %d.%m}")
            continue
        minute = local.hour * 60 + local.minute
        if any(a <= minute < b for a, b in windows):
            continue
        if runs and runs[-1][-1] == i - 1 and r["ts"] - rows[i - 1]["ts"] <= RUN_GAP_SECONDS:
            runs[-1].append(i)
        else:
            runs.append([i])
            run_windows.append(windows)

    fix: list[int] = []
    for run, windows in zip(runs, run_windows):
        temps = [rows[i]["temp"] for i in run if rows[i]["temp"] is not None]
        rise = max(temps) - temps[0] if temps else 0.0
        end = rows[run[-1] + 1]["ts"] if run[-1] + 1 < len(rows) else rows[run[-1]]["ts"]
        end = min(end, rows[run[-1]]["ts"] + RUN_GAP_SECONDS)
        corrected = rise < REAL_HEATING_RISE
        report.stretches.append(
            Stretch(
                start=rows[run[0]]["ts"],
                end=end,
                rows=len(run),
                kwh=rated * (end - rows[run[0]]["ts"]) / 3.6e6,
                temp_change=temps[-1] - temps[0] if temps else None,
                windows=windows,
                corrected=corrected,
            )
        )
        if corrected:
            fix += [rows[i]["ts"] for i in run]

    if apply and fix:
        db.mark_not_heating(source, fix)
        db.add_event(
            "info", f"control:{zone}",
            f"{settings.zones[zone].name}: corrected {len(fix)} readings ({report.kwh:.1f} kWh) logged as heating "
            "while the schedule held the boiler back",
        )
        report.applied = True
    return report
