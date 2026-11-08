"""IKEA DIRIGERA hub: TIMMERFLOTTE temperature/humidity sensors and MYGGBETT door sensors (docs/DESIGN.md §3.6).

Reads the hub's local, unofficial REST API (https://<hub>:8443/v1, bearer token from `generate-token`, self-signed
certificate) with httpx — no IKEA cloud. Devices are matched by the name given to them in the IKEA Home app.

A TIMMERFLOTTE shows up as two devices with the same relationId: one with the temperature (carries the custom name)
and one with the humidity (keeps the default name).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

import httpx

HUB_PORT = 8443
# A TIMMERFLOTTE reports when the value changes and otherwise every few minutes; its last value counts as current
# while the hub has heard from it within this time.
SENSOR_SILENT_SECONDS = 20 * 60


class HubError(RuntimeError):
    pass


@dataclass
class HubDevice:
    name: str
    kind: str  # 'environment' | 'door'
    reachable: bool
    last_seen: float | None  # unix time the hub last heard from the device
    temp: float | None = None
    humidity: float | None = None
    is_open: bool | None = None
    battery: int | None = None


def _ts(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def parse_devices(raw: list[dict[str, Any]]) -> dict[str, HubDevice]:
    """Sensors and door sensors by their IKEA app name."""
    humidity_by_relation: dict[str, float] = {}
    for d in raw:
        a = d.get("attributes") or {}
        if d.get("deviceType") == "environmentSensor" and a.get("currentRH") is not None and d.get("relationId"):
            humidity_by_relation[d["relationId"]] = float(a["currentRH"])

    out: dict[str, HubDevice] = {}
    for d in raw:
        a = d.get("attributes") or {}
        name = a.get("customName")
        common = dict(reachable=bool(d.get("isReachable")), last_seen=_ts(d.get("lastSeen")), battery=a.get("batteryPercentage"))
        if d.get("deviceType") == "environmentSensor" and a.get("currentTemperature") is not None:
            out[name] = HubDevice(
                name, "environment", temp=round(float(a["currentTemperature"]), 2),
                humidity=a.get("currentRH") if a.get("currentRH") is not None else humidity_by_relation.get(d.get("relationId")),
                **common,
            )
        elif d.get("deviceType") == "openCloseSensor" and a.get("isOpen") is not None:
            out[name] = HubDevice(name, "door", is_open=bool(a["isOpen"]), **common)
    return out


class DirigeraHub:
    def __init__(self, host: str, token: str, client: httpx.AsyncClient | None = None, timeout: float = 10.0):
        self.host = host
        self._client = client or httpx.AsyncClient(
            base_url=f"https://{host}:{HUB_PORT}/v1",
            headers={"Authorization": f"Bearer {token}"},
            verify=False,  # the hub uses a self-signed certificate
            timeout=timeout,
        )

    async def devices(self) -> dict[str, HubDevice]:
        try:
            r = await self._client.get("/devices")
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            hint = " (token rejected — create a new one)" if e.response.status_code in (401, 403) else ""
            raise HubError(f"DIRIGERA {self.host}: HTTP {e.response.status_code}{hint}") from e
        except httpx.HTTPError as e:
            raise HubError(f"DIRIGERA {self.host}: {type(e).__name__} {e}") from e
        return parse_devices(r.json())

    async def aclose(self) -> None:
        await self._client.aclose()


class DirigeraSensor:
    """Same surface as EsphomeSensor (temp, humidity, updated_at, connected, last_error); fed by DirigeraPoller."""

    def __init__(self, name: str, device: str):
        self.name = name
        self.device = device  # name in the IKEA Home app
        self.temp: float | None = None
        self.humidity: float | None = None
        self.updated_at: float | None = None
        self.connected = False
        self.last_error: str | None = None

    def update(self, dev: HubDevice | None, now: float) -> None:
        if dev is None:
            self.connected, self.last_error = False, f"no sensor named {self.device!r} on the hub"
        elif not dev.reachable or dev.last_seen is None or now - dev.last_seen > SENSOR_SILENT_SECONDS:
            self.connected, self.last_error = False, f"{self.device!r} not heard from (battery {dev.battery} %)"
        else:
            self.temp, self.humidity, self.updated_at = dev.temp, dev.humidity, now
            self.connected, self.last_error = True, None


class DirigeraPoller:
    """One hub read keeps the DirigeraSensors current and reports door changes via `on_door(name, ts, is_open)`.
    The collector calls `poll()` on a timer and tracks the hub's health."""

    def __init__(
        self,
        hub: DirigeraHub,
        sensors: list[DirigeraSensor],
        doors: dict[str, str],  # door name in config -> name in the IKEA Home app
        on_door: Callable[[str, int, bool], None],
        interval: float,
    ):
        self.hub = hub
        self.sensors = sensors
        self.doors = doors
        self.on_door = on_door
        self.interval = interval
        self.door_state: dict[str, bool] = {}
        self.door_errors: dict[str, str | None] = {name: None for name in doors}

    async def poll(self) -> None:
        """Raises HubError when the hub can't be read."""
        now = time.time()
        try:
            devices = await self.hub.devices()
        except HubError as e:
            for s in self.sensors:
                s.connected, s.last_error = False, str(e)
            raise
        for s in self.sensors:
            s.update(devices.get(s.device), now)
        for name, device in self.doors.items():
            dev = devices.get(device)
            if dev is None or dev.is_open is None:
                self.door_errors[name] = f"no door sensor named {device!r} on the hub"
                continue
            if not dev.reachable:
                self.door_errors[name] = f"{device!r} not reachable (battery {dev.battery} %)"
                continue
            self.door_errors[name] = None
            if self.door_state.get(name) != dev.is_open:
                # The hub's lastSeen of a door sensor is the time of its last report, i.e. the change itself.
                at = dev.last_seen if dev.last_seen and now - self.interval * 2 <= dev.last_seen <= now else now
                self.door_state[name] = dev.is_open
                self.on_door(name, int(at), dev.is_open)
