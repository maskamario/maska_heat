"""Hot water planner (pure functions): which hours of a day the water heater heats, from that day's prices.

Per day: `hours_per_day` hours in the cheapest hours, of which at least `morning_hours` before `morning_by` (tank full
for morning showers) and `evening_hours` between `evening_from` and `evening_by` (top-up before the evening wash).
"""

from __future__ import annotations

import math
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


def plan_day(prices: dict[int, float], rules: HotWaterRules, avoid: set[int] = frozenset()) -> list[tuple[int, int]]:
    """Heating windows (start, end) in minutes since local midnight from {hour start minute: €/kWh}.

    Hours are whole clock hours; a requirement only counts hours that end by its deadline. Hours in `avoid` (price
    spikes) are never heated — not even to merge two windows into one.
    """
    hours = sorted(h for h in prices if h not in avoid)
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
        gaps = [(_cost(prices, avoid, range(windows[i][1], windows[i + 1][0], 60)), i) for i in range(len(windows) - 1)]
        cost, i = min(gaps)
        if cost == math.inf:  # every gap crosses a spike: give up the smallest, priciest window instead
            drop = min(windows, key=lambda w: (w[1] - w[0], -_cost(prices, avoid, range(*w, 60))))
            chosen -= set(range(*drop, 60))
        else:
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
        for h in sorted(ends, key=lambda h: -prices.get(h, math.inf)):
            if ok(chosen - {h}):
                chosen.discard(h)
                break
        else:
            break
    return _windows(chosen)


def _cost(prices: dict[int, float], avoid: set[int], hours: range) -> float:
    """Price of heating these hours: a spike hour can't be heated (infinite); the hour that does not exist on the
    spring DST day costs nothing."""
    return sum(math.inf if h in avoid else prices.get(h, 0.0) for h in hours)


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
