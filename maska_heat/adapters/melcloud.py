"""Mitsubishi air-to-air units via classic MELCloud (unofficial API, through pymelcloud).

Install with: pip install -e .[melcloud]
Never powers the unit on or off; refuses to change the setpoint while the unit is off.
"""

from __future__ import annotations

from typing import Any

from .base import Capabilities, DeviceError, DeviceState


class MelcloudAdapter:
    capabilities = Capabilities(
        kind="heat_pump",
        setpoint_min=16.0,  # replaced by the unit's own limits after the first read
        setpoint_max=31.0,
        setpoint_step=0.5,
        reports_power=False,
        reports_energy=True,
    )

    def __init__(self, email: str, password: str, device_name: str | None = None):
        self._email = email
        self._password = password
        self.device_name = device_name
        self._session = None
        self._device = None

    async def _connect(self) -> Any:
        if self._device is not None:
            return self._device
        try:
            import aiohttp
            import pymelcloud
        except ImportError as e:
            raise DeviceError("MELCloud support not installed: pip install -e .[melcloud]") from e

        self._session = aiohttp.ClientSession()
        try:
            token = await pymelcloud.login(self._email, self._password, session=self._session)
            devices = await pymelcloud.get_devices(token, session=self._session)
        except Exception as e:  # pymelcloud raises plain aiohttp errors
            raise DeviceError(f"MELCloud login/device list failed: {e}") from e

        units = devices.get(pymelcloud.DEVICE_TYPE_ATA, [])
        if not units:
            raise DeviceError("No air-to-air units on this MELCloud account")
        if self.device_name is None:
            self._device = units[0]
        else:
            matches = [u for u in units if u.name == self.device_name]
            if not matches:
                names = ", ".join(repr(u.name) for u in units)
                raise DeviceError(f"MELCloud device {self.device_name!r} not found (available: {names})")
            self._device = matches[0]
        return self._device

    async def read(self) -> DeviceState:
        d = await self._connect()
        try:
            await d.update()
        except Exception as e:
            raise DeviceError(f"MELCloud update failed: {e}") from e
        if d.target_temperature_min is not None and d.target_temperature_max is not None:
            self.capabilities = Capabilities(
                kind="heat_pump",
                setpoint_min=float(d.target_temperature_min),
                setpoint_max=float(d.target_temperature_max),
                setpoint_step=float(d.target_temperature_step or 0.5),
                reports_power=False,
                reports_energy=True,
            )
        return DeviceState(
            room_temp=d.room_temperature,
            setpoint=d.target_temperature,
            power_on=d.power,
            is_heating=None,
            energy_kwh=d.total_energy_consumed,
            raw={
                "name": d.name,
                "operation_mode": d.operation_mode,
                "fan_speed": d.fan_speed,
                "target_temperature_min": d.target_temperature_min,
                "target_temperature_max": d.target_temperature_max,
                "target_temperature_step": d.target_temperature_step,
                "last_seen": str(d.last_seen),
            },
        )

    async def set_setpoint(self, temp_c: float) -> None:
        d = await self._connect()
        await d.update()
        if not d.power:
            raise DeviceError("Mitsubishi is switched off — not changing anything")
        try:
            await d.set({"target_temperature": self.capabilities.clamp(temp_c)})
        except Exception as e:
            raise DeviceError(f"MELCloud set failed: {e}") from e

    async def aclose(self) -> None:
        if self._session is not None:
            await self._session.close()
