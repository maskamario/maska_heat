"""Mill Gen 3 heaters via the local HTTP API.

Endpoints (LAN, no auth): GET /status, GET /control-status, GET|POST /operation-mode, POST /set-temperature.

Setpoints only take effect in operation mode "Control individually". Switching to it overrides the
weekly program configured in the Mill app, so it is an explicit step (`take_control`), never implicit.
"""

from __future__ import annotations

import httpx

from .base import Capabilities, DeviceError, DeviceState

CONTROL_INDIVIDUALLY = "Control individually"
WEEKLY_PROGRAM = "Weekly program"


class MillLocalAdapter:
    capabilities = Capabilities(
        kind="resistive",
        setpoint_min=5.0,
        setpoint_max=35.0,
        setpoint_step=0.5,
        reports_power=True,
        reports_energy=False,
    )

    def __init__(self, host: str, client: httpx.AsyncClient | None = None, timeout: float = 5.0):
        self.base_url = f"http://{host}"
        self._client = client or httpx.AsyncClient(timeout=timeout)

    async def _get(self, path: str) -> dict:
        try:
            r = await self._client.get(self.base_url + path)
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise DeviceError(f"Mill {self.base_url}{path}: {e}") from e
        data = r.json()
        if data.get("status") != "ok":
            raise DeviceError(f"Mill {path} returned status {data.get('status')!r}")
        return data

    async def _post(self, path: str, payload: dict) -> None:
        try:
            r = await self._client.post(self.base_url + path, json=payload)
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise DeviceError(f"Mill {self.base_url}{path}: {e}") from e

    async def info(self) -> dict:
        return await self._get("/status")

    async def read(self) -> DeviceState:
        d = await self._get("/control-status")
        power = d.get("current_power")
        return DeviceState(
            room_temp=d.get("ambient_temperature"),
            setpoint=d.get("set_temperature"),
            power_on=d.get("operation_mode") != "Off",
            is_heating=bool(power) if power is not None else None,
            power_w=power,
            raw=d,
        )

    async def operation_mode(self) -> str:
        return (await self._get("/operation-mode"))["mode"]

    async def set_operation_mode(self, mode: str) -> None:
        await self._post("/operation-mode", {"mode": mode})

    async def take_control(self) -> None:
        """Switch to 'Control individually' so setpoints from this app take effect."""
        await self.set_operation_mode(CONTROL_INDIVIDUALLY)

    async def release_control(self) -> None:
        """Hand control back to the weekly program set in the Mill app (used on shutdown)."""
        await self.set_operation_mode(WEEKLY_PROGRAM)

    async def set_setpoint(self, temp_c: float) -> None:
        mode = await self.operation_mode()
        if mode != CONTROL_INDIVIDUALLY:
            raise DeviceError(f"Mill is in {mode!r}; call take_control() first, setpoints are ignored otherwise")
        await self._post("/set-temperature", {"type": "Normal", "value": self.capabilities.clamp(temp_c)})

    async def aclose(self) -> None:
        await self._client.aclose()
