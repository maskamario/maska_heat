"""ESPHome temperature/humidity sensors via the native API (aioesphomeapi).

Install with: pip install -e .[esphome]
"""

from __future__ import annotations

import asyncio
import time
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


def _classify(info) -> str | None:
    """Map an ESPHome sensor entity to 'temp' / 'humidity', ignoring others (WiFi signal etc.)."""
    device_class = (getattr(info, "device_class", "") or "").lower()
    unit = info.unit_of_measurement or ""
    if device_class == "temperature" or unit == "°C":
        return "temp"
    if device_class == "humidity" or (unit == "%" and "humid" in info.object_id):
        return "humidity"
    return None


class EsphomeSensor:
    """Keeps a persistent connection to one ESPHome device and remembers the latest values.

    `run()` never returns on its own: it reconnects with backoff when the device disappears.
    """

    def __init__(self, name: str, host: str, encryption_key: str | None, port: int = 6053):
        self.name = name
        self.host = host
        self.port = port
        self._key = encryption_key or None
        self.temp: float | None = None
        self.humidity: float | None = None
        self.updated_at: float | None = None  # unix time of the last state update
        self.connected = False
        self.last_error: str | None = None

    async def run(self) -> None:
        try:
            from aioesphomeapi import APIClient, SensorState
        except ImportError as e:
            raise SensorError("ESPHome support not installed: pip install -e .[esphome]") from e

        backoff = 5.0
        while True:
            client = APIClient(self.host, self.port, password=None, noise_psk=self._key)
            stopped = asyncio.Event()

            async def on_stop(expected_disconnect: bool) -> None:
                stopped.set()

            try:
                await client.connect(on_stop=on_stop, login=True)
                entities, _ = await client.list_entities_services()
                kinds = {e.key: k for e in entities if hasattr(e, "unit_of_measurement") and (k := _classify(e))}

                def on_state(state) -> None:
                    if isinstance(state, SensorState) and state.key in kinds and not state.missing_state:
                        setattr(self, kinds[state.key], round(float(state.state), 2))
                        self.updated_at = time.time()

                client.subscribe_states(on_state)
                self.connected = True
                self.last_error = None
                backoff = 5.0
                await stopped.wait()
            except asyncio.CancelledError:
                await client.disconnect(force=True)
                raise
            except Exception as e:  # connection refused, timeouts, bad key …
                self.last_error = str(e) or type(e).__name__
                try:
                    await client.disconnect(force=True)
                except Exception:
                    pass
            finally:
                self.connected = False
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300.0)
