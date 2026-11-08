import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest

from maska_heat.collector import Collector
from maska_heat.notify import Notifier
from maska_heat.sensors.dirigera import (
    DirigeraHub,
    DirigeraPoller,
    DirigeraSensor,
    HubError,
    parse_devices,
)
from maska_heat.settings import Settings

TZ = ZoneInfo("Europe/Tallinn")


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def hub_json(now: float, temp=21.63, is_open=True, door_seen=None, reachable=True, temp_seen=None):
    """Shape of GET /v1/devices as returned by a DIRIGERA (2026-11), trimmed."""
    return [
        {"id": "gw_1", "type": "gateway", "deviceType": "gateway", "isReachable": True, "attributes": {"customName": "KristMas"}},
        {
            "id": "a8_1", "relationId": "a8", "type": "sensor", "deviceType": "environmentSensor",
            "isReachable": reachable, "lastSeen": iso(temp_seen or now - 60),
            "attributes": {"customName": "Allkorrus", "currentTemperature": temp, "batteryPercentage": 100},
        },
        {
            "id": "a8_2", "relationId": "a8", "type": "sensor", "deviceType": "environmentSensor",
            "isReachable": reachable, "lastSeen": iso(temp_seen or now - 60),
            "attributes": {"customName": "TIMMERFLOTTE temp/hmd sensor", "currentRH": 44, "batteryPercentage": 100},
        },
        {
            "id": "ac_1", "type": "sensor", "deviceType": "openCloseSensor", "isReachable": True,
            "lastSeen": iso(door_seen or now - 3600),
            "attributes": {"customName": "Matise uks", "isOpen": is_open, "batteryPercentage": 90},
        },
    ]


def test_parse_merges_timmerflotte_halves_and_reads_door():
    now = time.time()
    devices = parse_devices(hub_json(now))
    assert set(devices) == {"Allkorrus", "Matise uks"}
    env = devices["Allkorrus"]
    assert (env.kind, env.temp, env.humidity, env.battery) == ("environment", 21.63, 44, 100)
    assert abs(env.last_seen - (now - 60)) < 1
    door = devices["Matise uks"]
    assert (door.kind, door.is_open, door.battery) == ("door", True, 90)


def test_sensor_goes_stale_when_unreachable_silent_or_missing():
    now = time.time()
    s = DirigeraSensor("living", "Allkorrus")
    s.update(parse_devices(hub_json(now))["Allkorrus"], now)
    assert s.connected and s.temp == 21.63 and s.humidity == 44 and s.updated_at == now

    s.update(parse_devices(hub_json(now, reachable=False))["Allkorrus"], now)
    assert not s.connected and "not heard" in s.last_error
    s.update(parse_devices(hub_json(now, temp_seen=now - 3600))["Allkorrus"], now)
    assert not s.connected
    s.update(None, now)
    assert not s.connected and "no sensor named" in s.last_error


class FakeHub:
    def __init__(self, raw):
        self.raw = raw
        self.fail = None

    async def devices(self):
        if self.fail:
            raise HubError(self.fail)
        return parse_devices(self.raw)

    async def aclose(self):
        pass


async def test_poller_reports_door_changes_once_with_hub_time():
    now = time.time()
    hub = FakeHub(hub_json(now, is_open=True))
    sensor = DirigeraSensor("living", "Allkorrus")
    changes = []
    poller = DirigeraPoller(hub, [sensor], {"matis": "Matise uks"}, lambda *a: changes.append(a), interval=15)

    await poller.poll()
    assert sensor.connected and changes == [("matis", int(changes[0][1]), True)]
    assert abs(changes[0][1] - now) < 5  # old lastSeen → time of the first poll

    await poller.poll()
    assert len(changes) == 1  # same state, nothing new

    hub.raw = hub_json(now, is_open=False, door_seen=now - 7)
    await poller.poll()
    assert changes[-1] == ("matis", int(now - 7), False)  # change time from the hub's lastSeen

    hub.fail = "DIRIGERA: ConnectTimeout"
    with pytest.raises(HubError):
        await poller.poll()
    assert not sensor.connected and "ConnectTimeout" in sensor.last_error


async def test_poller_flags_unknown_door():
    poller = DirigeraPoller(FakeHub(hub_json(time.time())), [], {"madli": "Madli uks"}, lambda *a: None, interval=15)
    await poller.poll()
    assert "no door sensor named 'Madli uks'" in poller.door_errors["madli"]


async def test_hub_rejected_token_has_a_clear_error():
    transport = httpx.MockTransport(lambda request: httpx.Response(403))
    hub = DirigeraHub("hub.test", "t", client=httpx.AsyncClient(base_url="https://hub.test:8443/v1", transport=transport))
    with pytest.raises(HubError, match="token rejected"):
        await hub.devices()
    await hub.aclose()


async def test_hub_sends_bearer_token():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        seen["path"] = request.url.path
        return httpx.Response(200, json=hub_json(time.time()))

    hub = DirigeraHub("hub.test", "secret-token")
    hub._client = httpx.AsyncClient(base_url="https://hub.test:8443/v1", headers=hub._client.headers, transport=httpx.MockTransport(handler))
    assert "Allkorrus" in await hub.devices()
    assert seen == {"auth": "Bearer secret-token", "path": "/v1/devices"}
    await hub.aclose()


def test_door_state_only_stores_changes(db):
    assert db.add_door_state("door:matis", 100, True)
    assert not db.add_door_state("door:matis", 200, True)
    assert db.add_door_state("door:matis", 300, False)
    assert [r["is_open"] for r in db.door_changes("door:matis", 0, 1000)] == [1, 0]
    assert db.door_state_at("door:matis", 250)["is_open"] == 1


def _hub_settings(settings: Settings, **over) -> Settings:
    raw = settings.model_dump(mode="json")
    raw["dirigera"] = {"host": "hub.test"}
    raw["sensors"]["living"] = {"type": "dirigera", "device": "Allkorrus"}
    raw["doors"] = {"matis": {"device": "Matise uks", "zone": "matis"}}
    raw.update(over)
    return Settings.model_validate(raw)


def test_settings_validate_doors_and_hub(settings):
    with pytest.raises(ValueError, match="unknown zone"):
        _hub_settings(settings, doors={"x": {"device": "D", "zone": "nope"}})
    with pytest.raises(ValueError, match="dirigera: host is missing"):
        _hub_settings(settings, dirigera=None)
    with pytest.raises(ValueError, match="needs device"):
        _hub_settings(settings, sensors={"living": {"type": "dirigera"}})


async def test_collector_reads_hub_sensors_and_doors(settings, db):
    s = _hub_settings(settings)
    c = Collector(s, {"dirigera": {"token": "t"}}, db, Notifier(s.notify, None, db, TZ))
    assert isinstance(c.sensors["living"], DirigeraSensor) and c.dirigera is not None
    c.dirigera.hub = FakeHub(hub_json(time.time(), is_open=True))

    await c.poll_dirigera()
    await c.log_sensors()
    assert db.latest_reading("sensor:living")["temp"] == 21.63
    assert db.door_state_at("door:matis", 2**62)["is_open"] == 1
    assert not c.health["dirigera"].failing and not c.health["door:matis"].failing

    c.dirigera.hub.fail = "DIRIGERA hub.test: ConnectTimeout"
    await c.poll_dirigera()
    assert c.health["dirigera"].failing
    await c.stop()


def test_collector_without_token_warns(settings, db):
    s = _hub_settings(settings)
    c = Collector(s, {}, db, Notifier(s.notify, None, db, TZ))
    assert c.dirigera is None and "living" not in c.sensors
    assert "token missing" in db.events(1)[0]["message"]
