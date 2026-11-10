import time

import pytest
from fastapi.testclient import TestClient

from maska_heat.adapters.base import Capabilities
from maska_heat.control import ControlStore, ZoneControl
from maska_heat.db import Reading
from maska_heat.executor import Executor, heater_offset
from maska_heat.settings import Settings
from maska_heat.web.app import create_app

NOW = 1_800_000_000


def room_settings(settings: Settings) -> Settings:
    raw = settings.model_dump(mode="json")
    raw["sensors"]["madli"] = {"host": "madli.test", "enabled": False}
    raw["zones"]["madli"] |= {"temp_sources": ["sensor:madli", "device:madli_adax"], "control_by": "room"}
    return Settings.model_validate(raw)


class Heater:
    def __init__(self, step=1.0):
        self.capabilities = Capabilities("resistive", 5, 35, step, False, False)


def history(db, room: float, heater: float, setpoint: float = 20.0, minutes: int = 25, end: int = NOW):
    """Room sensor every minute, Adax every 5 min, over the last `minutes`."""
    db.add_readings([Reading(end - 60 * m, "sensor:madli", temp=room) for m in range(minutes)])
    db.add_readings([Reading(end - 60 * m, "device:madli_adax", temp=heater, setpoint=setpoint, power_on=True) for m in range(0, minutes, 5)])


@pytest.fixture
def rig(settings, db):
    s = room_settings(settings)
    store = ControlStore(db, [])
    return Executor(s, db, store, {"madli_adax": Heater(), "matis_mill": Heater(0.5)}), store, db


def test_offset_median_and_staleness(db):
    history(db, room=22.0, heater=18.9)
    assert heater_offset(db, "madli_adax", "sensor:madli", NOW) == pytest.approx(-3.1)
    assert heater_offset(db, "madli_adax", "sensor:madli", NOW + 1200) is None  # room sensor silent for 20 min
    history(db, room=22.0, heater=12.0, end=NOW + 3600)
    assert heater_offset(db, "madli_adax", "sensor:madli", NOW + 3600) is None  # 10 °C gap: implausible


def test_manual_room_target_becomes_heater_setpoint(rig):
    ex, store, db = rig
    history(db, room=22.0, heater=18.9)
    store.set_zone("madli", ZoneControl(mode="manual", manual_setpoint=20.0))
    d = ex.decide("madli", now=NOW)
    assert (d.room_target, d.offset, d.setpoint) == (20.0, pytest.approx(-3.1), 17.0)


def test_door_open_smaller_gap_raises_heater_setpoint(rig):
    ex, store, db = rig
    history(db, room=20.0, heater=19.0, setpoint=17.0)  # door open: room cooler, gap only 1 °C
    store.set_zone("madli", ZoneControl(mode="manual", manual_setpoint=20.0))
    assert ex.decide("madli", now=NOW).setpoint == 19.0


def test_deadband_keeps_current_heater_setpoint(rig):
    ex, store, db = rig
    history(db, room=20.0, heater=17.4, setpoint=17.0)  # wants 17.4: within ¾ of the Adax's 1 °C step
    store.set_zone("madli", ZoneControl(mode="manual", manual_setpoint=20.0))
    assert ex.decide("madli", now=NOW).setpoint == 17.0


def test_never_sends_a_room_target_unshifted(rig):
    """No room sensor and no known gap: the heater is left alone (sending 20 would heat the room to ~23)."""
    ex, store, db = rig
    db.add_readings([Reading(NOW - 60, "device:madli_adax", temp=19.0, setpoint=17.0, power_on=True)])
    store.set_zone("madli", ZoneControl(mode="manual", manual_setpoint=20.0))
    d = ex.decide("madli", now=NOW)
    assert d.setpoint is None and d.room_target == 20.0 and "gap is unknown" in d.blocked


def test_room_sensor_silent_uses_last_known_gap(rig):
    ex, store, db = rig
    history(db, room=22.0, heater=18.9)
    store.set_zone("madli", ZoneControl(mode="manual", manual_setpoint=20.0))
    assert ex.decide("madli", now=NOW).setpoint == 17.0  # gap −3.1 measured and remembered
    later = NOW + 2 * 3600
    db.add_readings([Reading(later - 60, "device:madli_adax", temp=17.0, setpoint=17.0, power_on=True)])
    d = ex.decide("madli", now=later)  # the room sensor has been silent for two hours
    assert d.setpoint == 17.0 and d.offset == pytest.approx(-3.1) and not d.learn and "last known gap" in d.label
    assert ex.room_estimate("madli", later) == pytest.approx(20.1)  # heater 17.0 corrected by the gap
    db.add_readings([Reading(later + 86400, "device:madli_adax", temp=17.0, setpoint=17.0, power_on=True)])
    assert ex.decide("madli", now=later + 86400 + 60).setpoint is None  # a day later the gap is too old to trust


def test_room_equivalent_of_heater_setpoint(rig):
    ex, store, db = rig
    history(db, room=22.0, heater=18.9)
    assert ex.room_equivalent("madli", 17.0, now=NOW) == 20.0
    assert ex.room_equivalent("matis", 19.5, now=NOW) == 19.5  # heater-controlled zone: as it is


def test_smart_target_is_translated(rig):
    ex, store, db = rig
    history(db, room=21.0, heater=18.0)
    store.set_zone("madli", ZoneControl(mode="smart"))
    d = ex.decide("madli", now=NOW)
    assert d.room_target is not None and d.setpoint == ex.devices["madli_adax"].capabilities.clamp(d.room_target - 3.0)


def test_other_zones_unchanged(rig):
    ex, store, db = rig
    db.add_readings([Reading(NOW - 60, "device:matis_mill", temp=19.0, setpoint=19.0, power_on=True)])
    store.set_zone("matis", ZoneControl(mode="manual", manual_setpoint=20.5))
    d = ex.decide("matis", now=NOW)
    assert d.setpoint == 20.5 and d.room_target is None


def test_settings_need_a_room_sensor(settings):
    raw = settings.model_dump(mode="json")
    raw["zones"]["matis"]["control_by"] = "room"
    with pytest.raises(ValueError, match="control_by room"):
        Settings.model_validate(raw)


def test_cards_show_room_target(settings, db):
    s = room_settings(settings)
    now = int(time.time())
    history(db, room=22.0, heater=18.9, setpoint=17.0, end=now)
    app = create_app(s, {"adax": {"account_id": "1", "api_secret": "x"}}, start_jobs=False, db=db)
    with TestClient(app) as client:
        client.post("/control/madli/mode", data={"mode": "manual"})
        app.state.store.set_zone("madli", ZoneControl(mode="manual", manual_setpoint=20.0))
        page = client.get("/control").text
        assert "Heater set to 17.0 °C" in page and "3.1 °C lower than the room" in page
        assert "(heater 17)" in client.get("/").text


def test_manual_starts_from_the_room_equivalent(settings, db):
    """Madli's heater at 17 with the room at 20: Manual must start at 20 (room), not 17 (which would become ~14)."""
    s = room_settings(settings)
    now = int(time.time())
    history(db, room=20.0, heater=16.9, setpoint=17.0, end=now)
    app = create_app(s, {"adax": {"account_id": "1", "api_secret": "x"}}, start_jobs=False, db=db)
    with TestClient(app) as client:
        client.post("/control/madli/mode", data={"mode": "manual"})
        assert app.state.store.zone("madli").manual_setpoint == 20.0


def test_feedback_raises_heater_when_room_stays_low(rig):
    ex, store, db = rig
    store.set_zone("madli", ZoneControl(mode="manual", manual_setpoint=22.0))
    history(db, room=21.6, heater=19.0, setpoint=19.0, minutes=90, end=NOW + 3600)  # gap −2.6 → heater 19, room 0.4 low
    assert ex.decide("madli", now=NOW).setpoint == 19.0
    for k in range(13):  # an hour of 5-min cycles with the room 0.4 °C low
        t = NOW + 300 * k
        ex.update_trim("madli", ex.decide("madli", now=t), t)
    assert ex.trim("madli") == pytest.approx(1.5 * 0.4 * 1.0, abs=0.01)
    d = ex.decide("madli", now=NOW + 3600)
    assert d.trim > 0.5 and d.setpoint == 20.0  # one Adax step up


def test_feedback_lowers_heater_when_room_stays_high(rig):
    ex, store, db = rig
    store.set_zone("madli", ZoneControl(mode="manual", manual_setpoint=20.0))
    history(db, room=20.6, heater=17.4, setpoint=17.0, minutes=90, end=NOW + 3600)  # gap −3.2 → heater 17 and cycling, room 0.6 high
    for k in range(13):
        t = NOW + 300 * k
        ex.update_trim("madli", ex.decide("madli", now=t), t)
    assert ex.trim("madli") == pytest.approx(-0.6 * 1.5, abs=0.01)
    assert ex.decide("madli", now=NOW + 3600).setpoint == 16.0


def test_no_learning_while_room_just_cools_down(rig):
    """10.11 night: the room cooled from 22 to 20 °C towards its Night target with the heater idle.

    v1 learned −2.3 °C from that and the next day held the room 2 °C below target."""
    ex, store, db = rig
    store.set_zone("madli", ZoneControl(mode="manual", manual_setpoint=20.0))
    history(db, room=20.6, heater=18.0, setpoint=17.0, minutes=90, end=NOW + 3600)  # heater 1 °C above its setpoint: idle
    for k in range(13):
        t = NOW + 300 * k
        ex.update_trim("madli", ex.decide("madli", now=t), t)
    assert ex.trim("madli") == 0.0


def test_no_learning_while_heater_runs_flat_out(rig):
    ex, store, db = rig
    store.set_zone("madli", ZoneControl(mode="manual", manual_setpoint=22.0))
    history(db, room=21.4, heater=17.5, setpoint=19.0, minutes=90, end=NOW + 3600)  # warming up: heater far below its setpoint
    for k in range(13):
        t = NOW + 300 * k
        ex.update_trim("madli", ex.decide("madli", now=t), t)
    assert ex.trim("madli") == 0.0


def test_feedback_does_not_wind_up(rig):
    ex, store, db = rig
    history(db, room=19.0, heater=16.0, setpoint=17.0)
    store.set_zone("madli", ZoneControl(mode="manual", manual_setpoint=22.0))  # 3 °C below: still warming up
    for k in range(13):
        ex.update_trim("madli", ex.decide("madli", now=NOW), NOW + 300 * k)
    assert ex.trim("madli") == 0.0
    db.add_readings([Reading(NOW + 3600 * 3, "device:madli_adax", temp=19.0, setpoint=17.0)])  # room sensor silent
    ex.update_trim("madli", ex.decide("madli", now=NOW + 3600 * 3), NOW + 3600 * 3)
    assert ex.trim("madli") == 0.0
