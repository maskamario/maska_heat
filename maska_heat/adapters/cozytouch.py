"""Thermor / Atlantic water heater on the Cozytouch cloud (Atlantic "Magellan" API, as the current Cozytouch app).

Read-only for now: the hot water tank is logged to learn when it heats before anything is controlled.
Newer Wi-Fi water heaters (e.g. Thermor Onix Connect) are not on the older Overkiz platform, so pyoverkiz cannot
see them. Login is the Cozytouch app's email and password (secrets.yaml → cozytouch).

Capability ids used (names from GET /magellan/productmodels/capabilities):
    22 DHW_WaterSetpoint · 87 DHW_TemperatureSetpointMode · 227 DHW_stAwayModeStatus · 230 DHW_stOperatingRangeMode
    265 DHW_tWaterTankMiddle · 268 DHW_vV40TankRemaining (litres of 40 °C water) · 271 DHW_rV40StateOfChargeHMI (%)
    278 DHW_pwrElecTankEstimation (W) · 281 DHW_bWaterHeatReq · 288 DHW_stWaterHeaterServiceStatus
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from .base import Capabilities, DeviceError, DeviceState

API_URL = "https://apis.groupe-atlantic.com"
# OAuth client id of the Cozytouch mobile app (public; the same value is published in pyoverkiz and the
# Home Assistant integrations). It identifies the app, not the user.
APP_CLIENT_ID = "Q3RfMUpWeVRtSUxYOEllZkE3YVVOQmpGblpVYToyRWNORHpfZHkzNDJVSnFvMlo3cFNKTnZVdjBh"
TIMEOUT_SECONDS = 30

CAP_SETPOINT, CAP_SETPOINT_MODE, CAP_AWAY, CAP_RANGE = 22, 87, 227, 230
CAP_WATER_TEMP, CAP_V40_LITRES, CAP_CHARGE, CAP_POWER, CAP_HEAT_REQ, CAP_SERVICE = 265, 268, 271, 278, 281, 288

SETPOINT_MODES = {0: "Constant", 2: "Eco/comfort schedule", 3: "Automatic", 4: "Milestone schedule"}
OPERATING_RANGES = {0: "heats any time", 1: "heats on schedule", 2: "heats off-peak", 3: "self-consumption"}
SERVICE_ON = {2, 3}  # On, OnReduced (0 Off, 1 OffAntifrost)


def _num(caps: dict[int, str], cap: int) -> float | None:
    try:
        return float(caps[cap])
    except (KeyError, TypeError, ValueError):
        return None


def _int(caps: dict[int, str], cap: int) -> int | None:
    v = _num(caps, cap)
    return None if v is None else int(v)


def parse_water_heater(caps: dict[int, str]) -> DeviceState:
    setpoint_mode, op_range, service = _int(caps, CAP_SETPOINT_MODE), _int(caps, CAP_RANGE), _int(caps, CAP_SERVICE)
    mode = ", ".join(
        m
        for m in (
            SETPOINT_MODES.get(setpoint_mode, f"mode {setpoint_mode}") if setpoint_mode is not None else None,
            OPERATING_RANGES.get(op_range, f"range {op_range}") if op_range is not None else None,
            "away" if _int(caps, CAP_AWAY) == 1 else None,
        )
        if m
    )
    heat_req = _int(caps, CAP_HEAT_REQ)
    power = _num(caps, CAP_POWER)
    return DeviceState(
        room_temp=_num(caps, CAP_WATER_TEMP),
        setpoint=_num(caps, CAP_SETPOINT),
        power_on=None if service is None else service in SERVICE_ON,
        is_heating=None if heat_req is None else heat_req == 1,
        power_w=power if power else None,  # 0 is also reported while heating; the collector falls back to rated power
        level_pct=_num(caps, CAP_CHARGE),
        mode=mode or None,
        raw={"capabilities": caps, "v40_litres": _num(caps, CAP_V40_LITRES)},
    )


class CozytouchWaterHeaterAdapter:
    capabilities = Capabilities(
        kind="water_heater",
        setpoint_min=50.0,
        setpoint_max=70.0,
        setpoint_step=1.0,
        reports_power=True,
        reports_energy=False,
        sensor="air",
        writable=False,
    )

    def __init__(self, email: str, password: str, device_name: str | None = None, client: httpx.AsyncClient | None = None):
        self._email = email
        self._password = password
        self.device_name = device_name
        self._client = client or httpx.AsyncClient(base_url=API_URL, timeout=TIMEOUT_SECONDS)
        self._token: str | None = None
        self._token_until = 0.0

    async def _login(self) -> None:
        try:
            r = await self._client.post(
                "/users/token",
                headers={"Authorization": f"Basic {APP_CLIENT_ID}"},
                data={"grant_type": "password", "scope": "openid", "username": f"GA-PRIVATEPERSON/{self._email}", "password": self._password},
            )
        except httpx.HTTPError as e:
            raise DeviceError(f"Cozytouch: {type(e).__name__}") from e
        if r.status_code in (400, 401):
            raise DeviceError("Cozytouch: login rejected — check email/password in secrets.yaml → cozytouch")
        if r.status_code != 200:
            raise DeviceError(f"Cozytouch login: HTTP {r.status_code}")
        body = r.json()
        self._token = body["access_token"]
        self._token_until = time.time() + int(body.get("expires_in", 3600)) - 60

    async def _get(self, path: str) -> Any:
        for attempt in (1, 2):
            if not self._token or time.time() >= self._token_until:
                await self._login()
            try:
                r = await self._client.get(path, headers={"Authorization": f"Bearer {self._token}"})
            except httpx.HTTPError as e:
                raise DeviceError(f"Cozytouch: {type(e).__name__}") from e
            if r.status_code == 401 and attempt == 1:
                self._token = None  # expired early — log in again once
                continue
            if r.status_code == 429:
                raise DeviceError(f"Cozytouch: rate limited (retry after {r.headers.get('Retry-After', '?')} s)")
            if r.status_code != 200:
                raise DeviceError(f"Cozytouch {path.split('?')[0]}: HTTP {r.status_code}")
            return r.json()
        raise DeviceError("Cozytouch: not authorised")

    def _pick(self, devices: list[dict]) -> dict:
        for d in devices:
            if self.device_name is not None:
                if self.device_name in (d.get("name"), d.get("customName")):
                    return d
            elif d.get("modelFamily") == "Water_Heater":
                return d
        want = repr(self.device_name) if self.device_name else "a water heater"
        raise DeviceError(f"Cozytouch: no {want} on the account")

    async def read(self) -> DeviceState:
        setups = await self._get("/magellan/cozytouch/setupviewv2")
        devices = [d for s in (setups if isinstance(setups, list) else [setups]) for d in s.get("devices") or []]
        dev = self._pick(devices)  # setup name and address are never stored or logged
        caps = {int(c["capabilityId"]): c.get("value") for c in dev.get("capabilities") or []}
        state = parse_water_heater(caps)
        state.online = bool(dev.get("isAvailable", True))
        state.raw["name"] = dev.get("name")
        if not state.online:
            raise DeviceError(f"Cozytouch: {dev.get('name')} is not connected")
        return state

    async def set_setpoint(self, temp_c: float) -> None:
        raise DeviceError("the water heater is logged only — the app does not control it yet")

    async def aclose(self) -> None:
        await self._client.aclose()
