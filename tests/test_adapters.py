import json

import httpx
import pytest

from maska_heat.adapters.adax_cloud import AdaxCloudAdapter
from maska_heat.adapters.base import Capabilities, DeviceError
from maska_heat.adapters.mill_local import MillLocalAdapter

MILL_STATUS = {
    "ambient_temperature": 20.4,
    "current_power": 0,
    "control_signal": 0,
    "set_temperature": 10,
    "switched_on": False,
    "operation_mode": "Weekly program",
    "status": "ok",
}


def test_capabilities_clamp_and_step():
    caps = Capabilities("resistive", 5, 35, 0.5, False, False)
    assert caps.clamp(21.3) == 21.5
    assert caps.clamp(2) == 5
    assert caps.clamp(40) == 35


async def test_mill_read():
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=MILL_STATUS))
    mill = MillLocalAdapter("mill", client=httpx.AsyncClient(transport=transport))
    s = await mill.read()
    assert (s.room_temp, s.setpoint, s.is_heating, s.power_on) == (20.4, 10, False, True)


async def test_mill_refuses_setpoint_in_weekly_program():
    posted = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            posted.append(req.url.path)
        return httpx.Response(200, json={"mode": "Weekly program", "status": "ok"})

    mill = MillLocalAdapter("mill", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(DeviceError):
        await mill.set_setpoint(21)
    assert posted == []


async def test_mill_sets_clamped_setpoint_when_in_control():
    posted = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            posted.append((req.url.path, json.loads(req.content)))
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(200, json={"mode": "Control individually", "status": "ok"})

    mill = MillLocalAdapter("mill", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await mill.set_setpoint(21.2)
    assert posted == [("/set-temperature", {"type": "Normal", "value": 21.0})]


def adax_handler(posted: list):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/auth/token"):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        if req.url.path.endswith("/content/"):
            return httpx.Response(
                200,
                json={
                    "rooms": [
                        {"id": 1, "name": "Kitchen", "temperature": 2000, "targetTemperature": 1800, "heatingEnabled": True},
                        {"id": 2, "name": "Madli", "temperature": 2150, "targetTemperature": 2100, "heatingEnabled": True},
                    ],
                    "devices": [{"id": 9, "roomId": 2, "energyWh": 1500}],
                },
            )
        posted.append(json.loads(req.content))
        return httpx.Response(200, json={})

    return handler


async def test_adax_read_selected_room():
    adax = AdaxCloudAdapter("1", "s", room_name="Madli", client=httpx.AsyncClient(transport=httpx.MockTransport(adax_handler([]))))
    s = await adax.read()
    assert (s.room_temp, s.setpoint, s.energy_kwh) == (21.5, 21.0, 1.5)


async def test_adax_set_sends_centidegrees():
    posted: list = []
    adax = AdaxCloudAdapter("1", "s", room_name="Madli", client=httpx.AsyncClient(transport=httpx.MockTransport(adax_handler(posted))))
    await adax.set_setpoint(20.6)
    assert posted == [{"rooms": [{"id": 2, "targetTemperature": "2100"}]}]


async def test_adax_unknown_room():
    adax = AdaxCloudAdapter("1", "s", room_name="Nope", client=httpx.AsyncClient(transport=httpx.MockTransport(adax_handler([]))))
    with pytest.raises(DeviceError, match="Madli"):
        await adax.read()
