"""Price-aware hot water: let the water heater heat only in the cheapest hours of each day (controller; the
planner is in hotwater_plan.py).

The boiler's own weekly time-slot schedule (up to 3 windows a day) is rewritten every day from the electricity
prices, its operating range set to "heat on schedule" and its target temperature held constant (in its own
"Automatic" mode the boiler lowers the target after a quiet week and then skips the cheap windows). If the app, the Pi or the cloud is down, the boiler keeps
following the last schedule it got — hot water never depends on the app being up.

Per day: `hours_per_day` hours in the cheapest hours, of which at least `morning_hours` before `morning_by` (tank full
for morning showers) and `evening_hours` between `evening_from` and `evening_by` (top-up before the evening wash).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time as _time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .adapters.cozytouch import (
    CAP_BOOST,
    CAP_RANGE,
    CAP_SCHEDULE,
    CAP_SETPOINT,
    CAP_SETPOINT_MODE,
    RANGE_SCHEDULE,
    SETPOINT_CONSTANT,
    CozytouchWaterHeaterAdapter,
    parse_schedule,
)
from .hotwater_plan import HotWaterRules, day_prices, describe, in_window, plan_day  # noqa: F401  (re-exported)
from .jobs import Jobs

log = logging.getLogger(__name__)

RUN_SECONDS = 10 * 60
BOOST_COOLDOWN = 3 * 3600
STATE_KEY = "hotwater"
MIN_HOURS_FOR_PLAN = 23  # a day's prices must be complete (23 h on the DST day)


def rules_from(cfg) -> HotWaterRules:
    return HotWaterRules(
        hours_per_day=cfg.hours_per_day,
        morning_by=cfg.morning_by,
        morning_hours=cfg.morning_hours,
        evening_from=cfg.evening_from,
        evening_by=cfg.evening_by,
        evening_hours=cfg.evening_hours,
        boost_below_pct=cfg.boost_below_pct,
    )


def planned(settings, db, day: date) -> list[tuple[int, int]] | None:
    """The windows for a local day, or None while its prices are not complete."""
    from .energy import hour_prices

    tz = ZoneInfo(settings.location.timezone)
    start = datetime(day.year, day.month, day.day, tzinfo=tz).timestamp()
    prices = day_prices(hour_prices(settings, db, start, start + 25 * 3600), day, tz)
    if len(prices) < MIN_HOURS_FOR_PLAN:
        return None
    if settings.spike and settings.hot_water.zone in settings.spike.zones:
        from .spike import spike_hours

        skip = spike_hours(settings, db, day)
        return plan_day(prices, rules_from(settings.hot_water), avoid=skip)  # never heat in a price spike
    return plan_day(prices, rules_from(settings.hot_water))


def _float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def starts_within(today: list | None, tomorrow: list | None, local: datetime, minutes: int) -> bool:
    """Whether a heating window starts in the next `minutes` (tomorrow's first one counts just before midnight)."""
    m = local.hour * 60 + local.minute
    starts = [a for a, _ in today or []] + [a + 1440 for a, _ in tomorrow or []]
    return any(0 < a - m <= minutes for a in starts)


class HotWaterController:
    """Applies the zone's mode to the water heater: Smart = price-aware schedule, Hands-off = as it was before."""

    def __init__(self, settings, db, store, devices, notifier):
        self.settings = settings
        self.cfg = settings.hot_water
        self.db = db
        self.store = store
        self.devices = devices
        self.notifier = notifier
        self.tz = ZoneInfo(settings.location.timezone)
        self._lock = asyncio.Lock()
        self.jobs = Jobs()

    @property
    def zone(self) -> str:
        return self.cfg.zone

    def _adapter(self) -> CozytouchWaterHeaterAdapter | None:
        device = self.settings.zones[self.zone].device
        adapter = self.devices.get(device)
        return adapter if isinstance(adapter, CozytouchWaterHeaterAdapter) or hasattr(adapter, "set_day_schedule") else None

    def state(self) -> dict:
        raw = self.db.get_kv(STATE_KEY)
        return json.loads(raw) if raw else {"engaged": False}

    def _save(self, state: dict) -> None:
        self.db.set_kv(STATE_KEY, json.dumps(state))

    def _event(self, message: str, level: str = "info") -> None:
        self.db.add_event(level, f"control:{self.zone}", f"{self.settings.zones[self.zone].name}: {message}")

    async def run(self, now: float | None = None) -> None:
        async with self._lock:
            try:
                await self._run(now or _time.time())
            except Exception as e:
                log.warning("hot water control failed: %s", e)
                self._event(f"{type(e).__name__}: {e}", "warning")

    async def _run(self, now: float) -> None:
        adapter = self._adapter()
        if adapter is None:
            return
        state = self.state()
        mode = self.store.zone(self.zone).mode
        if mode != "smart":
            if state.get("engaged"):
                await self._release(adapter, state)
            return

        current = await adapter.read()
        caps = current.raw["capabilities"]
        if not state.get("engaged"):
            state = {
                "engaged": True,
                "restore_range": caps.get(CAP_RANGE),
                "restore_schedule": {str(wd): caps.get(cap) for wd, cap in CAP_SCHEDULE.items()},
                "last_boost": 0,
            }
            self._save(state)
            self._event("maska-heat took control — heats in the cheapest hours")

        if "restore_setpoint_mode" not in state:  # also for a boiler taken over before the target was held
            state["restore_setpoint_mode"] = caps.get(CAP_SETPOINT_MODE)
            state["restore_setpoint"] = caps.get(CAP_SETPOINT)
            self._save(state)

        local = datetime.fromtimestamp(now, self.tz)
        today_windows = tomorrow_windows = None
        for day in (local.date(), local.date() + timedelta(days=1)):
            windows = planned(self.settings, self.db, day)
            if day == local.date():
                today_windows = windows
            else:
                tomorrow_windows = windows
            if windows is None:
                continue
            cap = CAP_SCHEDULE[day.weekday()]
            if parse_schedule(caps.get(cap)) != windows:
                await adapter.set_day_schedule(day.weekday(), windows)
                self._event(f"{day:%a %d.%m} heats {describe(windows)}")
        if caps.get(CAP_RANGE) is not None and str(caps.get(CAP_RANGE)) != str(RANGE_SCHEDULE):  # None: not reported
            await adapter.set_operating_range(RANGE_SCHEDULE)
            self._event("set to heat on schedule")
        await self._hold_target(adapter, caps, state)

        level = current.level_pct
        from .spike import pausing

        if (
            not pausing(self.settings, self.db, self.zone, now)  # no boost in a price spike
            and level is not None
            and level < self.cfg.boost_below_pct
            and not (today_windows and in_window(today_windows, local))
            and not starts_within(today_windows, tomorrow_windows, local, self.cfg.boost_wait_min)
            and now - state.get("last_boost", 0) > BOOST_COOLDOWN
            and caps.get(CAP_BOOST) != "1"
        ):
            await adapter.boost()
            state["last_boost"] = now
            self._save(state)
            msg = f"hot water at {level:.0f} % outside the cheap hours — boost started"
            self._event(msg)
            await self.notifier.send("hotwater", "Hot water boost", msg, tags="droplet")

    async def _hold_target(self, adapter, caps: dict, state: dict) -> None:
        """Constant target temperature while Smart. A refused write must not stop the schedule or the boost: it is
        reported once and tried again on the next run."""
        want = self.cfg.water_temp
        if want is None or caps.get(CAP_SETPOINT_MODE) is None:  # not configured / not reported
            return
        try:
            if str(caps.get(CAP_SETPOINT_MODE)) != str(SETPOINT_CONSTANT):
                await adapter.write(CAP_SETPOINT_MODE, str(SETPOINT_CONSTANT))
                self._event(f"target temperature fixed (was the boiler's own choice, {caps.get(CAP_SETPOINT)} °C)")
            if _float(caps.get(CAP_SETPOINT)) != float(want):
                await adapter.write(CAP_SETPOINT, str(want))
                self._event(f"target temperature set to {want} °C")
            error = None
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            if state.get("target_error") != error:
                self._event(f"could not fix the target temperature — {error}", "warning")
        if state.get("target_error") != error:
            state["target_error"] = error
            self._save(state)

    async def _release(self, adapter, state: dict) -> None:
        """Put the boiler's own schedule and mode back: only what differs, the saved values as they were (not
        re-parsed), and progress saved after every day — Cozytouch allows ~30 requests at a time and each write
        costs several, so a hand-back cut short by the rate limit continues where it stopped on the next run."""
        caps = (await adapter.read()).raw["capabilities"]
        pending = state.get("restore_schedule") or {}
        for wd in sorted(pending):
            value = pending[wd]
            if value is not None and caps.get(CAP_SCHEDULE[int(wd)]) != value:
                await adapter.write(CAP_SCHEDULE[int(wd)], value)
            state["restore_schedule"] = pending = {k: v for k, v in pending.items() if k != wd}
            self._save(state)
        rng = state.get("restore_range")
        if rng is not None and str(caps.get(CAP_RANGE)) != str(rng):
            await adapter.set_operating_range(int(rng))
        mode, temp = state.get("restore_setpoint_mode"), state.get("restore_setpoint")
        if mode is not None and caps.get(CAP_SETPOINT_MODE) is not None:
            # A target the boiler chose itself is not written back: it picks its own again in that mode.
            if str(mode) == str(SETPOINT_CONSTANT) and temp is not None and _float(caps.get(CAP_SETPOINT)) != _float(temp):
                await adapter.write(CAP_SETPOINT, str(temp))
            if str(caps.get(CAP_SETPOINT_MODE)) != str(mode):
                await adapter.write(CAP_SETPOINT_MODE, str(mode))
        self._save({"engaged": False})
        self._event("handed back — the boiler's own mode and schedule restored")

    def trigger(self) -> None:
        self.jobs.spawn(self.run(), "hotwater:apply")

    def start(self, initial_delay: float = 60.0) -> None:
        self.jobs.every(RUN_SECONDS, self.run, "hotwater", initial_delay)

    async def stop(self) -> None:
        # Nothing to undo: the boiler keeps following its last schedule while the app is down.
        await self.jobs.stop()
