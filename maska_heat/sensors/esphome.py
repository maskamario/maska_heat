"""ESPHome temperature/humidity sensors via the native API (aioesphomeapi).

Install with: pip install -e .[esphome]
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass


class SensorError(RuntimeError):
    pass


@dataclass
class SensorReading:
    name: str
    object_id: str
    value: float
    unit: str


async def read_esphome(host: str, encryption_key: str | None, seconds: float = 15.0, port: int = 6053) -> list[SensorReading]:
    """Connect, listen for sensor states for `seconds`, return the latest value of each sensor."""
    try:
        from aioesphomeapi import APIClient, SensorInfo, SensorState
    except ImportError as e:
        raise SensorError("ESPHome support not installed: pip install -e .[esphome]") from e

    client = APIClient(host, port, password=None, noise_psk=encryption_key or None)
    try:
        await client.connect(login=True)
    except Exception as e:
        raise SensorError(f"ESPHome {host}: {e}") from e
    try:
        entities, _ = await client.list_entities_services()
        infos = {e.key: e for e in entities if isinstance(e, SensorInfo)}
        latest: dict[int, SensorReading] = {}

        def on_state(state) -> None:
            if isinstance(state, SensorState) and state.key in infos and not state.missing_state:
                info = infos[state.key]
                latest[state.key] = SensorReading(info.name, info.object_id, state.state, info.unit_of_measurement)

        client.subscribe_states(on_state)
        await asyncio.sleep(seconds)
        return list(latest.values())
    finally:
        await client.disconnect()
