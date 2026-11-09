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


def test_fallback_without_room_sensor(rig):
    ex, store, db = rig
    db.add_readings([Reading(NOW - 60, "device:madli_adax", temp=19.0, setpoint=20.0, power_on=True)])
    store.set_zone("madli", ZoneControl(mode="manual", manual_setpoint=20.0))
    d = ex.decide("madli", now=NOW)
    assert d.setpoint == 20.0 and d.room_target is None and "heater's own sensor" in d.label


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
