from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from maska_heat import spike as spikes
from maska_heat.control import ControlStore, WorkFromHome, ZoneControl
from maska_heat.db import Reading
from maska_heat.executor import Executor
from maska_heat.settings import Settings
from maska_heat.web.app import create_app

from test_spike import D0, SPOT, TZ, FakeHeater, at, spike_settings

# Monday 9 Nov 2026: both kids at school 07:00–13:00 ("Out", 19 °C); price spike 16:00–19:00
UNTIL = D0 + timedelta(hours=17)


def wfh_settings(settings: Settings) -> Settings:
    raw = spike_settings(settings).model_dump(mode="json")
    raw["sensors"]["hall"] = {"host": "hall.test", "enabled": False}
    raw["zones"]["hall"] = {"name": "Upstairs hall", "temp_sources": ["sensor:hall"]}
    raw["dirigera"] = {"host": "hub.test"}
    raw["doors"] = {"madli": {"device": "Madli uks", "zone": "madli"}, "matis": {"device": "Matise uks", "zone": "matis"}}
    raw["work_from_home"] = {"hall_zone": "hall", "heat_zones": ["madli", "matis"]}
    return Settings.model_validate(raw)


def feed(db, now: float, hall: float | None, madli: float = 20.0, matis: float = 20.0):
    """Fresh readings: the hall sensor (unless silent) and both heaters."""
    rows = [
        Reading(int(now) - 60, "device:madli_adax", temp=madli, setpoint=19, power_on=True),
        Reading(int(now) - 60, "device:matis_mill", temp=matis, setpoint=19, power_on=True),
    ]
    if hall is not None:
        rows += [Reading(int(now) - 60 * m, "sensor:hall", temp=hall) for m in range(1, 6)]
    db.add_readings(rows)


@pytest.fixture
def rig(settings, db):
    s = wfh_settings(settings)
    db.upsert_prices([(int(at(h)), 60, float(SPOT[h % 24])) for h in range(48)])
    store = ControlStore(db, ["madli", "matis"])
    for z in ("madli", "matis"):
        store.set_zone(z, ZoneControl(mode="smart"))
        db.add_door_state(f"door:{z}", int(at(8)), True)
    store.set_work_from_home(WorkFromHome(until=UNTIL))
    ex = Executor(s, db, store, {"madli_adax": FakeHeater(), "matis_mill": FakeHeater()})
    return ex, store, db


def test_off_follows_the_school_day_schedule(rig):
    ex, store, db = rig
    store.set_work_from_home(WorkFromHome())
    feed(db, at(10), hall=19.5)
    d = ex.decide("madli", now=at(10))
    assert d.setpoint == store.comfort().out.preferred and d.label == "Out" and d.wfh is None


def test_cold_hall_heats_both_bedrooms_to_the_maximum(rig):
    ex, store, db = rig
    feed(db, at(10), hall=19.5)
    for z in ("madli", "matis"):
        d = ex.decide(z, now=at(10))
        assert d.setpoint == 24.0 and not d.learn
        assert d.label == "Working from home — warming the hall (19.5 → 21.5 °C)"


def test_hall_thermostat_has_hysteresis(rig):
    ex, store, db = rig
    feed(db, at(10), hall=21.6)  # target reached: bedrooms held at the hall target, above their Out 19
    d = ex.decide("madli", now=at(10))
    assert d.setpoint == 21.5 and "warm enough (21.6 °C)" in d.label
    feed(db, at(10.5), hall=21.3)  # 0.2 below: still within the hysteresis
    assert ex.decide("madli", now=at(10.5)).setpoint == 21.5
    feed(db, at(11), hall=21.1)  # 0.4 below: heat again …
    assert ex.decide("madli", now=at(11)).setpoint == 24.0
    feed(db, at(11.5), hall=21.4)  # … and keep heating until the target is reached
    assert ex.decide("madli", now=at(11.5)).setpoint == 24.0


def test_configured_target_and_maximum_are_used(rig):
    ex, store, db = rig
    store.set_work_from_home(WorkFromHome(target=20.5, max_bedroom=23.0, until=UNTIL))
    feed(db, at(10), hall=19.5)
    assert ex.decide("matis", now=at(10)).setpoint == 23.0
    feed(db, at(10.5), hall=20.6)
    assert ex.decide("matis", now=at(10.5)).setpoint == 20.5


def test_closed_door_keeps_that_bedroom_on_its_schedule(rig):
    ex, store, db = rig
    db.add_door_state("door:madli", int(at(9)), False)
    feed(db, at(10), hall=19.5)
    d = ex.decide("madli", now=at(10))
    assert d.setpoint == store.comfort().out.preferred and "door closed" in d.label  # not heated for nothing
    assert ex.decide("matis", now=at(10)).setpoint == 24.0  # the other heater still works for the hall
    db.add_door_state("door:madli", int(at(10.2)), True)  # door opened again
    feed(db, at(10.5), hall=19.5)
    assert ex.decide("madli", now=at(10.5)).setpoint == 24.0


def test_no_door_data_counts_as_open(settings, db):
    s = wfh_settings(settings)
    store = ControlStore(db, ["madli", "matis"])
    store.set_zone("madli", ZoneControl(mode="smart"))
    store.set_work_from_home(WorkFromHome(until=UNTIL))
    ex = Executor(s, db, store, {"madli_adax": FakeHeater(), "matis_mill": FakeHeater()})
    feed(db, at(10), hall=19.5)
    assert ex.door_open("madli", at(10)) is None and ex.decide("madli", now=at(10)).setpoint == 24.0


def test_stale_hall_sensor_holds_the_hall_target(rig):
    ex, store, db = rig
    feed(db, at(9.5), hall=19.5)
    feed(db, at(10), hall=None)  # the hall sensor has been silent for half an hour
    d = ex.decide("madli", now=at(10))
    assert d.setpoint == 21.5 and "hall sensor silent" in d.label  # neither the maximum nor the Out setback


def test_never_below_the_schedule_target(rig):
    ex, store, db = rig
    feed(db, at(14), hall=21.8)  # kids back from school: Day 22 is above the hall target
    d = ex.decide("madli", now=at(14))
    assert d.setpoint == store.comfort().day.preferred and "Working from home" in d.label


def test_manual_and_handsoff_rooms_are_left_alone(rig):
    ex, store, db = rig
    store.set_zone("madli", ZoneControl(mode="manual", manual_setpoint=20.0))
    store.set_zone("matis", ZoneControl(mode="handsoff"))
    feed(db, at(10), hall=19.5)
    assert ex.decide("madli", now=at(10)).setpoint == 20.0
    assert ex.decide("matis", now=at(10)).setpoint is None


def test_price_spike_still_pauses_and_keep_heating_still_overrides(rig):
    ex, store, db = rig
    store.set_work_from_home(WorkFromHome(until=D0 + timedelta(hours=18)))
    feed(db, at(16.5), hall=19.5)
    d = ex.decide("madli", now=at(16.5))
    assert d.setpoint == 5 and d.heater_units and "price spike 16:00–19:00" in d.label

    feed(db, at(16.6), hall=19.5, madli=17.0)  # the bedroom itself below the 18 °C safety limit: comfort minimum only
    d = ex.decide("madli", now=at(16.6))
    assert d.safety and d.setpoint == store.comfort().band(d.smart.mode).min

    spikes.set_override(db, spikes.active(ex.settings, db, at(16.5)).start, True)  # "Keep heating"
    feed(db, at(16.7), hall=19.5)
    assert ex.decide("madli", now=at(16.7)).setpoint == 24.0


def test_ends_by_itself_and_logs_it(rig):
    ex, store, db = rig
    feed(db, at(17.1), hall=19.5)
    assert ex.decide("madli", now=at(17.1)).wfh is None  # past the end time: the schedule again, at once
    assert not ex.end_work_from_home(at(16.9))
    assert ex.end_work_from_home(at(17.1)) and store.work_from_home().until is None
    assert "Working from home ended at 17:00" in db.events(1)[0]["message"]
    assert not ex.end_work_from_home(at(17.2))  # logged once


def test_settings_need_a_hall_sensor_and_heaters(settings):
    raw = wfh_settings(settings).model_dump(mode="json")
    raw["work_from_home"]["hall_zone"] = "madli"  # a heater's own sensor is not a room sensor
    with pytest.raises(ValueError, match="hall_zone"):
        Settings.model_validate(raw)
    raw["work_from_home"] = {"hall_zone": "hall", "heat_zones": ["hall"]}
    with pytest.raises(ValueError, match="heater"):
        Settings.model_validate(raw)


def test_target_must_not_exceed_the_bedroom_maximum():
    with pytest.raises(ValueError, match="bedroom maximum"):
        WorkFromHome(target=23.0, max_bedroom=22.0)


def test_button_settings_and_events_in_the_web_app(settings, db):
    import time

    s = wfh_settings(settings)
    now = time.time()
    feed(db, now, hall=20.4)
    db.add_door_state("door:madli", int(now) - 600, True)
    db.add_door_state("door:matis", int(now) - 600, False)
    app = create_app(s, {"adax": {"account_id": "1", "api_secret": "x"}}, start_jobs=False, db=db)
    with TestClient(app) as client:
        app.state.collector.devices.update({"madli_adax": FakeHeater(), "matis_mill": FakeHeater()})
        for z in ("madli", "matis"):
            app.state.store.set_zone(z, ZoneControl(mode="smart"))
        page = client.get("/").text
        assert "20.4&nbsp;°C now" in page
        # the one-tap button offers the default end; after the night start there is none to offer
        assert "Start working from home · until" in page or "It is night" in page

        r = client.post("/settings/work-from-home", data={"target": "21", "max_bedroom": "23.5", "end_time": "16:30"}, follow_redirects=False)
        assert r.headers["location"] == "/settings?saved=wfh"
        assert app.state.store.work_from_home().target == 21.0
        r = client.post("/settings/work-from-home", data={"target": "25", "max_bedroom": "23.5", "end_time": "16:30"}, follow_redirects=False)
        assert "error=" in r.headers["location"] and app.state.store.work_from_home().target == 21.0
        assert 'value="21.0"' in client.get("/settings").text

        today = datetime.now(TZ)
        end = min(today + timedelta(minutes=30), today.replace(hour=23, minute=59)).replace(second=0, microsecond=0)
        if end <= today:
            pytest.skip("the last minute of the day: no end time later today")
        r = client.post("/work-from-home", data={"until": f"{end:%H:%M}", "next": "/"}, follow_redirects=False)
        assert r.headers["location"] == "/" and app.state.store.work_from_home().until == end
        assert f"Working from home until {end:%H:%M} — hall target 21.0 °C" in db.events(1)[0]["message"]
        for page in (client.get("/").text, client.get("/control").text):
            assert f"on until {end:%H:%M}" in page and "End working from home" in page
            assert "door open" in page and "warming the hall (20.4 → 21.0 °C)" in page
            assert "door closed" in page and "follows its normal schedule" in page
        assert "Target</dt><dd>21.0" in client.get("/").text  # the hall's card shows the mode's target

        r = client.post("/work-from-home", data={"until": "00:00"}, follow_redirects=False)  # not later today
        assert "error=" in r.headers["location"]

        client.post("/work-from-home/stop", data={"next": "/control"})
        assert app.state.store.work_from_home().until is None
        assert "Working from home ended" in db.events(1)[0]["message"]
        assert "Start working from home" in client.get("/control").text
