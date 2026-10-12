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
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from .adapters.base import DeviceAdapter
from .control import ControlStore, ZoneControl
from .dashboard import zone_views
from .db import Database
from .schedule import MODE_LABEL, Snapshot, SmartTarget, smart_target
from .settings import Settings

log = logging.getLogger(__name__)

RUN_SECONDS = 5 * 60
PENDING_SECONDS = 10 * 60  # don't re-send the same value within this time
FRESH_READING_SECONDS = 15 * 60


@dataclass
class ZoneDecision:
    mode: str  # handsoff | manual | smart
    setpoint: float | None
    label: str
    smart: SmartTarget | None = None
    blocked: str | None = None  # why nothing can be sent right now


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
        self._task: asyncio.Task | None = None

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

    def decide(self, zone_key: str, now: float | None = None, snap: Snapshot | None = None) -> ZoneDecision:
        now = now or time.time()
        ctl = self.store.zone(zone_key)
        zone = self.settings.zones[zone_key]
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

        if ctl.mode == "manual":
            sp = ctl.manual_setpoint
            return ZoneDecision("manual", sp, "Manual" if sp is not None else "Manual — no temperature set", blocked=blocked)

        view = next(v for v in zone_views(self.settings, self.db, now) if v.key == zone_key)
        kind = self.devices[zone.device].capabilities.kind
        target = smart_target(zone_key, datetime.fromtimestamp(now, self.tz), view.temp, kind, snap or self.snapshot())
        return ZoneDecision("smart", target.setpoint, target.reason, smart=target, blocked=blocked)

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
        if adapter is None:
            return
        ctl = self.store.zone(zone_key)

        if ctl.mode == "handsoff":
            if ctl.engaged:
                await self._release(zone_key, adapter, ctl)
            return

        decision = self.decide(zone_key)
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
        if pending and pending[0] == target and time.time() - pending[1] < PENDING_SECONDS:
            return
        if latest["setpoint"] is not None and abs(latest["setpoint"] - target) < 0.05:
            return

        if self._device_type(zone_key) == "mill_local":
            await adapter.take_control()  # idempotent; covers a mode change made in the Mill app
        await adapter.set_setpoint(target)
        self._pending[zone_key] = (target, time.time())
        self.db.add_event("info", f"control:{zone_key}", f"{zone.name}: set {target:.1f} °C ({self._label(decision)})")

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
        asyncio.get_running_loop().create_task(coro)

    # lifecycle ----------------------------------------------------------------

    def start(self, initial_delay: float = 30.0) -> None:
        async def loop() -> None:
            await asyncio.sleep(initial_delay)  # let the collector take first readings
            while True:
                await self.run_all()
                await asyncio.sleep(RUN_SECONDS)

        self._task = asyncio.create_task(loop(), name="executor")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        # A Mill under our control goes back to its own weekly program while the app is down.
        for zone_key, zone in self.settings.zones.items():
            if self._device_type(zone_key) == "mill_local" and self.store.zone(zone_key).engaged and zone.device in self.devices:
                try:
                    await self.devices[zone.device].release_control()
                    self.db.add_event("info", f"control:{zone_key}", f"{zone.name}: app stopping — Mill back on weekly program")
                except Exception as e:
                    log.warning("could not release %s on shutdown: %s", zone_key, e)


def describe_mode(mode: str) -> str:
    return {"handsoff": "Hands-off", "manual": "Manual", "smart": "Smart"}[mode]


__all__ = ["Executor", "ZoneDecision", "describe_mode", "MODE_LABEL"]
