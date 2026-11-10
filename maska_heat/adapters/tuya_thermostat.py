"""Tuya Wi-Fi floor heating thermostat (Heatline HL-05 / Hysen HY511WE), read over the local network with tinytuya.

Read-only for now: bathroom 2 is logged to learn how the floor behaves before anything is controlled.

Data points, from the device's Tuya thing model (temperatures in 0.1 °C):
    1 Power · 3 temp_set · 4 Mode (Manual/Program/Holiday/TempProg) · 12 Fault · 24 temp_current
    102 Heating_state · 103 Ext_temperature (floor sensor, raw) · 116 temp_sensor_type (in/ext/all)

temp_current is the temperature the thermostat controls on, after its calibration (DP 109), and what the Smart Life
app shows. With the floor sensor selected ("ext") that is the floor - the thermostat then reports no air
temperature, and DP 103 is the same sensor uncalibrated (1.0 °C higher on 2026-11-09 with calibration -1.0 °C).
"""

from __future__ import annotations

import asyncio
from typing import Any

from .base import Capabilities, DeviceError, DeviceState

DP_POWER, DP_SETPOINT, DP_MODE, DP_FAULT, DP_TEMP = "1", "3", "4", "12", "24"
DP_HEATING, DP_FLOOR, DP_SENSOR = "102", "103", "116"
TIMEOUT_SECONDS = 5


def _tenths(dps: dict[str, Any], dp: str) -> float | None:
    v = dps.get(dp)
    return None if v is None else round(v / 10, 1)


def parse_status(dps: dict[str, Any], sensor_mode: str | None = None) -> DeviceState:
    """`sensor_mode`: the last known DP 116, for a reply that leaves it out (else floor and air would swap)."""
    controlled = _tenths(dps, DP_TEMP)
    on_floor = dps.get(DP_SENSOR, sensor_mode) == "ext"
    return DeviceState(
        room_temp=None if on_floor else controlled,
        setpoint=_tenths(dps, DP_SETPOINT),
        power_on=dps.get(DP_POWER),
        is_heating=dps.get(DP_HEATING),
        floor_temp=controlled if on_floor else _tenths(dps, DP_FLOOR),
        mode=dps.get(DP_MODE),
        raw=dict(dps),
    )


class TuyaThermostatAdapter:
    capabilities = Capabilities(
        kind="floor",
        setpoint_min=5.0,
        setpoint_max=35.0,
        setpoint_step=0.5,
        reports_power=False,
        reports_energy=False,
        sensor="air+floor",
        writable=False,
    )

    def __init__(self, device_id: str, host: str, local_key: str, version: float = 3.5, device=None):
        if device is None:
            import tinytuya

            device = tinytuya.Device(device_id, host, local_key, version=version)
            device.set_socketTimeout(TIMEOUT_SECONDS)
            device.set_socketRetryLimit(2)
        self.host = host
        self._device = device
        self._lock = asyncio.Lock()  # these thermostats accept one local connection at a time
        self._sensor_mode: str | None = None  # last DP 116 seen: which sensor DP 24 belongs to

    async def read(self) -> DeviceState:
        async with self._lock:
            status = await asyncio.to_thread(self._device.status)
        if not isinstance(status, dict) or "Error" in status or "dps" not in status:
            err = status.get("Error") if isinstance(status, dict) else status
            raise DeviceError(f"Tuya {self.host}: {err or 'no answer'}")
        self._sensor_mode = status["dps"].get(DP_SENSOR, self._sensor_mode)
        state = parse_status(status["dps"], self._sensor_mode)
        if status["dps"].get(DP_FAULT):
            state.mode = f"{state.mode} fault {status['dps'][DP_FAULT]}"
        return state

    async def set_setpoint(self, temp_c: float) -> None:
        """Write the setpoint (DP 3). Only the price-spike pause uses this — the zone is otherwise logged only
        (capabilities.writable is False, so the executor never controls it)."""
        value = int(round(self.capabilities.clamp(temp_c) * 10))
        async with self._lock:
            result = await asyncio.to_thread(self._device.set_value, int(DP_SETPOINT), value)
        if isinstance(result, dict) and "Error" in result:
            raise DeviceError(f"Tuya {self.host}: {result['Error']}")

    async def aclose(self) -> None:
        close = getattr(self._device, "close", None)
        if close:
            await asyncio.to_thread(close)
