import json
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from maska_heat import spike as spikes
from maska_heat.adapters.base import Capabilities
from maska_heat.control import ControlStore, ZoneControl
from maska_heat.db import Reading
from maska_heat.executor import Executor
from maska_heat.hotwater import planned
from maska_heat.notify import Notifier
from maska_heat.settings import Settings
from maska_heat.web.app import create_app

TZ = ZoneInfo("Europe/Tallinn")
DAY = date(2026, 11, 9)
D0 = datetime(2026, 11, 9, tzinfo=TZ)
# 9 Nov 2026 spot prices (€/MWh) shaped like the real day: spike at 16:00 and 18:00
SPOT = [150] * 24
SPOT[16], SPOT[17], SPOT[18], SPOT[19] = 400, 300, 350, 290


def at(hour: float) -> float:
    return (D0 + timedelta(hours=hour)).timestamp()


def spike_settings(settings: Settings, **over) -> Settings:
    raw = settings.model_dump(mode="json")
    raw["devices"]["water_heater"] = {"type": "cozytouch", "power_w": 2250}
    raw["devices"]["bathroom2_floor"] = {"type": "tuya_thermostat", "host": "t", "device_id": "x", "power_w": 420}
    raw["zones"]["hot_water"] = {"name": "Hot water", "device": "water_heater"}
    raw["zones"]["bathroom2"] = {"name": "Bathroom 2", "device": "bathroom2_floor"}
    raw["hot_water"] = {"zone": "hot_water"}
    raw["spike"] = {"zones": ["madli", "matis", "bathroom2", "hot_water"], **over}
    raw["notify"]["app_url"] = "http://maska-heat:8000"
    return Settings.model_validate(raw)


@pytest.fixture
def s(settings, db):
    db.upsert_prices([(int(at(h)), 60, float(SPOT[h % 24])) for h in range(48)])
    return spike_settings(settings)


def test_day_spikes_three_most_expensive_hours_gap_filled(s, db):
    found = spikes.day_spikes(s, db, DAY)
    assert [(datetime.fromtimestamp(x.start, TZ).hour, datetime.fromtimestamp(x.end, TZ).hour) for x in found] == [(16, 19)]
    assert found[0].peak > 0.4


def test_day_spikes_thresholds(settings, db):
    db.upsert_prices([(int(at(h)), 60, float(SPOT[h % 24])) for h in range(48)])
    loose = spike_settings(settings, max_hours=5, ratio=1.2)
    assert [(datetime.fromtimestamp(x.start, TZ).hour, datetime.fromtimestamp(x.end, TZ).hour) for x in spikes.day_spikes(loose, db, DAY)] == [(16, 20)]
    strict = spike_settings(settings, min_price_c=80)
    assert spikes.day_spikes(strict, db, DAY) == []
    assert spikes.day_spikes(strict, db, DAY + timedelta(days=5)) is None  # no prices yet


def test_override(s, db):
    sp = spikes.active(s, db, at(16.5))
    assert spikes.pausing(s, db, "madli", at(16.5)) == sp
    assert spikes.pausing(s, db, "living", at(16.5)) is None  # heat pump not listed
    spikes.set_override(db, sp.start, True)
    assert spikes.pausing(s, db, "madli", at(16.5)) is None
    spikes.set_override(db, sp.start, False)
    assert spikes.pausing(s, db, "madli", at(16.5)) == sp


class FakeHeater:
    def __init__(self, kind="resistive"):
        self.capabilities = Capabilities(kind, 5, 35, 0.5, False, False, writable=kind != "floor")
        self.sent = []

    async def set_setpoint(self, t):
        self.sent.append(t)

    async def take_control(self):
        pass


def executor(s, db):
    store = ControlStore(db, [])
    for z in ("madli", "matis"):
        store.set_zone(z, ZoneControl(mode="smart"))
    return Executor(s, db, store, {"madli_adax": FakeHeater(), "matis_mill": FakeHeater()}), store


def room(db, zone_sensor: str, temp: float, ts: float):
    db.add_readings([Reading(int(ts), f"device:{zone_sensor}", temp=temp, setpoint=20, power_on=True)])


def test_executor_pauses_preheats_and_keeps_safety(s, db):
    ex, store = executor(s, db)
    room(db, "madli_adax", 20.5, at(16.5))
    d = ex.decide("madli", now=at(16.5))
    assert d.setpoint == 5 and "price spike 16:00–19:00" in d.label

    room(db, "madli_adax", 17.0, at(17.0))  # below the 18 °C safety limit: back to the comfort minimum
    d = ex.decide("madli", now=at(17.0))
    band_min = store.comfort().band(d.smart.mode).min
    assert d.setpoint == band_min and "keeps the minimum" in d.label

    room(db, "madli_adax", 20.0, at(15.5))
    d = ex.decide("madli", now=at(15.5))  # 15:00 is cheap: pre-heat to the comfort max
    assert d.setpoint == store.comfort().band(d.smart.mode).max and "pre-heating" in d.label

    spikes.set_override(db, spikes.active(s, db, at(16.5)).start, True)
    room(db, "madli_adax", 20.5, at(16.6))
    assert ex.decide("madli", now=at(16.6)).setpoint > 5


def test_manual_zone_is_not_paused(s, db):
    ex, store = executor(s, db)
    store.set_zone("matis", ZoneControl(mode="manual", manual_setpoint=21))
    room(db, "matis_mill", 20.5, at(16.5))
    assert ex.decide("matis", now=at(16.5)).setpoint == 21


async def test_floor_paused_and_restored_and_notified(s, db):
    floor = FakeHeater("floor")
    sent = []

    async def send(*a, **k):
        sent.append((a, k))
        return True

    notifier = Notifier(s.notify.model_copy(update={"enabled": True}), "test-topic", db, TZ)
    notifier.send = send
    changes = []
    ctl = spikes.SpikeController(s, db, {"bathroom2_floor": floor}, notifier, lambda: changes.append(1))
    db.add_readings([Reading(int(at(15)), "device:bathroom2_floor", setpoint=27.0, floor_temp=26)])

    await ctl.run(at(15.2))  # 48 min before: warn once, with buttons
    await ctl.run(at(15.3))
    assert len(sent) == 1
    (_, title, msg), kw = sent[0][0][:3], sent[0][1]
    assert "16:00–19:00" in msg and "Bathroom 2" in msg
    assert kw["actions"][0]["url"].endswith(f"/spike/{int(at(16))}/override")

    await ctl.run(at(16.01))
    assert floor.sent == [5] and changes  # paused; the other controllers were nudged
    db.add_readings([Reading(int(at(16.1)), "device:bathroom2_floor", setpoint=5.0)])
    await ctl.run(at(17))
    assert floor.sent == [5]
    await ctl.run(at(19.01))
    assert floor.sent == [5, 27.0]
    assert json.loads(db.get_kv(spikes.FLOOR_KEY)) == {}


async def test_floor_not_restored_over_a_manual_change(s, db):
    floor = FakeHeater("floor")
    ctl = spikes.SpikeController(s, db, {"bathroom2_floor": floor}, Notifier(s.notify, None, db, TZ))
    db.add_readings([Reading(int(at(15)), "device:bathroom2_floor", setpoint=27.0)])
    await ctl.run(at(16.01))
    db.add_readings([Reading(int(at(17)), "device:bathroom2_floor", setpoint=24.0)])  # someone set 24 in the app
    await ctl.run(at(19.01))
    assert floor.sent == [5]


def test_hot_water_avoids_spike_hours(s, db):
    windows = planned(s, db, DAY)
    hours = {m // 60 for a, b in windows for m in range(a, b, 60)}
    assert not hours & {16, 17, 18}
    spikes.set_override(db, spikes.active(s, db, at(16.5)).start, True)
    assert len(planned(s, db, DAY)) >= 1


def test_dashboard_notice_and_override(s, db):
    app = create_app(s, {}, start_jobs=False, db=db)
    now = time.time()
    day = datetime.fromtimestamp(now, TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    db.upsert_prices([(int((day + timedelta(hours=h)).timestamp()), 60, float(SPOT[h % 24])) for h in range(48)])
    with TestClient(app) as client:
        assert "nothing pauses" in client.get("/").text or not spikes.next_or_active(s, db, now)  # all Hands-off
        app.state.store.set_zone("madli", ZoneControl(mode="smart"))
        page = client.get("/").text
        upcoming = spikes.next_or_active(s, db, now)
        if upcoming:
            assert "Price spike" in page and "Keep heating" in page
            r = client.post(f"/spike/{upcoming.start}/override")
            assert r.status_code == 200 and spikes.overridden(db, upcoming)
            assert "heating normally" in client.get("/").text
            client.post(f"/spike/{upcoming.start}/resume")
            assert not spikes.overridden(db, upcoming)
        assert client.post("/spike/123/override").status_code == 404


class Phone:
    """A notifier whose sends succeed or fail on demand."""

    def __init__(self, s, db, ok=True):
        self.inner = Notifier(s.notify, "test-topic", db, TZ)
        self.ok, self.sent = ok, []
        self.available = True

    def in_quiet_hours(self, now=None):
        return self.inner.in_quiet_hours(now)

    async def send(self, kind, title, message, **kw):
        self.sent.append(message)
        return self.ok


async def test_warning_waits_for_quiet_hours_to_end(settings, db):
    """A 07:00 spike: the warning is due at 06:00 (quiet hours) and goes out at 06:30 instead of never."""
    spot = [150] * 48
    spot[7] = spot[31] = 500
    db.upsert_prices([(int(at(h)), 60, float(spot[h])) for h in range(48)])
    s = spike_settings(settings)
    phone = Phone(s, db)
    ctl = spikes.SpikeController(s, db, {}, phone)
    await ctl.run(at(6.1))
    assert phone.sent == []
    await ctl.run(at(6.51))
    await ctl.run(at(6.6))
    assert len(phone.sent) == 1 and "07:00–08:00" in phone.sent[0]


async def test_failed_warning_is_retried(s, db):
    phone = Phone(s, db, ok=False)
    ctl = spikes.SpikeController(s, db, {}, phone)
    await ctl.run(at(15.2))
    await ctl.run(at(15.25))  # 3 min later: not yet again
    assert len(phone.sent) == 1
    phone.ok = True
    await ctl.run(at(15.3))
    await ctl.run(at(15.4))
    assert len(phone.sent) == 2


async def test_warning_lists_only_zones_that_pause(s, db):
    store = ControlStore(db, [])
    store.set_zone("madli", ZoneControl(mode="smart"))
    store.set_zone("matis", ZoneControl(mode="manual", manual_setpoint=21))
    floor = FakeHeater("floor")
    phone = Phone(s, db)
    ctl = spikes.SpikeController(s, db, {"bathroom2_floor": floor}, phone, store=store)
    await ctl.run(at(15.2))
    msg = phone.sent[0]
    assert "Madli" in msg and "Bathroom 2" in msg  # Smart room and the logged-only floor
    assert "Matis" not in msg and "Hot water" not in msg  # Manual keeps heating; the water heater is Hands-off


async def test_a_failing_part_does_not_stop_the_rest(s, db):
    changes = []
    ctl = spikes.SpikeController(s, db, {}, Phone(s, db), lambda: changes.append(1))

    async def broken(now):
        raise RuntimeError("ntfy exploded")

    ctl._notify = broken
    await ctl.run(at(16.5))
    assert changes  # the other controllers were still told that the spike started


def test_safety_heating_has_hysteresis(s, db):
    ex, store = executor(s, db)
    room(db, "madli_adax", 17.9, at(16.2))
    assert ex.decide("madli", now=at(16.2)).safety
    ex._safety.add("madli")  # as _apply does
    room(db, "madli_adax", 18.2, at(16.3))
    assert ex.decide("madli", now=at(16.3)).safety  # just above the limit: keeps heating
    room(db, "madli_adax", 18.6, at(16.4))
    assert not ex.decide("madli", now=at(16.4)).safety
