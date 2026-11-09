import sqlite3
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient

from maska_heat.adapters.base import DeviceError
from maska_heat.adapters.cozytouch import API_URL, CozytouchWaterHeaterAdapter, parse_water_heater
from maska_heat.collector import Collector
from maska_heat.db import Database, Reading
from maska_heat.notify import Notifier
from maska_heat.settings import Settings
from maska_heat.web.app import create_app

TZ = ZoneInfo("Europe/Tallinn")
SECRETS = {"cozytouch": {"email": "user@example.test", "password": "pw"}}

# Thermor Onix Connect 80 l as read on 2026-11-09 (trimmed)
CAPS = {22: "70", 87: "3", 227: "0", 230: "0", 265: "67.77", 268: "125", 271: "91", 278: "0", 281: "0", 288: "2"}


def setup_json(caps=CAPS, available=True):
    return [
        {
            "id": 1, "name": "Home", "address": {"street": "never stored"},
            "devices": [
                {"name": "FLAT/S4 IOTHUB DEFAULT", "customName": "gw", "modelFamily": None, "isAvailable": True,
                 "capabilities": [{"capabilityId": 219, "value": "x", "modificationDate": 0}]},
                {"name": "ONIX CONNECT MP 080 S 2250W", "customName": "Boiler", "modelFamily": "Water_Heater",
                 "isAvailable": available,
                 "capabilities": [{"capabilityId": k, "value": v, "modificationDate": 0} for k, v in caps.items()]},
            ],
        }
    ]


class FakeCloud:
    def __init__(self):
        self.caps = dict(CAPS)
        self.logins = 0
        self.expire_next = False
        self.rate_limited = False
        self.reject_login = False
        self.paths: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(request.url.path)
        if request.url.path == "/users/token":
            if self.reject_login:
                return httpx.Response(400, json={"error": "invalid_grant"})
            self.logins += 1
            assert request.headers["authorization"].startswith("Basic ")
            assert b"GA-PRIVATEPERSON%2Fuser%40example.test" in request.content
            return httpx.Response(200, json={"access_token": f"t{self.logins}", "expires_in": 28800, "token_type": "Bearer"})
        if self.rate_limited:
            return httpx.Response(429, headers={"Retry-After": "120"})
        if self.expire_next:
            self.expire_next = False
            return httpx.Response(401)
        assert request.headers["authorization"] == f"Bearer t{self.logins}"
        return httpx.Response(200, json=setup_json(self.caps))

    def adapter(self, device_name=None) -> CozytouchWaterHeaterAdapter:
        client = httpx.AsyncClient(base_url=API_URL, transport=httpx.MockTransport(self.handler))
        return CozytouchWaterHeaterAdapter("user@example.test", "pw", device_name=device_name, client=client)


def test_parse_water_heater():
    s = parse_water_heater(CAPS)
    assert (s.room_temp, s.setpoint, s.level_pct) == (67.77, 70.0, 91.0)
    assert (s.power_on, s.is_heating, s.power_w) == (True, False, None)
    assert s.mode == "Automatic, heats any time"
    heating = parse_water_heater(CAPS | {281: "1", 278: "2210", 230: "2", 227: "1"})
    assert (heating.is_heating, heating.power_w, heating.mode) == (True, 2210.0, "Automatic, heats off-peak, away")


async def test_read_logs_in_once_and_picks_the_water_heater():
    cloud = FakeCloud()
    cz = cloud.adapter()
    s = await cz.read()
    await cz.read()
    assert cloud.logins == 1 and s.room_temp == 67.77 and s.raw["name"].startswith("ONIX")
    assert "address" not in str(s.raw)
    by_name = cloud.adapter(device_name="Boiler")
    assert (await by_name.read()).level_pct == 91.0
    with pytest.raises(DeviceError, match="no 'Nope'"):
        await cloud.adapter(device_name="Nope").read()
    await cz.aclose()


async def test_expired_token_rate_limit_and_bad_login():
    cloud = FakeCloud()
    cz = cloud.adapter()
    await cz.read()
    cloud.expire_next = True
    await cz.read()
    assert cloud.logins == 2
    cloud.rate_limited = True
    with pytest.raises(DeviceError, match="rate limited"):
        await cz.read()
    bad = FakeCloud()
    bad.reject_login = True
    with pytest.raises(DeviceError, match="login rejected"):
        await bad.adapter().read()


async def test_offline_heater_and_never_writes():
    cloud = FakeCloud()
    cz = cloud.adapter()
    cloud_caps = setup_json(available=False)
    cloud.handler = lambda request: (  # noqa: E731
        httpx.Response(200, json={"access_token": "t", "expires_in": 100})
        if request.url.path == "/users/token" else httpx.Response(200, json=cloud_caps)
    )
    cz._client = httpx.AsyncClient(base_url=API_URL, transport=httpx.MockTransport(cloud.handler))
    with pytest.raises(DeviceError, match="not connected"):
        await cz.read()
    assert not cz.capabilities.writable
    with pytest.raises(DeviceError, match="not controlled"):
        await cz.set_setpoint(60)


def with_boiler(settings: Settings) -> Settings:
    raw = settings.model_dump(mode="json")
    raw["devices"]["water_heater"] = {"type": "cozytouch", "power_w": 2250}
    raw["zones"]["hot_water"] = {"name": "Hot water", "device": "water_heater"}
    return Settings.model_validate(raw)


async def test_collector_logs_level_and_rated_power(settings, db):
    s = with_boiler(settings)
    c = Collector(s, SECRETS, db, Notifier(s.notify, None, db, TZ))
    cloud = FakeCloud()
    cloud.caps |= {281: "1"}  # heating, power estimate 0
    c.devices["water_heater"] = cloud.adapter()
    await c.poll_device("water_heater")
    row = db.latest_reading("device:water_heater")
    assert (row["temp"], row["setpoint"], row["level_pct"], row["is_heating"], row["power_w"]) == (67.77, 70.0, 91.0, 1, 2250)
    await c.stop()


def test_collector_needs_login(settings, db):
    s = with_boiler(settings)
    c = Collector(s, {}, db, Notifier(s.notify, None, db, TZ))
    assert "water_heater" not in c.devices
    assert "cozytouch email/password missing" in db.events(1)[0]["message"]


def test_hot_water_card_is_logging_only(settings, db):
    import time

    s = with_boiler(settings)
    db.add_readings([Reading(int(time.time()), "device:water_heater", temp=67.8, setpoint=70, power_on=True, level_pct=91)])
    app = create_app(s, SECRETS, start_jobs=False, db=db)
    with TestClient(app) as client:
        assert app.state.executor.decide("hot_water").mode == "logging"
        page = client.get("/").text
        assert "Hot water" in page and "91 %" in page
        assert 'hx-post="/control/hot_water/mode"' not in client.get("/control").text


def test_old_database_gets_level_column(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE readings (ts INTEGER NOT NULL, source TEXT NOT NULL, temp REAL, setpoint REAL, power_on INTEGER,"
        " is_heating INTEGER, power_w REAL, energy_kwh REAL, humidity REAL, mode TEXT, floor_temp REAL)"
    )
    conn.commit()
    conn.close()
    db = Database(path)
    db.add_readings([Reading(2, "device:x", temp=60.0, level_pct=80.0)])
    assert db.latest_reading("device:x")["level_pct"] == 80.0
    db.close()
