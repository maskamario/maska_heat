"""Price-aware hot water: let the water heater heat only in the cheapest hours of each day.

The boiler's own weekly time-slot schedule (up to 3 windows a day) is rewritten every day from the electricity
prices, and its operating range set to "heat on schedule". If the app, the Pi or the cloud is down, the boiler keeps
following the last schedule it got — hot water never depends on the app being up.

Per day: `hours_per_day` hours in the cheapest hours, of which at least `morning_hours` before `morning_by` (tank full
for morning showers) and `evening_hours` between `evening_from` and `evening_by` (top-up before the evening wash).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .adapters.cozytouch import MAX_WINDOWS


@dataclass(frozen=True)
class HotWaterRules:
    hours_per_day: int = 6
    morning_by: time = time(6, 30)
    morning_hours: int = 3
    evening_from: time = time(12, 0)
    evening_by: time = time(19, 0)
    evening_hours: int = 1
    boost_below_pct: float = 30.0


def _minute(t: time) -> int:
    return t.hour * 60 + t.minute


def plan_day(prices: dict[int, float], rules: HotWaterRules) -> list[tuple[int, int]]:
    """Heating windows (start, end) in minutes since local midnight from {hour start minute: €/kWh}.

    Hours are whole clock hours; a requirement only counts hours that end by its deadline.
    """
    hours = sorted(prices)
    if not hours:
        return []
    chosen: set[int] = set()

    def take(candidates: list[int], n: int) -> None:
        for h in sorted((h for h in candidates if h not in chosen), key=lambda h: prices[h])[: max(n, 0)]:
            chosen.add(h)

    morning = [h for h in hours if h + 60 <= _minute(rules.morning_by)]
    evening = [h for h in hours if h >= _minute(rules.evening_from) and h + 60 <= _minute(rules.evening_by)]
    take(morning, rules.morning_hours)
    take(evening, rules.evening_hours - len(chosen & set(evening)))
    take(hours, rules.hours_per_day - len(chosen))

    windows = _windows(chosen)
    while len(windows) > MAX_WINDOWS:  # the boiler takes 3 windows a day: fill the cheapest gap
        gaps = [
            (sum(prices.get(h, 0.0) for h in range(windows[i][1], windows[i + 1][0], 60)), i)
            for i in range(len(windows) - 1)
        ]
        _, i = min(gaps)
        chosen.update(range(windows[i][1], windows[i + 1][0], 60))
        windows = _windows(chosen)

    def ok(hs: set[int]) -> bool:
        return (
            len(hs & set(morning)) >= min(rules.morning_hours, len(morning))
            and len(hs & set(evening)) >= min(rules.evening_hours, len(evening))
            and len(_windows(hs)) <= MAX_WINDOWS
        )

    # Filling gaps can overshoot the hours: drop the most expensive window ends while the rules still hold.
    while len(chosen) > rules.hours_per_day:
        ends = {h for a, b in _windows(chosen) for h in (a, b - 60)}
        for h in sorted(ends, key=lambda h: -prices.get(h, 0.0)):
            if ok(chosen - {h}):
                chosen.discard(h)
                break
        else:
            break
    return _windows(chosen)


def _windows(hours: set[int]) -> list[tuple[int, int]]:
    out: list[list[int]] = []
    for h in sorted(hours):
        if out and out[-1][1] == h:
            out[-1][1] = h + 60
        else:
            out.append([h, h + 60])
    return [(a, b) for a, b in out]


def day_prices(hour_prices: dict[int, float], day: date, tz: ZoneInfo) -> dict[int, float]:
    """{minute of the local day: €/kWh} for one local day, from {unix hour start: €/kWh}."""
    start = datetime(day.year, day.month, day.day, tzinfo=tz)
    end = datetime.combine(day + timedelta(days=1), time(), tz)
    out = {}
    for ts, price in hour_prices.items():
        local = datetime.fromtimestamp(ts, tz)
        if start <= local < end:
            out.setdefault(local.hour * 60, price)  # the repeated hour on the DST day keeps its first price
    return out


def in_window(windows: list[tuple[int, int]], local: datetime) -> bool:
    m = local.hour * 60 + local.minute
    return any(a <= m < b for a, b in windows)


def describe(windows: list[tuple[int, int]]) -> str:
    return ", ".join(f"{a // 60:02d}:{a % 60:02d}–{b // 60:02d}:{b % 60:02d}" for a, b in windows) or "none"


# controller -----------------------------------------------------------------------------------------

import asyncio  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import time as _time  # noqa: E402

from .adapters.cozytouch import (  # noqa: E402
    CAP_BOOST,
    CAP_RANGE,
    CAP_SCHEDULE,
    RANGE_SCHEDULE,
    CozytouchWaterHeaterAdapter,
    parse_schedule,
    schedule_value,
)

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
        self._task: asyncio.Task | None = None

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
        if str(caps.get(CAP_RANGE)) != str(RANGE_SCHEDULE):
            await adapter.set_operating_range(RANGE_SCHEDULE)
            self._event("set to heat on schedule")

        level = current.level_pct
        if (
            level is not None
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
        for wd, value in (state.get("restore_schedule") or {}).items():
            if value is not None:
                await adapter.set_day_schedule(int(wd), parse_schedule(value))
        if state.get("restore_range") is not None:
            await adapter.set_operating_range(int(state["restore_range"]))
        self._save({"engaged": False})
        self._event("handed back — the boiler's own mode and schedule restored")

    def trigger(self) -> None:
        asyncio.get_running_loop().create_task(self.run())

    def start(self, initial_delay: float = 60.0) -> None:
        async def loop() -> None:
            await asyncio.sleep(initial_delay)
            while True:
                await self.run()
                await asyncio.sleep(RUN_SECONDS)

        self._task = asyncio.create_task(loop(), name="hotwater")

    async def stop(self) -> None:
        # Nothing to undo: the boiler keeps following its last schedule while the app is down.
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
