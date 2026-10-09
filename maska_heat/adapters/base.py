"""Common interface for heating devices (see docs/DESIGN.md §3.1).

Adapters only ever change setpoints. Switching a device on or off belongs to the user.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


class DeviceError(RuntimeError):
    pass


@dataclass(frozen=True)
class Capabilities:
    kind: Literal["resistive", "heat_pump", "floor"]
    setpoint_min: float
    setpoint_max: float
    setpoint_step: float
    reports_power: bool
    reports_energy: bool
    sensor: Literal["air", "floor", "air+floor"] = "air"
    writable: bool = True

    def clamp(self, temp_c: float) -> float:
        """Clamp to the device range and round to its step."""
        t = min(max(temp_c, self.setpoint_min), self.setpoint_max)
        return round(round(t / self.setpoint_step) * self.setpoint_step, 2)


@dataclass
class DeviceState:
    room_temp: float | None
    setpoint: float | None
    power_on: bool | None
    is_heating: bool | None
    power_w: float | None = None
    energy_kwh: float | None = None
    online: bool = True
    raw: dict[str, Any] = field(default_factory=dict)


class DeviceAdapter(Protocol):
    capabilities: Capabilities

    async def read(self) -> DeviceState: ...

    async def set_setpoint(self, temp_c: float) -> None: ...
