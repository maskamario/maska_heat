"""Adax WiFi heaters via the Adax cloud API.

Auth: OAuth2 password grant with the account ID and the API secret generated in the Adax app
(Account → Remote API). The API controls Adax "rooms", not individual heaters.

Temperatures are exchanged in hundredths of a degree (2150 == 21.5 °C).
"""

from __future__ import annotations

import time

import httpx

from .base import Capabilities, DeviceError, DeviceState

API_URL = "https://api-1.adax.no/client-api"


def from_centi(value: float | int | None) -> float | None:
    return None if value is None else round(value / 100.0, 2)


class AdaxCloudAdapter:
    capabilities = Capabilities(
        kind="resistive",
        setpoint_min=5.0,
        setpoint_max=35.0,
        setpoint_step=1.0,
        reports_power=False,
        reports_energy=True,
    )

    def __init__(
        self,
        account_id: str,
        api_secret: str,
        room_name: str | None = None,
        client: httpx.AsyncClient | None = None,
        timeout: float = 15.0,
    ):
        self._account_id = str(account_id)
        self._api_secret = api_secret
        self.room_name = room_name
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._token: str | None = None
        self._token_expires = 0.0
        self._room_id: int | None = None

    async def _auth_header(self) -> dict[str, str]:
        if not self._token or time.monotonic() > self._token_expires - 60:
            try:
                r = await self._client.post(
                    f"{API_URL}/auth/token",
                    data={"grant_type": "password", "username": self._account_id, "password": self._api_secret},
                )
                r.raise_for_status()
            except httpx.HTTPError as e:
                raise DeviceError(f"Adax auth failed: {type(e).__name__} {e}".rstrip()) from e
            body = r.json()
            self._token = body["access_token"]
            self._token_expires = time.monotonic() + float(body.get("expires_in", 3600))
        return {"Authorization": f"Bearer {self._token}"}

    async def content(self) -> dict:
        """Raw account content: rooms and devices (with energy)."""
        try:
            r = await self._client.get(f"{API_URL}/rest/v1/content/?withEnergy=1", headers=await self._auth_header())
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise DeviceError(f"Adax content request failed: {type(e).__name__} {e}".rstrip()) from e
        return r.json()

    def _pick_room(self, content: dict) -> dict:
        rooms = content.get("rooms") or []
        if not rooms:
            raise DeviceError("Adax account has no rooms")
        if self.room_name is None:
            return rooms[0]
        for room in rooms:
            if room.get("name") == self.room_name:
                return room
        names = ", ".join(repr(r.get("name")) for r in rooms)
        raise DeviceError(f"Adax room {self.room_name!r} not found (available: {names})")

    async def read(self) -> DeviceState:
        content = await self.content()
        room = self._pick_room(content)
        self._room_id = room["id"]
        devices = [d for d in content.get("devices") or [] if d.get("roomId") == room["id"]]
        energy_wh = sum(d.get("energyWh") or 0 for d in devices) if devices else None
        enabled = room.get("heatingEnabled")
        return DeviceState(
            room_temp=from_centi(room.get("temperature")),
            setpoint=from_centi(room.get("targetTemperature")),
            power_on=enabled,
            is_heating=None,  # not reported by the cloud API
            energy_kwh=None if energy_wh is None else energy_wh / 1000.0,
            raw={"room": room, "devices": devices},
        )

    async def set_setpoint(self, temp_c: float) -> None:
        if self._room_id is None:
            await self.read()
        target = self.capabilities.clamp(temp_c)
        payload = {"rooms": [{"id": self._room_id, "targetTemperature": str(int(round(target * 100)))}]}
        try:
            r = await self._client.post(f"{API_URL}/rest/v1/control/", json=payload, headers=await self._auth_header())
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise DeviceError(f"Adax set temperature failed: {type(e).__name__} {e}".rstrip()) from e

    async def aclose(self) -> None:
        await self._client.aclose()
