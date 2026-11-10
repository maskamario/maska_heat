"""Applies the control mode of each zone to its heating device.

- Hands-off: the app never writes. When a zone returns to hands-off, the device is handed back
  (Mill → its weekly program, others → the setpoint they had before the app took over).
- Manual: the user's fixed setpoint from the UI.
- Smart: the schedule target from schedule.smart_target (no price shifting yet).

Rules: only setpoints are written, never power on/off; a setpoint is only sent when it differs from the
device's current one; a sent value is not repeated while the device may still be syncing (Adax cloud ~5 min).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from .adapters.base import DeviceAdapter
from .control import ControlStore, ZoneControl
from .dashboard import zone_view
from .db import Database
from .jobs import Jobs
from .schedule import MODE_LABEL, Snapshot, SmartTarget, slot_at, smart_target
from . import spike
from .settings import Settings

log = logging.getLogger(__name__)

RUN_SECONDS = 5 * 60
OFFSET_WINDOW = 20 * 60  # heater-vs-room gap: median over this long
ROOM_SENSOR_FRESH = 10 * 60
MAX_OFFSET = 6.0  # a bigger gap is implausible: fall back to the heater's own sensor
OFFSET_KEY = "room_offset"  # kv: zone -> {"offset": °C, "ts"}: the last measured gap, used while it can't be measured
OFFSET_MAX_AGE = 24 * 3600
OFFSET_REFRESH = 10 * 60
SAFETY_HYSTERESIS = 0.5  # °C above spike.safety_min_temp before a room heating for safety pauses again
PREHEAT_KEY = "preheat_for"  # kv: zone -> ISO time of the slot being pre-heated for
TRIM_KEY = "room_trim_v2"  # kv: zone -> {"trim": °C, "ts": last update} (v1 wound up overnight: discarded)
TRIM_GAIN = 1.5  # °C of heater setpoint per °C of room error per hour
TRIM_MAX = 1.5
TRIM_BAND = 1.0  # only learn while the room is within this of the target (not while warming up after a change)
TRIM_MAX_STEP = 15 * 60  # longest gap counted between two updates
TRIM_CYCLING = 0.5  # the heater is cycling (not idle or flat out) while its sensor is within this of its setpoint
PENDING_SECONDS = 10 * 60  # don't re-send the same value within this time
FRESH_READING_SECONDS = 15 * 60


@dataclass
class ZoneDecision:
    mode: str  # handsoff | manual | smart | logging (device is read-only)
    setpoint: float | None  # what goes to the device (a room target is converted for room-sensor control)
    label: str
    smart: SmartTarget | None = None
    blocked: str | None = None  # why nothing can be sent right now
    heater_units: bool = False  # setpoint is already a heater value (spike pause), not a room target
    room_target: float | None = None  # room-sensor control: the room temperature aimed at
    offset: float | None = None  # room-sensor control: heater sensor − room sensor, °C
    trim: float = 0.0  # room-sensor control: learned correction when the room settles off target, °C
    learn: bool = False  # the trim may learn from this cycle: a plain target on fresh data (no spike rule)
    safety: bool = False  # a spike pause overridden because the room is below the safety minimum


def heater_offset(db: Database, device: str, sensor_source: str, now: float) -> float | None:
    """Median gap between the heater's own sensor and the room sensor over the last minutes (None if unknown)."""
    room = [(r["ts"], r["temp"]) for r in db.readings(sensor_source, int(now - OFFSET_WINDOW - 600), int(now) + 1) if r["temp"] is not None]
    if not room or now - room[-1][0] > ROOM_SENSOR_FRESH:
        return None
    gaps = []
    for r in db.readings(f"device:{device}", int(now - OFFSET_WINDOW), int(now) + 1):
        if r["temp"] is None:
            continue
        ts, temp = min(room, key=lambda x: abs(x[0] - r["ts"]))
        if abs(ts - r["ts"]) <= 300:
            gaps.append(r["temp"] - temp)
    if len(gaps) < 2:
        return None
    gaps.sort()
    mid = len(gaps) // 2
    offset = gaps[mid] if len(gaps) % 2 else (gaps[mid - 1] + gaps[mid]) / 2
    return offset if abs(offset) <= MAX_OFFSET else None


def room_temp(db: Database, sensor_source: str, now: float, window: int = 10 * 60) -> float | None:
    """Mean room-sensor temperature over the last minutes (None if the sensor is silent)."""
    temps = [r["temp"] for r in db.readings(sensor_source, int(now - window), int(now) + 1) if r["temp"] is not None]
    return sum(temps) / len(temps) if temps else None


class Executor:
    def __init__(self, settings: Settings, db: Database, store: ControlStore, devices: dict[str, DeviceAdapter]):
        self.settings = settings
        self.db = db
        self.store = store
        self.devices = devices
        self.tz = ZoneInfo(settings.location.timezone)
        self._pending: dict[str, tuple[float, float]] = {}  # zone -> (value, sent_at)
        self._locks: dict[str, asyncio.Lock] = {}
        self._blocked_noted: dict[str, str] = {}
        self._mill_taken: set[str] = set()  # Mills switched to 'Control individually' since this process started
        # zone -> start of the slot being pre-heated for; kept in kv so a restart mid-pre-heat doesn't flip-flop
        self._preheating_for: dict[str, datetime] = {
            k: datetime.fromisoformat(v) for k, v in json.loads(db.get_kv(PREHEAT_KEY) or "{}").items()
        }
        self._safety: set[str] = set()  # zones heating through a spike because the room fell below the limit
        self.jobs = Jobs()

    def snapshot(self) -> Snapshot:
        return Snapshot(
            comfort=self.store.comfort(),
            house=self.store.house(),
            school=self.store.school(),
            away=self.store.away(),
            home_today=self.store.home_today(),
        )

    def _device_type(self, zone_key: str) -> str | None:
        dev = self.settings.zones[zone_key].device
        return self.settings.devices[dev].type if dev else None

    # decision -----------------------------------------------------------------
    #
    # One pipeline per zone: (1) zones the app does not drive, (2) the room target — manual or the schedule,
    # (3) price-spike rules on top of it, (4) for room-sensor control, one conversion to the heater's units.

    def decide(self, zone_key: str, now: float | None = None, snap: Snapshot | None = None) -> ZoneDecision:
        now = now or time.time()
        d = self._decide(zone_key, now, snap)
        zone = self.settings.zones[zone_key]
        if zone.control_by == "room" and d.setpoint is not None and not d.heater_units:
            self._to_heater_units(zone_key, d, now)
        return d

    def _decide(self, zone_key: str, now: float, snap: Snapshot | None) -> ZoneDecision:
        ctl = self.store.zone(zone_key)
        zone = self.settings.zones[zone_key]
        # (1) not driven by the executor
        if self.settings.hot_water and zone_key == self.settings.hot_water.zone:
            return self._hot_water_decision(ctl.mode, now)
        if zone.device in self.devices and not self.devices[zone.device].capabilities.writable:
            return ZoneDecision("logging", None, "Logging only — the app does not control this heater")
        if ctl.mode == "handsoff":
            return ZoneDecision("handsoff", None, "Hands-off — heater follows its own app")
        if not zone.device or zone.device not in self.devices:
            return ZoneDecision(ctl.mode, None, "No controllable device", blocked="device not available")

        latest = self.db.latest_reading(f"device:{zone.device}")
        blocked = None
        if latest is None or now - latest["ts"] > FRESH_READING_SECONDS:
            blocked = "no recent reading from the device"
        elif latest["power_on"] == 0:
            blocked = "device is switched off — the app never switches it on"

        # (2) the room target
        if ctl.mode == "manual":  # a fixed temperature: price spikes do not apply
            sp = ctl.manual_setpoint
            label = "Manual" if sp is not None else "Manual — no temperature set"
            return ZoneDecision("manual", sp, label, blocked=blocked, learn=blocked is None)
        temp = self.room_estimate(zone_key, now)
        snap = snap or self.snapshot()
        target = smart_target(
            zone_key, datetime.fromtimestamp(now, self.tz), temp, self.devices[zone.device].capabilities.kind, snap,
            preheating_for=self._preheating_for.get(zone_key),
        )
        d = ZoneDecision("smart", target.setpoint, target.reason, smart=target, blocked=blocked, learn=blocked is None)

        # (3) price spikes
        band = snap.comfort.band(target.mode)
        paused = spike.pausing(self.settings, self.db, zone_key, now)
        if paused:
            d.learn = False
            label = f"price spike {paused.label(self.tz)} — paused"
            limit = self.settings.spike.safety_min_temp
            # hysteresis: once heating for safety, keep it until the room is half a degree above the limit
            if temp is not None and (temp < limit or (zone_key in self._safety and temp < limit + SAFETY_HYSTERESIS)):
                d.setpoint, d.safety = band.min, True
                d.label = f"{label}, but below {limit:.0f} °C: keeps the minimum"
            else:
                d.setpoint, d.heater_units, d.label = self.devices[zone.device].capabilities.setpoint_min, True, label
            return d
        ahead = spike.preheating(self.settings, self.db, zone_key, now)
        if ahead and slot_at(zone_key, datetime.fromtimestamp(ahead.start, self.tz), snap).mode not in ("day", "night"):
            ahead = None  # nobody home during the spike (Away, Out): stored heat would buy nothing
        if ahead and band.max > target.setpoint:
            d.setpoint, d.learn = band.max, False
            d.label = f"pre-heating before the price spike at {datetime.fromtimestamp(ahead.start, self.tz):%H:%M}"
        return d

    def room_estimate(self, zone_key: str, now: float) -> float | None:
        """The room temperature the decision works with.

        Room-sensor control: the room sensor (last 10 min); while it is silent, the heater's own sensor corrected
        by the last known gap, or without one the heater's sensor as it is (it reads low: errs on the warm side).
        Otherwise the zone's usual temperature source.
        """
        zone = self.settings.zones[zone_key]
        if zone.control_by == "room":
            t = room_temp(self.db, zone.room_sensor, now, ROOM_SENSOR_FRESH)
            if t is not None:
                return t
            heater = self.db.latest_reading(f"device:{zone.device}")
            if heater is None or heater["temp"] is None or now - heater["ts"] > FRESH_READING_SECONDS:
                return None
            known = self.last_offset(zone_key, now)
            return heater["temp"] - known if known is not None else heater["temp"]
        return zone_view(self.settings, self.db, zone_key, now).temp

    def _to_heater_units(self, zone_key: str, d: ZoneDecision, now: float) -> None:
        """Turn a room target into the heater's setpoint: shift by how much lower/higher the heater's sensor reads.

        While the gap cannot be measured (room sensor silent, heater readings missing), the last measured gap is
        used for up to a day; without one, nothing is sent: a room target is never sent to the heater as it is
        (the room would end up some 3 °C off).
        """
        zone = self.settings.zones[zone_key]
        offset = heater_offset(self.db, zone.device, zone.room_sensor, now)
        if offset is not None:
            self._remember_offset(zone_key, offset, now)
        else:
            d.learn = False
            offset = self.last_offset(zone_key, now)
            if offset is None:
                d.room_target, d.setpoint = d.setpoint, None
                d.blocked = d.blocked or "the room sensor is silent and the heater-vs-room gap is unknown — heater left as it is"
                return
            d.label += " (room sensor silent: using the last known gap)"
        caps = self.devices[zone.device].capabilities
        trim = self.trim(zone_key)
        wanted = d.setpoint + offset + trim
        latest = self.db.latest_reading(f"device:{zone.device}")
        current = latest["setpoint"] if latest else None
        if current is not None and abs(wanted - current) < 0.75 * caps.setpoint_step:
            setpoint = current  # within the heater's step: keep it, no flip-flopping
        else:
            setpoint = caps.clamp(wanted)
        d.room_target, d.offset, d.trim, d.setpoint = d.setpoint, offset, trim, setpoint

    def room_equivalent(self, zone_key: str, heater_setpoint: float, now: float | None = None) -> float | None:
        """The room temperature a heater setpoint amounts to (the conversion backwards), to the nearest 0.5 °C."""
        now = now or time.time()
        zone = self.settings.zones[zone_key]
        if zone.control_by != "room":
            return heater_setpoint
        offset = heater_offset(self.db, zone.device, zone.room_sensor, now)
        if offset is None:
            offset = self.last_offset(zone_key, now)
        if offset is None:
            return None
        return round((heater_setpoint - offset - self.trim(zone_key)) * 2) / 2

    def _offsets(self) -> dict:
        raw = self.db.get_kv(OFFSET_KEY)
        return json.loads(raw) if raw else {}

    def last_offset(self, zone_key: str, now: float) -> float | None:
        entry = self._offsets().get(zone_key)
        return entry["offset"] if entry and 0 <= now - entry["ts"] <= OFFSET_MAX_AGE else None

    def _remember_offset(self, zone_key: str, offset: float, now: float) -> None:
        offsets = self._offsets()
        old = offsets.get(zone_key)
        if old and abs(old["offset"] - offset) < 0.05 and 0 <= now - old["ts"] < OFFSET_REFRESH:
            return  # unchanged: spare the write (decide() also runs for every page view)
        offsets[zone_key] = {"offset": round(offset, 2), "ts": now}
        self.db.set_kv(OFFSET_KEY, json.dumps(offsets))

    # room-sensor feedback ---------------------------------------------------------

    def _trims(self) -> dict:
        raw = self.db.get_kv(TRIM_KEY)
        return json.loads(raw) if raw else {}

    def trim(self, zone_key: str) -> float:
        return self._trims().get(zone_key, {}).get("trim", 0.0)

    def update_trim(self, zone_key: str, d: ZoneDecision, now: float) -> None:
        """Integral feedback: if the room stays below (above) its target, nudge the heater setpoint up (down).

        The heater-vs-room gap alone leaves the room a few tenths off, because the gap changes while the
        heater runs. Learns only from plain targets on fresh data (`d.learn`: not blocked, no spike rule, the gap
        measured just now), only while the room is near the target (no wind-up after a target change) and
        only while the heater can act on it: a too-warm room is not learned while the heater is already
        idle (its sensor above its setpoint: the room is just cooling down, e.g. overnight), and a too-cold
        room not while the heater already runs flat out (its sensor well below its setpoint).
        """
        trims = self._trims()
        entry = trims.get(zone_key, {"trim": 0.0, "ts": now})
        dt = min(max(now - entry["ts"], 0), TRIM_MAX_STEP)
        entry["ts"] = now
        zone = self.settings.zones[zone_key]
        room = room_temp(self.db, zone.room_sensor, now)
        if d.learn and d.room_target is not None and d.offset is not None and room is not None:
            error = d.room_target - room
            heater = self.db.latest_reading(f"device:{zone.device}")
            h_temp = heater["temp"] if heater else None
            h_set = heater["setpoint"] if heater else None
            if h_temp is None or h_set is None:
                acting = False
            elif error < 0:
                acting = h_temp <= h_set + TRIM_CYCLING  # still heating at least part of the time
            else:
                acting = h_temp >= h_set - TRIM_CYCLING  # not yet running flat out
            if abs(error) <= TRIM_BAND and acting:
                entry["trim"] = max(-TRIM_MAX, min(TRIM_MAX, entry["trim"] + TRIM_GAIN * error * dt / 3600))
        trims[zone_key] = entry
        self.db.set_kv(TRIM_KEY, json.dumps(trims))

    def _hot_water_decision(self, mode: str, now: float) -> ZoneDecision:
        from .hotwater import describe, planned

        if mode != "smart":
            return ZoneDecision("handsoff", None, "Hands-off — heats whenever it wants")
        windows = planned(self.settings, self.db, datetime.fromtimestamp(now, self.tz).date())
        label = f"heats {describe(windows)}" if windows is not None else "waiting for today's prices"
        return ZoneDecision("smart", None, label)

    # applying -----------------------------------------------------------------

    async def apply_zone(self, zone_key: str) -> None:
        lock = self._locks.setdefault(zone_key, asyncio.Lock())
        async with lock:
            try:
                await self._apply(zone_key)
            except Exception as e:
                text = f"{type(e).__name__}: {e}"
                log.warning("control of %s failed: %s", zone_key, text)
                self.db.add_event("warning", f"control:{zone_key}", f"{self.settings.zones[zone_key].name}: {text}")

    async def _apply(self, zone_key: str) -> None:
        zone = self.settings.zones[zone_key]
        adapter = self.devices.get(zone.device) if zone.device else None
        if adapter is None or not adapter.capabilities.writable:
            return
        ctl = self.store.zone(zone_key)

        if ctl.mode == "handsoff":
            self._set_preheat(zone_key, None)
            if ctl.engaged:
                await self._release(zone_key, adapter, ctl)
            return

        decision = self.decide(zone_key)
        if zone.control_by == "room":
            self.update_trim(zone_key, decision, time.time())
        if decision.safety:
            self._safety.add(zone_key)
        else:
            self._safety.discard(zone_key)
        self._set_preheat(zone_key, decision.smart.preheat_for if decision.smart else None)
        if decision.blocked:
            if self._blocked_noted.get(zone_key) != decision.blocked:
                self._blocked_noted[zone_key] = decision.blocked
                self.db.add_event("info", f"control:{zone_key}", f"{zone.name}: not changed — {decision.blocked}")
            return
        self._blocked_noted.pop(zone_key, None)
        if decision.setpoint is None:
            return

        latest = self.db.latest_reading(f"device:{zone.device}")
        if not ctl.engaged:
            ctl.restore_setpoint = latest["setpoint"]
            ctl.engaged = True
            self.store.set_zone(zone_key, ctl)
            self.db.add_event("info", f"control:{zone_key}", f"{zone.name}: maska-heat took control (was {latest['setpoint']} °C)")

        target = adapter.capabilities.clamp(decision.setpoint)
        if self._device_type(zone_key) == "mill_local" and zone_key not in self._mill_taken:
            # After a restart the Mill is back on its weekly program (released on shutdown) — take it again.
            await adapter.take_control()
            self._mill_taken.add(zone_key)
            self._pending.pop(zone_key, None)
            await adapter.set_setpoint(target)
            self._pending[zone_key] = (target, time.time())
            self.db.add_event("info", f"control:{zone_key}", f"{zone.name}: set {target:.1f} °C ({self._label(decision)})")
            return
        pending = self._pending.get(zone_key)
        reported = latest["setpoint"]
        if pending and time.time() - pending[1] < PENDING_SECONDS and (reported is None or abs(reported - pending[0]) >= 0.05):
            # our last write hasn't shown up yet (Adax cloud ~5 min): the device is heading for that value, not
            # for the one it still reports — so compare with it
            if pending[0] == target:
                return
        elif reported is not None and abs(reported - target) < 0.05:
            return

        if self._device_type(zone_key) == "mill_local":
            await adapter.take_control()  # idempotent; covers a mode change made in the Mill app
        await adapter.set_setpoint(target)
        self._pending[zone_key] = (target, time.time())
        self.db.add_event("info", f"control:{zone_key}", f"{zone.name}: set {target:.1f} °C ({self._label(decision)})")

    def _set_preheat(self, zone_key: str, at: datetime | None) -> None:
        if self._preheating_for.get(zone_key) == at:
            return
        if at is None:
            self._preheating_for.pop(zone_key, None)
        else:
            self._preheating_for[zone_key] = at
        self.db.set_kv(PREHEAT_KEY, json.dumps({k: v.isoformat() for k, v in self._preheating_for.items()}))

    @staticmethod
    def _label(decision: ZoneDecision) -> str:
        return "manual" if decision.mode == "manual" else f"smart: {decision.label}"

    async def _release(self, zone_key: str, adapter: DeviceAdapter, ctl: ZoneControl) -> None:
        zone = self.settings.zones[zone_key]
        if self._device_type(zone_key) == "mill_local":
            await adapter.release_control()
            msg = "handed back to the Mill weekly program"
        elif ctl.restore_setpoint is not None:
            latest = self.db.latest_reading(f"device:{zone.device}")
            if latest is not None and latest["power_on"] == 0:
                msg = "released (device off, setpoint not restored)"
            else:
                await adapter.set_setpoint(adapter.capabilities.clamp(ctl.restore_setpoint))
                msg = f"handed back, setpoint restored to {ctl.restore_setpoint:.1f} °C"
        else:
            msg = "handed back"
        ctl = self.store.zone(zone_key)  # re-read: the mode may have been changed in the UI while we waited
        ctl.engaged = False
        ctl.restore_setpoint = None
        self._mill_taken.discard(zone_key)
        self.store.set_zone(zone_key, ctl)
        self._pending.pop(zone_key, None)
        self.db.add_event("info", f"control:{zone_key}", f"{zone.name}: {msg}")

    async def run_all(self) -> None:
        for zone_key in self.settings.zones:
            await self.apply_zone(zone_key)

    def trigger(self, zone_key: str | None = None) -> None:
        """Apply a change right away (from the UI) without waiting for the next cycle."""
        coro = self.apply_zone(zone_key) if zone_key else self.run_all()
        self.jobs.spawn(coro, f"apply:{zone_key or 'all'}")

    # lifecycle ----------------------------------------------------------------

    def start(self, initial_delay: float = 30.0) -> None:
        # the initial delay lets the collector take first readings
        self.jobs.every(RUN_SECONDS, self.run_all, "executor", initial_delay)

    async def stop(self) -> None:
        await self.jobs.stop()  # also any change still being applied: nothing is sent after the Mill is released
        # A Mill under our control goes back to its own weekly program while the app is down.
        for zone_key, zone in self.settings.zones.items():
            if self._device_type(zone_key) == "mill_local" and self.store.zone(zone_key).engaged and zone.device in self.devices:
                try:
                    await self.devices[zone.device].release_control()
                    self.db.add_event("info", f"control:{zone_key}", f"{zone.name}: app stopping — Mill back on weekly program")
                except Exception as e:
                    log.warning("could not release %s on shutdown: %s", zone_key, e)


def describe_mode(mode: str) -> str:
    return {"handsoff": "Hands-off", "manual": "Manual", "smart": "Smart", "logging": "Logging only"}[mode]


def zone_modes(settings: Settings, zone_key: str) -> list[str]:
    """Modes a zone offers in the UI: the water heater has no manual temperature."""
    if settings.hot_water and zone_key == settings.hot_water.zone:
        return ["handsoff", "smart"]
    return ["handsoff", "manual", "smart"]


__all__ = ["Executor", "ZoneDecision", "describe_mode", "zone_modes", "MODE_LABEL"]
