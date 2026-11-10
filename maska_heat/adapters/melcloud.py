"""Mitsubishi air-to-air units via classic MELCloud (unofficial API, through pymelcloud).

Install with: pip install -e .[melcloud]
Never powers the unit on or off; refuses to change the setpoint while the unit is off.

Energy: the device list's running total (CurrentEnergyConsumed) stopped following real use in Oct 2026, so it is
not logged. Energy comes from the energy report instead:
- `energy_days` — kWh per day, the figures the MELCloud app shows (they add up to the monthly total). Ranges of up
  to 31 days come back per day; longer ones per month.
- `energy_hours` — kWh per hour, only for about the last two weeks, in 0.575 kWh steps that add up to ~15 % more
  than the daily figure. Use it for the shape of the day, not for totals.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Any

from .base import Capabilities, DeviceError, DeviceState


REPORT_MODES = ("Heating", "Cooling", "Auto", "Dry", "Fan", "Other")
MAX_DAILY_RANGE_DAYS = 31


def _total_per_label(rep: dict[str, Any]) -> list[float]:
    labels = rep.get("Labels") or []
    series = [rep.get(m) or [] for m in REPORT_MODES]
    return [sum((s[i] or 0) for s in series if i < len(s)) for i in range(len(labels))]


def _parse_last_seen(value: str | None) -> datetime | None:
    """MELCloud's LastCommunication (UTC), with or without fractional seconds — pymelcloud's own parser
    fails without them."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


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
        except Exception:
            await self._reset()  # don't leak a session per failed poll
            raise
        return self._device

    async def _reset(self) -> None:
        """Drop the connection; the next call logs in again (also covers a token that stopped working)."""
        session, self._session, self._device = self._session, None, None
        if session is not None:
            await session.close()

    async def read(self) -> DeviceState:
        d = await self._connect()
        try:
            await d.update()
        except Exception as e:
            await self._reset()
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
            mode=d.operation_mode,
            raw={
                "name": d.name,
                "operation_mode": d.operation_mode,
                "fan_speed": d.fan_speed,
                "target_temperature_min": d.target_temperature_min,
                "target_temperature_max": d.target_temperature_max,
                "target_temperature_step": d.target_temperature_step,
                "last_seen": str(_parse_last_seen((d._state or {}).get("LastCommunication"))),
            },
        )

    async def energy_report(self, start: date, end: date) -> dict[str, Any]:
        """Energy use per day (or per hour for a single day) from MELCloud's energy report — the figures the
        MELCloud app shows. Unofficial endpoint; `end` is inclusive."""
        from pymelcloud.client import BASE_URL, _headers

        d = await self._connect()
        body = {
            "DeviceID": d.device_id,
            "FromDate": f"{start.isoformat()}T00:00:00",
            "ToDate": f"{end.isoformat()}T00:00:00",
            "UseCurrency": False,
        }
        try:
            async with self._session.post(
                f"{BASE_URL}/EnergyCost/Report", json=body, headers=_headers(d._client.token), raise_for_status=True
            ) as resp:
                return await resp.json()
        except Exception as e:
            await self._reset()
            raise DeviceError(f"MELCloud energy report failed: {e}") from e

    async def energy_days(self, start: date, end: date, tz: tzinfo) -> list[tuple[datetime, float]]:
        """Electricity use per local day, all operating modes together. `end` is inclusive."""
        if (end - start).days + 1 > MAX_DAILY_RANGE_DAYS:
            raise ValueError(f"at most {MAX_DAILY_RANGE_DAYS} days per request")
        rep = await self.energy_report(start, end)
        if rep.get("LabelType") != 1:  # 1 = days
            raise DeviceError(f"MELCloud energy report: expected daily data, got LabelType {rep.get('LabelType')}")
        totals = _total_per_label(rep)
        if len(totals) != (end - start).days + 1:
            raise DeviceError(f"MELCloud energy report: {len(totals)} days for {start} … {end}")
        return [(datetime.combine(start + timedelta(days=i), time(), tz), round(k, 3)) for i, k in enumerate(totals)]

    async def energy_hours(self, day: date, tz: tzinfo) -> list[tuple[datetime, float]]:
        """Electricity use per hour of a local day, all operating modes together. Only for about the last two
        weeks — older days come back as a daily figure (DeviceError)."""
        rep = await self.energy_report(day, day)
        if rep.get("LabelType") != 0:  # 0 = hours
            raise DeviceError(f"MELCloud energy report: expected hourly data, got LabelType {rep.get('LabelType')}")
        by_start: dict[datetime, float] = {}
        for hour, kwh in zip(rep.get("Labels") or [], _total_per_label(rep)):
            # Labels are wall-clock hours 0–23, also on DST change days (the repeated hour comes as one label).
            # A label in the skipped spring hour lands on the next hour, so values are added up, not replaced.
            start = datetime.fromtimestamp(datetime.combine(day, time(int(hour)), tz).timestamp(), tz)
            by_start[start] = by_start.get(start, 0.0) + kwh
        return [(t, round(k, 3)) for t, k in sorted(by_start.items())]

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
        await self._reset()
