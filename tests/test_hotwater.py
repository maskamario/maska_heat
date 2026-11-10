import json
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from maska_heat.adapters.base import DeviceState
from maska_heat.adapters.cozytouch import CAP_BOOST, CAP_RANGE, CAP_SCHEDULE, parse_schedule, schedule_value
from maska_heat.control import ControlStore, ZoneControl
from maska_heat.hotwater import HotWaterController, HotWaterRules, day_prices, in_window, plan_day
from maska_heat.notify import Notifier
from maska_heat.settings import Settings
from maska_heat.web.app import create_app

TZ = ZoneInfo("Europe/Tallinn")
RULES = HotWaterRules()


def hours(prices_by_hour: list[float]) -> dict[int, float]:
    return {h * 60: p for h, p in enumerate(prices_by_hour)}


def total(windows):
    return sum(b - a for a, b in windows) // 60


def test_schedule_value_round_trip():
    v = schedule_value([(1320, 1440), (0, 360)])
    assert v == "[[0,360],[1320,1440],[0,0]]"
    assert parse_schedule(v) == [(0, 360), (1320, 1440)]
    with pytest.raises(ValueError):
        schedule_value([(0, 60), (120, 180), (240, 300), (360, 420)])
    with pytest.raises(ValueError):
        schedule_value([(10, 60)])


def test_plan_takes_cheapest_hours_with_morning_and_evening():
    # cheapest hours in the middle of the day; mornings and evenings expensive
    p = [0.30] * 24
    for h in (10, 11, 12, 13, 14, 15, 16, 17):
        p[h] = 0.05
    w = plan_day(hours(p), RULES)
    assert total(w) == 6 and len(w) <= 3
    assert sum(1 for a, b in w for m in range(a, b, 60) if m + 60 <= 390) >= 3  # 3 h before 06:30
    assert any(a < 19 * 60 and b > 12 * 60 for a, b in w)  # top-up between 12 and 19


def test_plan_merges_to_three_windows():
    p = [0.30 if h % 2 else 0.05 for h in range(24)]  # every other hour cheap: many small windows
    w = plan_day(hours(p), RULES)
    assert len(w) <= 3 and total(w) >= 6


def test_plan_never_merges_across_a_spike():
    """The bug: merging windows counted the (removed) spike hours as free, so a 03:00 spike got heated through."""
    p = [0.30 if h % 2 else 0.05 for h in range(24)]
    spike = {3 * 60}
    w = plan_day(hours(p), RULES, avoid=spike)
    assert len(w) <= 3
    assert not any(a <= 180 < b for a, b in w)
    p = [0.30] * 24
    for h in (0, 1, 2, 4, 5, 13, 20):  # cheap on both sides of a 03:00 and an 18:00 spike
        p[h] = 0.02
    w = plan_day(hours(p), HotWaterRules(hours_per_day=7, morning_hours=5), avoid={180, 18 * 60})
    assert len(w) <= 3 and not any(a <= m < b for a, b in w for m in (180, 18 * 60))


def test_plan_night_when_night_is_cheapest():
    p = [0.05] * 7 + [0.30] * 17
    p[14] = 0.2  # cheapest afternoon hour
    w = plan_day(hours(p), RULES)
    assert total(w) == 6 and (14 * 60, 15 * 60) in w


def test_day_prices_and_dst():
    day = date(2026, 10, 25)  # clocks go back at 04:00
    start = datetime(2026, 10, 25, tzinfo=TZ).timestamp()
    hp = {int(start) + i * 3600: float(i) for i in range(25)}
    p = day_prices(hp, day, TZ)
    assert len(p) == 24 and p[3 * 60] == 3.0  # repeated 03:00 keeps its first price
    assert in_window([(0, 360)], datetime(2026, 10, 25, 5, 59, tzinfo=TZ))
    assert not in_window([(0, 360)], datetime(2026, 10, 25, 6, 0, tzinfo=TZ))


class FakeBoiler:
    def __init__(self):
        self.caps = {CAP_RANGE: "0", CAP_BOOST: "0", **{cap: "[[0,360],[1320,1440],[0,0]]" for cap in CAP_SCHEDULE.values()}}
        self.level = 80.0
        self.writes: list[tuple[int, str]] = []

    async def read(self):
        return DeviceState(60.0, 70.0, True, False, level_pct=self.level, raw={"capabilities": dict(self.caps)})

    async def write(self, cap, value):
        self.writes.append((cap, value))
        self.caps[cap] = value

    async def set_day_schedule(self, weekday, windows):
        await self.write(CAP_SCHEDULE[weekday], schedule_value(windows))

    async def set_operating_range(self, mode):
        await self.write(CAP_RANGE, str(mode))

    async def boost(self):
        await self.write(CAP_BOOST, "1")


def boiler_settings(settings: Settings) -> Settings:
    raw = settings.model_dump(mode="json")
    raw["devices"]["water_heater"] = {"type": "cozytouch", "power_w": 2250}
    raw["zones"]["hot_water"] = {"name": "Hot water", "device": "water_heater"}
    raw["hot_water"] = {"zone": "hot_water"}
    return Settings.model_validate(raw)


NOW = datetime(2026, 11, 10, 9, 0, tzinfo=TZ)  # Tuesday


@pytest.fixture
def rig(settings, db):
    s = boiler_settings(settings)
    day = datetime(2026, 11, 10, tzinfo=TZ).timestamp()
    db.upsert_prices([(int(day) + h * 3600, 60, 300.0 if 7 <= h < 22 else 50.0) for h in range(48)])  # today + tomorrow
    store = ControlStore(db, [])
    boiler = FakeBoiler()
    sent = []
    notifier = Notifier(s.notify, None, db, TZ)
    notifier.send = lambda *a, **k: _record(sent, a)
    hw = HotWaterController(s, db, store, {"water_heater": boiler}, notifier)
    return hw, store, boiler, db, sent


async def _record(sent, args):
    sent.append(args)
    return True


async def test_handsoff_never_writes(rig):
    hw, store, boiler, db, _ = rig
    await hw.run(NOW.timestamp())
    assert boiler.writes == [] and not hw.state()["engaged"]


async def test_smart_writes_today_and_tomorrow_then_nothing(rig):
    hw, store, boiler, db, _ = rig
    store.set_zone("hot_water", ZoneControl(mode="smart"))
    await hw.run(NOW.timestamp())
    written = dict(boiler.writes)
    assert written[CAP_RANGE] == "1"
    tue, wed = parse_schedule(written[CAP_SCHEDULE[1]]), parse_schedule(written[CAP_SCHEDULE[2]])
    assert total(tue) == 6 and total(wed) == 6 and len(tue) <= 3
    assert CAP_SCHEDULE[3] not in written  # Thursday: no prices yet, keeps its schedule
    state = hw.state()
    assert state["engaged"] and state["restore_range"] == "0"
    assert state["restore_schedule"]["1"] == "[[0,360],[1320,1440],[0,0]]"

    boiler.writes.clear()
    await hw.run(NOW.timestamp() + 600)
    assert boiler.writes == []  # already as planned

    store.set_zone("hot_water", ZoneControl(mode="handsoff"))
    await hw.run(NOW.timestamp() + 1200)
    assert boiler.caps[CAP_RANGE] == "0" and boiler.caps[CAP_SCHEDULE[1]] == "[[0,360],[1320,1440],[0,0]]"
    assert not hw.state()["engaged"]


async def test_boost_when_low_outside_windows_once(rig):
    hw, store, boiler, db, sent = rig
    store.set_zone("hot_water", ZoneControl(mode="smart"))
    boiler.level = 25
    await hw.run(NOW.timestamp())  # 09:00, expensive: outside the windows
    assert boiler.caps[CAP_BOOST] == "1" and len(sent) == 1
    boiler.caps[CAP_BOOST] = "0"  # boost finished
    await hw.run(NOW.timestamp() + 1800)
    assert len(sent) == 1  # cooldown
    await hw.run(NOW.timestamp() + 4 * 3600)
    assert len(sent) == 2


async def test_no_boost_inside_window(rig):
    hw, store, boiler, db, sent = rig
    store.set_zone("hot_water", ZoneControl(mode="smart"))
    boiler.level = 10
    await hw.run(datetime(2026, 11, 10, 3, 0, tzinfo=TZ).timestamp())  # cheap night hour: it heats anyway
    assert boiler.caps[CAP_BOOST] == "0" and sent == []


def test_settings_require_a_cozytouch_zone(settings):
    raw = settings.model_dump(mode="json")
    raw["hot_water"] = {"zone": "matis"}
    with pytest.raises(ValueError, match="cozytouch"):
        Settings.model_validate(raw)


def test_control_card_and_modes(settings, db):
    s = boiler_settings(settings)
    now = time.time()
    day = datetime.fromtimestamp(now, TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    db.upsert_prices([(int((day + timedelta(hours=h)).timestamp()), 60, 100.0 + h) for h in range(24)])
    app = create_app(s, {"cozytouch": {"email": "e", "password": "p"}}, start_jobs=False, db=db)
    with TestClient(app) as client:
        page = client.get("/control").text
        assert 'hx-post="/control/hot_water/mode"' in page and "Hands-off" in page
        assert client.post("/control/hot_water/mode", data={"mode": "manual"}).status_code == 400
        r = client.post("/control/hot_water/mode", data={"mode": "smart"})
        assert r.status_code == 200 and "Today" in r.text and "cheapest hours" in r.text
        assert app.state.executor.decide("hot_water").label.startswith("heats ")
        client.post("/control/all", data={"mode": "handsoff"})
        assert app.state.store.zone("hot_water").mode == "smart"  # "all rooms" leaves the water heater alone
        assert "heats " in client.get("/").text


async def test_handback_writes_only_what_differs_and_resumes(rig):
    hw, store, boiler, db, _ = rig
    store.set_zone("hot_water", ZoneControl(mode="smart"))
    await hw.run(NOW.timestamp())  # writes Tue + Wed and the range
    boiler.writes.clear()
    calls = 0
    real = boiler.write

    async def limited(cap, value):  # the rate limit hits after the first write
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("rate limited")
        await real(cap, value)

    boiler.write = limited
    store.set_zone("hot_water", ZoneControl(mode="handsoff"))
    await hw.run(NOW.timestamp() + 600)
    assert hw.state()["engaged"] and list(hw.state()["restore_schedule"]) == ["2", "3", "4", "5", "6"]
    await hw.run(NOW.timestamp() + 1200)  # continues with Wednesday, skips the untouched days
    assert [c for c, _ in boiler.writes] == [CAP_SCHEDULE[1], CAP_SCHEDULE[2], CAP_RANGE]
    assert not hw.state()["engaged"] and boiler.caps[CAP_SCHEDULE[2]] == "[[0,360],[1320,1440],[0,0]]"
