import asyncio
import sqlite3
import time
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from maska_heat.adapters.base import DeviceError
from maska_heat.adapters.tuya_thermostat import TuyaThermostatAdapter, parse_status
from maska_heat.collector import Collector
from maska_heat.control import ZoneControl
from maska_heat.dashboard import history, zone_views
from maska_heat.db import Database, Reading
from maska_heat.notify import Notifier
from maska_heat.settings import Settings
from maska_heat.web.app import create_app

TZ = ZoneInfo("Europe/Tallinn")

# Status of the bathroom 2 HL-05 as read on 2026-11-09 (protocol 3.5)
DPS = {
    "1": True, "3": 270, "4": "Manual", "6": False, "12": 0, "24": 270, "28": "heat", "101": False, "102": True,
    "103": 280, "104": 1, "105": 15, "106": True, "107": True, "108": True, "109": -10, "110": 10, "111": 2,
    "112": 30, "113": 5, "114": 35, "115": 5, "116": "ext", "117": "keep", "118": "2days",
}
SECRETS = {"tuya": {"bathroom2_floor": "0123456789abcdef"}}


class FakeTuya:
    def __init__(self, result):
        self.result = result
        self.closed = False

    def status(self):
        return self.result

    def close(self):
        self.closed = True


def tuya(result) -> TuyaThermostatAdapter:
    return TuyaThermostatAdapter("id", "tuya.test", "key", device=FakeTuya(result))


def with_bathroom(settings: Settings) -> Settings:
    raw = settings.model_dump(mode="json")
    raw["devices"]["bathroom2_floor"] = {"type": "tuya_thermostat", "host": "tuya.test", "device_id": "bf69", "power_w": 420}
    raw["zones"]["bathroom2"] = {"name": "Bathroom 2", "device": "bathroom2_floor"}
    return Settings.model_validate(raw)


def test_parse_status_scales_tenths():
    s = parse_status(DPS)
    assert (s.room_temp, s.setpoint, s.floor_temp) == (27.0, 27.0, 28.0)
    assert (s.power_on, s.is_heating, s.mode) == (True, True, "Manual")


async def test_read_and_errors():
    s = await tuya({"dps": DPS}).read()
    assert s.floor_temp == 28.0 and s.raw["116"] == "ext"
    faulty = await tuya({"dps": DPS | {"12": 2}}).read()
    assert faulty.mode == "Manual fault 2"
    with pytest.raises(DeviceError, match="Network Error"):
        await tuya({"Error": "Network Error: Unable to Connect", "Err": "901"}).read()
    with pytest.raises(DeviceError, match="no answer"):
        await tuya(None).read()


async def test_never_writes():
    t = tuya({"dps": DPS})
    assert not t.capabilities.writable
    with pytest.raises(DeviceError, match="logged only"):
        await t.set_setpoint(22)
    await t.aclose()
    assert t._device.closed


def test_collector_needs_key_and_id(settings, db):
    s = with_bathroom(settings)
    c = Collector(s, {}, db, Notifier(s.notify, None, db, TZ))
    assert "bathroom2_floor" not in c.devices
    assert "local key missing" in db.events(1)[0]["message"]


async def test_collector_logs_floor_temp(settings, db):
    s = with_bathroom(settings)
    c = Collector(s, SECRETS, db, Notifier(s.notify, None, db, TZ))
    assert isinstance(c.devices["bathroom2_floor"], TuyaThermostatAdapter)
    c.devices["bathroom2_floor"] = tuya({"dps": DPS})
    await c.poll_device("bathroom2_floor")
    row = db.latest_reading("device:bathroom2_floor")
    assert (row["temp"], row["floor_temp"], row["setpoint"], row["is_heating"], row["mode"]) == (27.0, 28.0, 27.0, 1, "Manual")
    assert row["power_w"] == 420  # rated power while heating


    view = next(v for v in zone_views(s, db, time.time()) if v.key == "bathroom2")
    assert (view.temp, view.floor_temp, view.is_heating) == (27.0, 28.0, True)
    hist = history(s, db, "bathroom2", 1, time.time())
    assert hist["series"]["device:bathroom2_floor"]["floor_temp"][0][1] == 28.0

    # idle: 0 W (a new reading one second later, so it is the latest)
    c.devices["bathroom2_floor"] = tuya({"dps": DPS | {"102": False}})
    await asyncio.sleep(1.05)
    await c.poll_device("bathroom2_floor")
    assert db.latest_reading("device:bathroom2_floor")["power_w"] == 0


def test_logged_zone_is_never_controlled(settings, db):
    s = with_bathroom(settings)
    db.add_readings([Reading(int(time.time()), "device:bathroom2_floor", temp=27.0, setpoint=27.0, power_on=True, floor_temp=28.0)])
    app = create_app(s, SECRETS, start_jobs=False, db=db)
    with TestClient(app, follow_redirects=False) as client:
        written = []
        app.state.collector.devices["bathroom2_floor"].set_setpoint = lambda t: written.append(t)
        assert app.state.executor.decide("bathroom2").mode == "logging"  # the default hands-off too
        assert 'hx-post="/control/bathroom2/mode"' not in client.get("/control").text
        app.state.store.set_zone("bathroom2", ZoneControl(mode="manual", manual_setpoint=20))  # even if asked to
        decision = app.state.executor.decide("bathroom2")
        assert decision.mode == "logging" and decision.setpoint is None

        page = client.get("/control").text
        assert "Logging only" in page
        assert 'hx-post="/control/bathroom2/mode"' not in page
        assert "Floor" in client.get("/").text

        asyncio.run(app.state.executor.apply_zone("bathroom2"))
        assert written == []


def test_old_database_gets_floor_column(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE readings (ts INTEGER NOT NULL, source TEXT NOT NULL, temp REAL, setpoint REAL, power_on INTEGER,"
        " is_heating INTEGER, power_w REAL, energy_kwh REAL, humidity REAL, mode TEXT)"
    )
    conn.execute("INSERT INTO readings VALUES (1, 'device:x', 20.0, 21.0, 1, 0, NULL, NULL, NULL, NULL)")
    conn.commit()
    conn.close()

    db = Database(path)
    db.add_readings([Reading(2, "device:x", temp=27.0, floor_temp=28.0)])
    assert [r["floor_temp"] for r in db.readings("device:x", 0)] == [None, 28.0]
    db.close()
