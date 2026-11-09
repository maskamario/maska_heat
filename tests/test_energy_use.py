from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from maska_heat.db import Reading
from maska_heat.energy import adax_hours, period_bounds, power_hours, report_hours, summarize
from maska_heat.settings import Settings
from maska_heat.tariff import elec_cost
from maska_heat.web.app import create_app

TZ = ZoneInfo("Europe/Tallinn")
DAY = datetime(2026, 11, 5, tzinfo=TZ)  # a Thursday
T0 = int(DAY.timestamp())
H = 3600


def at(hour: float) -> int:
    return int(T0 + hour * H)


def mill(db, hour: float, watts: float, until: float | None = None):
    """A Mill reading at `hour`, repeated every minute (its poll interval) until `until`."""
    ts = range(at(hour), at(until), 60) if until else [at(hour)]
    db.add_readings([Reading(t, "device:matis_mill", temp=20, setpoint=21, power_on=True, power_w=watts) for t in ts])


def test_power_is_held_until_next_reading_and_split_by_hour(db):
    mill(db, 7.5, 1500, until=8.25)  # on 07:30–08:15
    mill(db, 8.25, 0)
    hours = power_hours(db, "device:matis_mill", at(0), at(24), interval=60)
    assert hours == {at(7): pytest.approx(0.75), at(8): pytest.approx(0.375)}


def test_power_gap_is_not_counted(db):
    mill(db, 10, 1500)  # last reading, then the device went silent: held 3 × 60 s only
    hours = power_hours(db, "device:matis_mill", at(0), at(24), interval=60)
    assert sum(hours.values()) == pytest.approx(1.5 * 180 / H)


def test_power_clipped_to_period(db):
    mill(db, -0.5, 1500)  # started before the period
    mill(db, 0.5, 0)
    hours = power_hours(db, "device:matis_mill", at(0), at(24), interval=3600)
    assert hours == {at(0): pytest.approx(0.75)}


def adax(db, hour: float, kwh: float):
    db.add_readings([Reading(at(hour), "device:madli_adax", temp=20, setpoint=21, energy_kwh=kwh)])


def test_adax_hourly_counter_is_extended_to_the_full_hour(db):
    for minute, kwh in [(5, 0.02), (30, 0.1), (58, 0.29)]:
        adax(db, 6 + minute / 60, kwh)
    adax(db, 7 + 3 / 60, 0.0)  # counter reset
    adax(db, 7 + 20 / 60, 0.05)  # current hour: not extended
    hours = adax_hours(db, "device:madli_adax", at(0), at(24), now=at(7.4))
    assert hours[at(6)] == pytest.approx(0.30)
    assert hours[at(7)] == pytest.approx(0.05)

    adax(db, 9 + 10 / 60, 0.1)  # went silent at minute 10: extended at most ×60/45
    hours = adax_hours(db, "device:madli_adax", at(0), at(24), now=at(12))
    assert hours[at(9)] == pytest.approx(0.1 * 60 / 45)


def test_report_hours_scaled_to_daily_total(db):
    src = "device:living_mitsubishi"
    db.upsert_energy(src, [(at(h), 60, 0.575) for h in (1, 2, 3, 4)] + [(T0, 1440, 2.0)])
    db.upsert_energy(src, [(T0 + 86400, 1440, 2.4)])  # next day: daily total only
    hours = report_hours(db, src, T0, T0 + 2 * 86400, now=T0 + 2 * 86400, tz=TZ)
    assert hours[at(1)] == pytest.approx(0.5) and sum(v for h, v in hours.items() if h < T0 + 86400) == pytest.approx(2.0)
    next_day = [v for h, v in hours.items() if h >= T0 + 86400]
    assert len(next_day) == 24 and sum(next_day) == pytest.approx(2.4)


def test_period_bounds_across_dst():
    now = datetime(2026, 10, 26, 9, 30, tzinfo=TZ).timestamp()  # day after DST ended
    start, end = period_bounds("yesterday", now, TZ)
    assert (start.isoformat(), end.isoformat()) == ("2026-10-25T00:00:00+03:00", "2026-10-26T00:00:00+02:00")
    assert end.timestamp() - start.timestamp() == 25 * H
    start, _ = period_bounds("7d", now, TZ)
    assert start.date().isoformat() == "2026-10-20"
    start, end = period_bounds("month", now, TZ)
    assert start.day == 1 and end.timestamp() == now


@pytest.fixture
def priced(settings, db):
    db.upsert_prices([(at(h), 60, 100.0 + 10 * h) for h in range(24)])
    return settings


def test_summary_costs_each_hour_at_its_price(priced, db):
    mill(db, 7, 1500, until=8)  # 07:00–08:00 → 1.5 kWh
    mill(db, 8, 0)
    adax(db, 3 + 58 / 60, 0.58)  # 03:00–04:00 → 0.6 kWh
    s = summarize(priced, db, "yesterday", now=T0 + 86400 + 600)
    by_zone = {d.zone: d for d in s.devices}
    price = lambda h: elec_cost(100.0 + 10 * h, DAY + timedelta(hours=h), priced.tariff).total  # noqa: E731
    assert by_zone["matis"].kwh == pytest.approx(1.5) and by_zone["matis"].cost == pytest.approx(1.5 * price(7))
    assert by_zone["madli"].kwh == pytest.approx(0.6) and by_zone["madli"].cost == pytest.approx(0.6 * price(3))
    assert "living" not in by_zone  # device disabled
    assert s.kwh == pytest.approx(2.1) and s.cost == pytest.approx(1.5 * price(7) + 0.6 * price(3))
    assert s.avg_price == pytest.approx(s.cost / 2.1)
    assert s.period_price == pytest.approx(sum(price(h) for h in range(24)) / 24)
    assert len(s.buckets) == 24 and s.buckets[7]["kwh"]["matis"] == pytest.approx(1.5)


def test_device_without_readings_has_no_data(priced, db):
    mill(db, 7, 0)
    s = summarize(priced, db, "yesterday", now=T0 + 86400 + 600)
    by_zone = {d.zone: d for d in s.devices}
    assert by_zone["matis"].has_data and by_zone["matis"].kwh == 0
    assert not by_zone["madli"].has_data


def test_unpriced_hours_are_reported(settings, db):
    mill(db, 7, 1500, until=8)
    mill(db, 8, 0)
    s = summarize(settings, db, "yesterday", now=T0 + 86400 + 600)
    assert s.unpriced_kwh == pytest.approx(1.5) and s.cost == 0 and s.avg_price is None


def test_energy_page_and_api(settings, db):
    import time

    now = time.time()
    db.add_readings([Reading(int(now) - 1800 + 60 * i, "device:matis_mill", temp=20, power_w=1500 if i < 15 else 0) for i in range(30)])
    app = create_app(settings, {}, start_jobs=False, db=db)
    with TestClient(app) as client:
        for period in ("today", "yesterday", "7d", "month"):
            assert client.get("/energy", params={"period": period}).status_code == 200
        page = client.get("/energy").text
        assert "Matis&#39;s bedroom" in page and "measured power" in page and "no data" in page
        data = client.get("/api/energy", params={"period": "today"}).json()
        matis = next(d for d in data["devices"] if d["zone"] == "matis")
        assert matis["kwh"] == pytest.approx(0.375, abs=0.01) and data["by_hour"]
        assert client.get("/api/energy", params={"period": "year"}).status_code == 400
        assert "Heating today" in client.get("/").text


def test_settings_default_boiler_poll(settings):
    raw = settings.model_dump(mode="json")
    raw["devices"]["water_heater"] = {"type": "cozytouch"}
    assert Settings.model_validate(raw).devices["water_heater"].interval == 120
