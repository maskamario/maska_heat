import sqlite3
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from maska_heat.adapters.melcloud import MelcloudAdapter, _parse_last_seen
from maska_heat.collector import ENERGY_DAYS, ENERGY_HOURLY_DAYS, Collector
from maska_heat.db import Database, Reading
from maska_heat.notify import Notifier

TZ = ZoneInfo("Europe/Tallinn")


def test_last_seen_with_and_without_fraction():
    assert _parse_last_seen("2026-10-26T06:20:29") == datetime(2026, 10, 26, 6, 20, 29, tzinfo=timezone.utc)
    assert _parse_last_seen("2026-10-26T06:20:29.123") == datetime(2026, 10, 26, 6, 20, 29, 123000, tzinfo=timezone.utc)
    assert _parse_last_seen(None) is None and _parse_last_seen("garbage") is None


def _hourly_report(heating, dry=None):
    return {"LabelType": 0, "Labels": list(range(len(heating))), "Heating": heating, "Dry": dry or [], "Cooling": None}


async def test_energy_hours_sums_modes_per_local_hour(monkeypatch):
    mel = MelcloudAdapter("e", "p")

    async def report(start, end):
        return _hourly_report([0, 0.575] + [0] * 22, dry=[0.1])

    monkeypatch.setattr(mel, "energy_report", report)
    hours = await mel.energy_hours(date(2026, 11, 2), TZ)
    assert len(hours) == 24
    assert hours[0] == (datetime(2026, 11, 2, 0, 0, tzinfo=TZ), 0.1)
    assert hours[1] == (datetime(2026, 11, 2, 1, 0, tzinfo=TZ), 0.575)


async def test_energy_hours_on_dst_days_follow_wall_clock_labels(monkeypatch):
    mel = MelcloudAdapter("e", "p")

    async def report(start, end):
        return _hourly_report([1.0] * 24)  # MELCloud sends 24 wall-clock labels on DST days too

    monkeypatch.setattr(mel, "energy_report", report)
    autumn = await mel.energy_hours(date(2026, 10, 25), TZ)  # clocks back at 04:00
    assert len(autumn) == 24 and autumn[-1][0] == datetime(2026, 10, 25, 23, 0, tzinfo=TZ)
    assert autumn[5][0].utcoffset() == timedelta(hours=2)

    spring = await mel.energy_hours(date(2026, 3, 29), TZ)  # 03:00–04:00 does not exist
    assert len(spring) == 23 and sum(k for _, k in spring) == 24.0


async def test_energy_days_maps_labels_to_local_days(monkeypatch):
    mel = MelcloudAdapter("e", "p")

    async def report(start, end):
        return {"LabelType": 1, "Labels": [30, 31, 1], "Heating": [4.025, 4.6, 2.3], "Fan": [0.1, 0, 0]}

    monkeypatch.setattr(mel, "energy_report", report)
    days = await mel.energy_days(date(2026, 10, 30), date(2026, 11, 1), TZ)
    assert days == [
        (datetime(2026, 10, 30, tzinfo=TZ), 4.125),
        (datetime(2026, 10, 31, tzinfo=TZ), 4.6),
        (datetime(2026, 11, 1, tzinfo=TZ), 2.3),
    ]


async def test_energy_hours_rejects_non_hourly_report(monkeypatch):
    mel = MelcloudAdapter("e", "p")

    async def report(start, end):
        return {"LabelType": 1, "Labels": [1, 2], "Heating": [4.0, 5.0]}

    monkeypatch.setattr(mel, "energy_report", report)
    try:
        await mel.energy_hours(date(2026, 11, 2), TZ)
    except Exception as e:
        assert "LabelType 1" in str(e)
    else:
        raise AssertionError("expected DeviceError")


def test_old_database_gets_mode_column(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE readings (ts INTEGER NOT NULL, source TEXT NOT NULL, temp REAL, setpoint REAL, power_on INTEGER,"
        " is_heating INTEGER, power_w REAL, energy_kwh REAL, humidity REAL)"
    )
    conn.execute("INSERT INTO readings VALUES (1, 'device:x', 20.0, 21.0, 1, 0, NULL, NULL, NULL)")
    conn.commit()
    conn.close()

    db = Database(path)
    db.add_readings([Reading(2, "device:x", temp=20.5, mode="heat")])
    rows = db.readings("device:x", 0)
    assert [r["mode"] for r in rows] == [None, "heat"]
    db.close()


class FakeMelcloud:
    def __init__(self):
        self.hourly: list[date] = []
        self.daily: list[tuple[date, date]] = []

    async def read(self):
        raise NotImplementedError

    async def energy_days(self, start, end, tz):
        self.daily.append((start, end))
        n = (end - start).days + 1
        return [(datetime.combine(start + timedelta(days=i), datetime.min.time(), tz), 4.025) for i in range(n)]

    async def energy_hours(self, day, tz):
        self.hourly.append(day)
        if day == datetime.now(TZ).date() - timedelta(days=5):
            return []  # MELCloud has nothing for that day
        midnight = datetime.combine(day, datetime.min.time(), tz)
        return [(midnight + timedelta(hours=h), 0.575) for h in range(24)]


def _day(db, d):
    start = datetime.combine(d, datetime.min.time(), TZ)
    return db.energy("device:living_mitsubishi", int(start.timestamp()), int((start + timedelta(days=1)).timestamp()))


async def test_collector_stores_daily_totals_and_hourly_profile(settings, db):
    c = Collector(settings, {}, db, Notifier(settings.notify, None, db, TZ))
    fake = FakeMelcloud()
    c.devices = {"living_mitsubishi": fake}
    today = datetime.now(TZ).date()

    await c.update_energy()
    assert fake.daily == [(today - timedelta(days=ENERGY_DAYS - 1), today)]
    assert len(fake.hourly) == ENERGY_HOURLY_DAYS  # today … 12 days back
    rows = _day(db, today - timedelta(days=3))
    assert sorted({r["minutes"] for r in rows}) == [60, 1440]
    assert [r["kwh"] for r in rows if r["minutes"] == 1440] == [4.025]
    old = _day(db, today - timedelta(days=20))
    assert [r["minutes"] for r in old] == [1440]  # only the daily total that far back

    fake.hourly.clear()
    await c.update_energy()
    assert fake.hourly == [today, today - timedelta(days=1)]  # only the days MELCloud may still correct
    assert len(fake.daily) == 2


async def test_collector_energy_failure_marks_health(settings, db):
    c = Collector(settings, {}, db, Notifier(settings.notify, None, db, TZ))

    class Down(FakeMelcloud):
        async def energy_days(self, start, end, tz):
            raise RuntimeError("503 Service Unavailable")

    c.devices = {"living_mitsubishi": Down()}
    await c.update_energy()
    assert c.health["energy:living_mitsubishi"].failing


async def test_one_day_without_hourly_data_does_not_stop_the_rest(settings, db):
    c = Collector(settings, {}, db, Notifier(settings.notify, None, db, TZ))
    today = datetime.now(TZ).date()
    gone = today - timedelta(days=8)

    class Patchy(FakeMelcloud):
        async def energy_hours(self, day, tz):
            if day == gone:
                self.hourly.append(day)
                raise RuntimeError("expected hourly data, got LabelType 1")
            return await super().energy_hours(day, tz)

    fake = Patchy()
    c.devices = {"living_mitsubishi": fake}
    await c.update_energy()
    assert not c.health["energy:living_mitsubishi"].failing  # daily data is fine: no "offline" alert
    assert any(r["minutes"] == 60 for r in _day(db, today - timedelta(days=10)))  # days after it still fetched
    fake.hourly.clear()
    await c.update_energy()
    assert gone not in fake.hourly  # not asked again


async def test_partial_hourly_day_is_fetched_again(settings, db):
    c = Collector(settings, {}, db, Notifier(settings.notify, None, db, TZ))
    today = datetime.now(TZ).date()
    d = today - timedelta(days=4)
    midnight = datetime.combine(d, datetime.min.time(), TZ)
    db.upsert_energy("device:living_mitsubishi", [(int((midnight + timedelta(hours=h)).timestamp()), 60, 0.5) for h in range(15)])
    assert d in c._hourly_days_to_fetch("device:living_mitsubishi", today)  # only 00–14 known (fetched as "today")
