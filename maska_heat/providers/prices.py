"""Nord Pool day-ahead prices from the Elering dashboard API.

Prices come in 15-minute resolution (since Oct 2025). `to_slots` aggregates them to the planning slot
length; with an hourly meter the hourly price is the mean of its four quarter prices.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

API_URL = "https://dashboard.elering.ee/api/nps/price"


@dataclass(frozen=True)
class PricePoint:
    start: datetime  # UTC
    minutes: int
    eur_mwh: float


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


async def fetch_prices(
    start: datetime, end: datetime, area: str = "ee", client: httpx.AsyncClient | None = None
) -> list[PricePoint]:
    """Raw price points with start in [start, end)."""
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=15.0)
    try:
        r = await client.get(API_URL, params={"start": _iso(start), "end": _iso(end - timedelta(milliseconds=1))})
        r.raise_for_status()
        body = r.json()
    finally:
        if own_client:
            await client.aclose()
    rows = sorted(body["data"].get(area, []), key=lambda p: p["timestamp"])
    if not rows:
        return []
    starts = [datetime.fromtimestamp(p["timestamp"], tz=timezone.utc) for p in rows]
    step = int((starts[1] - starts[0]).total_seconds() // 60) if len(starts) > 1 else 60
    return [PricePoint(s, step, float(p["price"])) for s, p in zip(starts, rows)]


def to_slots(points: list[PricePoint], slot_minutes: int = 60) -> list[PricePoint]:
    """Average raw points into slots of `slot_minutes`. Incomplete slots are dropped."""
    buckets: dict[datetime, list[PricePoint]] = defaultdict(list)
    for p in points:
        if p.minutes > slot_minutes or slot_minutes % p.minutes:
            raise ValueError(f"cannot build {slot_minutes}-min slots from {p.minutes}-min prices")
        offset = (p.start.hour * 60 + p.start.minute) % slot_minutes
        buckets[p.start - timedelta(minutes=offset)].append(p)
    slots = []
    for start in sorted(buckets):
        group = buckets[start]
        if sum(p.minutes for p in group) == slot_minutes:
            slots.append(PricePoint(start, slot_minutes, round(sum(p.eur_mwh for p in group) / len(group), 4)))
    return slots
