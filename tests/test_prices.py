from datetime import datetime, timedelta, timezone

import httpx
import pytest

from maska_heat.providers.prices import PricePoint, fetch_prices, to_slots

T0 = datetime(2026, 10, 9, 21, 0, tzinfo=timezone.utc)


def quarters(start: datetime, prices: list[float]) -> list[PricePoint]:
    return [PricePoint(start + timedelta(minutes=15 * i), 15, p) for i, p in enumerate(prices)]


def test_hourly_slot_is_mean_of_quarters():
    slots = to_slots(quarters(T0, [10, 20, 30, 40, 100, 100, 100, 100]), 60)
    assert slots == [PricePoint(T0, 60, 25.0), PricePoint(T0 + timedelta(hours=1), 60, 100.0)]


def test_incomplete_hour_is_dropped():
    slots = to_slots(quarters(T0, [10, 20, 30, 40, 50, 60]), 60)
    assert [s.start for s in slots] == [T0]


def test_15_min_slots_pass_through():
    pts = quarters(T0, [1, 2, 3])
    assert to_slots(pts, 15) == pts


def test_cannot_split_hourly_into_quarters():
    with pytest.raises(ValueError):
        to_slots([PricePoint(T0, 60, 1.0)], 15)


async def test_fetch_prices_parses_elering_response():
    body = {
        "success": True,
        "data": {
            "ee": [
                {"timestamp": int((T0 + timedelta(minutes=15)).timestamp()), "price": 18.07},
                {"timestamp": int(T0.timestamp()), "price": 18.71},
            ],
            "fi": [],
        },
    }
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=body))
    async with httpx.AsyncClient(transport=transport) as client:
        pts = await fetch_prices(T0, T0 + timedelta(hours=1), client=client)
    assert pts == [PricePoint(T0, 15, 18.71), PricePoint(T0 + timedelta(minutes=15), 15, 18.07)]
