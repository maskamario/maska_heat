"""Price spikes: pause heating during the day's most expensive hours, unless overridden.

A spike hour costs at least `ratio` × the day's average price and at least `min_price_c`; at most `max_hours` of
them a day (the most expensive), single-hour gaps between them filled. During a spike, by default, the zones listed
in `spike.zones` use no electricity:

- rooms in Smart mode (executor): setpoint to the heater's minimum — unless the room is below `safety_min_temp`,
  then its comfort minimum; the hour before, if cheaper, they pre-heat to the comfort maximum;
- a floor thermostat (logged only otherwise): setpoint to its minimum, restored afterwards;
- the water heater: its windows avoid the spike and it does not boost.

`notify_before_min` before a spike a notification goes out with a "Keep heating" button; the override (also on the
dashboard) lets the spike pass with normal heating.
"""

from __future__ import annotations

import json
import logging
import time as _time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .db import Database
from .jobs import Jobs
from .settings import Settings

log = logging.getLogger(__name__)

OVERRIDES_KEY = "spike_overrides"
NOTIFIED_KEY = "spike_notified"
FLOOR_KEY = "spike_floor"
RUN_SECONDS = 60
RETRY_SECONDS = 5 * 60  # a failed spike warning is sent again after this
MIN_HOURS_FOR_DAY = 23


@dataclass(frozen=True)
class Spike:
    start: int  # unix s
    end: int
    peak: float  # €/kWh incl. everything
    threshold: float

    def label(self, tz: ZoneInfo) -> str:
        s, e = datetime.fromtimestamp(self.start, tz), datetime.fromtimestamp(self.end, tz)
        return f"{s:%H:%M}–{e:%H:%M}"


def day_spikes(settings: Settings, db: Database, day: date) -> list[Spike] | None:
    """Spikes of a local day, or None while its prices are not complete."""
    from .energy import hour_prices

    cfg = settings.spike
    tz = ZoneInfo(settings.location.timezone)
    start = datetime(day.year, day.month, day.day, tzinfo=tz)
    end = datetime.combine(day + timedelta(days=1), datetime.min.time(), tz)
    prices = {h: p for h, p in hour_prices(settings, db, start.timestamp(), end.timestamp()).items() if start.timestamp() <= h < end.timestamp()}
    if len(prices) < MIN_HOURS_FOR_DAY or cfg is None or not cfg.enabled:
        return None if len(prices) < MIN_HOURS_FOR_DAY else []
    mean = sum(prices.values()) / len(prices)
    threshold = max(cfg.min_price_c / 100, cfg.ratio * mean)
    hours = sorted(h for h, p in prices.items() if p >= threshold)
    hours = sorted(sorted(hours, key=lambda h: -prices[h])[: cfg.max_hours])
    filled = set(hours)
    for a, b in zip(hours, hours[1:]):
        if b - a == 2 * 3600:  # one hour between two spike hours: pause through it
            filled.add(a + 3600)
    spikes, block = [], []
    for h in sorted(filled):
        if block and h != block[-1] + 3600:
            spikes.append(block)
            block = []
        block.append(h)
    if block:
        spikes.append(block)
    return [Spike(b[0], b[-1] + 3600, max(prices[h] for h in b), threshold) for b in spikes]


def spikes_around(settings: Settings, db: Database, now: float) -> list[Spike]:
    """Known spikes of today and tomorrow."""
    tz = ZoneInfo(settings.location.timezone)
    today = datetime.fromtimestamp(now, tz).date()
    out = []
    for d in (today, today + timedelta(days=1)):
        out += day_spikes(settings, db, d) or []
    return out


def active(settings: Settings, db: Database, now: float) -> Spike | None:
    return next((s for s in spikes_around(settings, db, now) if s.start <= now < s.end), None)


def upcoming(settings: Settings, db: Database, now: float, within: float) -> Spike | None:
    return next((s for s in spikes_around(settings, db, now) if now < s.start <= now + within), None)


def next_or_active(settings: Settings, db: Database, now: float) -> Spike | None:
    return next((s for s in spikes_around(settings, db, now) if now < s.end), None)


def overridden(db: Database, spike: Spike) -> bool:
    return spike.start in json.loads(db.get_kv(OVERRIDES_KEY) or "[]")


def set_override(db: Database, start: int, on: bool) -> None:
    starts = set(json.loads(db.get_kv(OVERRIDES_KEY) or "[]"))
    starts = (starts | {start}) if on else (starts - {start})
    cutoff = _time.time() - 3 * 86400
    db.set_kv(OVERRIDES_KEY, json.dumps(sorted(s for s in starts if s > cutoff)))


def pausing(settings: Settings, db: Database, zone: str, now: float) -> Spike | None:
    """The spike pausing this zone now (in spike.zones, not overridden), if any."""
    if not settings.spike or zone not in settings.spike.zones:
        return None
    s = active(settings, db, now)
    return s if s and not overridden(db, s) else None


def preheating(settings: Settings, db: Database, zone: str, now: float) -> Spike | None:
    """A spike starting within the hour that this zone pre-heats for (only if the hour before is cheaper)."""
    from .energy import hour_prices

    cfg = settings.spike
    if not cfg or not cfg.preheat or zone not in cfg.zones:
        return None
    s = upcoming(settings, db, now, 3600)
    if not s or overridden(db, s):
        return None
    hour = int(now // 3600 * 3600)
    price = hour_prices(settings, db, hour, hour + 3600).get(hour)
    return s if price is not None and price < s.threshold else None


def paused_zones(settings: Settings, store, devices: dict) -> list[str]:
    """The spike.zones a spike actually pauses with their current modes: rooms and the water heater only in Smart
    (Manual keeps its temperature, Hands-off is left alone); a logged-only floor thermostat always."""
    out = []
    for key in settings.spike.zones if settings.spike else []:
        zone = settings.zones.get(key)
        if zone is None:
            continue
        adapter = devices.get(zone.device) if zone.device else None
        if (adapter is not None and _spike_floor(adapter)) or store.zone(key).mode == "smart":
            out.append(key)
    return out


def _spike_floor(adapter) -> bool:
    """A floor thermostat the app does not control otherwise: the spike controller pauses it itself."""
    return adapter.capabilities.kind == "floor" and not adapter.capabilities.writable


def spike_hours(settings: Settings, db: Database, day: date) -> set[int]:
    """Start minutes (local) of the day's spike hours that are not overridden — the water heater avoids them."""
    tz = ZoneInfo(settings.location.timezone)
    out = set()
    for s in day_spikes(settings, db, day) or []:
        if overridden(db, s):
            continue
        for h in range(s.start, s.end, 3600):
            local = datetime.fromtimestamp(h, tz)
            out.add(local.hour * 60)
    return out


class SpikeController:
    """Every minute: warn before a spike, pause/restore the floor thermostat, and nudge the other controllers when a
    spike starts or ends so they react right away."""

    def __init__(self, settings: Settings, db: Database, devices: dict, notifier, on_change=None, store=None):
        self.settings = settings
        self.cfg = settings.spike
        self.db = db
        self.devices = devices
        self.notifier = notifier
        self.store = store
        self.on_change = on_change or (lambda: None)
        self.tz = ZoneInfo(settings.location.timezone)
        self._last: tuple[int, bool] | None = None
        self._announced: set[int] = set()  # spikes already written to the events
        self._tried: dict[int, float] = {}  # spike start -> last failed send
        self.jobs = Jobs()

    def _paused(self) -> list[str]:
        if self.store is None:
            return [z for z in self.cfg.zones if z in self.settings.zones]
        return paused_zones(self.settings, self.store, self.devices)

    def _names(self) -> str:
        return ", ".join(self.settings.zones[z].name for z in self._paused())

    def _event(self, message: str, level: str = "info") -> None:
        self.db.add_event(level, "spike", message)

    async def run(self, now: float | None = None) -> None:
        """One round; each part on its own, so a failing notification never stops the floor being restored."""
        now = now or _time.time()
        for part in (self._notify, self._floors, self._nudge):
            try:
                await part(now)
            except Exception as e:
                log.warning("spike control failed: %s", e)
                self._event(f"{type(e).__name__}: {e}", "warning")

    async def _nudge(self, now: float) -> None:
        s = active(self.settings, self.db, now)
        state = (s.start, overridden(self.db, s)) if s else None
        if state != self._last:
            self._last = state
            self.on_change()

    async def _notify(self, now: float) -> None:
        """Warn once per spike. Marked as warned only once the message went out: during quiet hours it waits
        (a 07:00 spike is announced at 06:30, when quiet hours end), and a failed send is retried every 5 min."""
        s = upcoming(self.settings, self.db, now, self.cfg.notify_before_min * 60)
        if not s or overridden(self.db, s):
            return
        notified = set(json.loads(self.db.get_kv(NOTIFIED_KEY) or "[]"))
        if s.start in notified:
            return
        names = self._names()
        if not names:
            return  # nothing would pause (all in Manual or Hands-off): no point in a "Keep heating" button
        if s.start not in self._announced:
            self._announced.add(s.start)
            self._event(f"price spike {s.label(self.tz)} ahead — {names} will pause")

        def done() -> None:
            notified.add(s.start)
            self.db.set_kv(NOTIFIED_KEY, json.dumps(sorted(t for t in notified if t > now - 3 * 86400)))

        if not self.notifier.available:  # ntfy not set up: nothing to wait for
            done()
            return
        if self.notifier.in_quiet_hours(datetime.fromtimestamp(now, self.tz)):
            return
        if now - self._tried.get(s.start, 0) < RETRY_SECONDS:
            return
        msg = f"{s.label(self.tz)}, up to {s.peak * 100:.0f} c/kWh. Paused: {names}."
        actions = []
        url = self.settings.notify.app_url
        if url:
            base = url.rstrip("/")
            actions = [
                {"action": "http", "label": "Keep heating", "url": f"{base}/spike/{s.start}/override", "method": "POST", "clear": True},
                {"action": "view", "label": "Open app", "url": f"{base}/"},
            ]
        if await self.notifier.send("spike", "Price spike — heating pauses", msg, tags="zap", actions=actions):
            done()
        else:
            self._tried[s.start] = now

    async def _floors(self, now: float) -> None:
        """Floor thermostats are not controlled otherwise: lower their setpoint for the spike, then put it back."""
        saved = json.loads(self.db.get_kv(FLOOR_KEY) or "{}")
        for zone in self.cfg.zones:
            z = self.settings.zones.get(zone)
            adapter = self.devices.get(z.device) if z and z.device else None
            if adapter is None or not _spike_floor(adapter):
                continue
            latest = self.db.latest_reading(f"device:{z.device}")
            current = latest["setpoint"] if latest else None
            paused = pausing(self.settings, self.db, zone, now)
            low = adapter.capabilities.setpoint_min
            if paused and zone not in saved and current is not None:
                await adapter.set_setpoint(low)
                saved[zone] = current
                self.db.set_kv(FLOOR_KEY, json.dumps(saved))
                self._event(f"{z.name}: paused for the price spike {paused.label(self.tz)} (was {current:.1f} °C)")
            elif not paused and zone in saved:
                restore = saved.pop(zone)
                if current is None or abs(current - low) < 0.05:  # leave it if someone changed it meanwhile
                    await adapter.set_setpoint(restore)
                    self._event(f"{z.name}: back to {restore:.1f} °C after the price spike")
                self.db.set_kv(FLOOR_KEY, json.dumps(saved))

    def start(self) -> None:
        self.jobs.every(RUN_SECONDS, self.run, "spike")

    async def stop(self) -> None:
        await self.jobs.stop()
