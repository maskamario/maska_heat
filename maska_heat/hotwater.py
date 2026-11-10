"""Price-aware hot water: let the water heater heat only in the cheapest hours of each day (controller; the
planner is in hotwater_plan.py).

The boiler's own weekly time-slot schedule (up to 3 windows a day) is rewritten every day from the electricity
prices, and its operating range set to "heat on schedule". If the app, the Pi or the cloud is down, the boiler keeps
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
    RANGE_SCHEDULE,
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

        local = datetime.fromtimestamp(now, self.tz)
        today_windows = None
        for day in (local.date(), local.date() + timedelta(days=1)):
            windows = planned(self.settings, self.db, day)
            if day == local.date():
                today_windows = windows
            if windows is None:
                continue
            cap = CAP_SCHEDULE[day.weekday()]
            if parse_schedule(caps.get(cap)) != windows:
                await adapter.set_day_schedule(day.weekday(), windows)
                self._event(f"{day:%a %d.%m} heats {describe(windows)}")
        if caps.get(CAP_RANGE) is not None and str(caps.get(CAP_RANGE)) != str(RANGE_SCHEDULE):  # None: not reported
            await adapter.set_operating_range(RANGE_SCHEDULE)
            self._event("set to heat on schedule")

        level = current.level_pct
        from .spike import pausing

        if (
            not pausing(self.settings, self.db, self.zone, now)  # no boost in a price spike
            and level is not None
            and level < self.cfg.boost_below_pct
            and not (today_windows and in_window(today_windows, local))
            and now - state.get("last_boost", 0) > BOOST_COOLDOWN
            and caps.get(CAP_BOOST) != "1"
        ):
            await adapter.boost()
            state["last_boost"] = now
            self._save(state)
            msg = f"hot water at {level:.0f} % outside the cheap hours — boost started"
            self._event(msg)
            await self.notifier.send("hotwater", "Hot water boost", msg, tags="droplet")

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
        self._save({"engaged": False})
        self._event("handed back — the boiler's own mode and schedule restored")

    def trigger(self) -> None:
        self.jobs.spawn(self.run(), "hotwater:apply")

    def start(self, initial_delay: float = 60.0) -> None:
        self.jobs.every(RUN_SECONDS, self.run, "hotwater", initial_delay)

    async def stop(self) -> None:
        # Nothing to undo: the boiler keeps following its last schedule while the app is down.
        await self.jobs.stop()
