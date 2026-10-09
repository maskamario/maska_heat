"""Phase 0 discovery CLI — check that every device and data source can be read (and set).

    python -m maska_heat.probe prices
    python -m maska_heat.probe weather
    python -m maska_heat.probe mill [--take-control] [--set 21] [--release-control]
    python -m maska_heat.probe adax [--set 21]
    python -m maska_heat.probe melcloud [--set 21]
    python -m maska_heat.probe sensor living [--seconds 20]

Reads are always safe. Anything that changes a device needs an explicit flag.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .adapters.base import DeviceError, DeviceState
from .config import ConfigError, load_config, load_secrets, require


def _tz(cfg: dict) -> ZoneInfo:
    return ZoneInfo(cfg.get("location", {}).get("timezone", "Europe/Tallinn"))


def _device_cfg(cfg: dict, type_: str) -> dict:
    for name, dev in (cfg.get("devices") or {}).items():
        if dev.get("type") == type_:
            return dev
    raise ConfigError(f"no device with type {type_!r} in config.yaml")


def _print_state(label: str, s: DeviceState, raw: bool = True) -> None:
    print(f"== {label}")
    print(f"  room temp : {s.room_temp} °C")
    print(f"  setpoint  : {s.setpoint} °C")
    print(f"  power on  : {s.power_on}")
    print(f"  heating   : {s.is_heating}")
    print(f"  power     : {s.power_w} W")
    print(f"  energy    : {s.energy_kwh} kWh")
    if raw:
        print("  raw       : " + json.dumps(s.raw, ensure_ascii=False, default=str, indent=2).replace("\n", "\n  "))


async def probe_prices(cfg: dict, args) -> None:
    from .providers.prices import fetch_prices, to_slots

    tz = _tz(cfg)
    today = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    raw = await fetch_prices(today, today + timedelta(days=2), area=cfg.get("price", {}).get("area", "ee"))
    slot = cfg.get("price", {}).get("slot_minutes", 60)
    slots = to_slots(raw, slot)
    print(f"== Elering: {len(raw)} raw points ({raw[0].minutes if raw else '?'} min) → {len(slots)} slots of {slot} min")
    for p in slots:
        print(f"  {p.start.astimezone(tz):%a %d.%m %H:%M}  {p.eur_mwh:8.2f} €/MWh")
    if slots:
        last = slots[-1].start.astimezone(tz)
        print(f"  prices known until {last + timedelta(minutes=slot):%a %d.%m %H:%M}")


async def probe_weather(cfg: dict, args) -> None:
    from .providers.weather import fetch_forecast

    loc = cfg["location"]
    tz = _tz(cfg)
    points = await fetch_forecast(loc["latitude"], loc["longitude"], days=2)
    print(f"== Open-Meteo: {len(points)} hourly points")
    for p in points[:: args.every]:
        print(
            f"  {p.start.astimezone(tz):%a %d.%m %H:%M}  {p.temp_c:5.1f} °C  wind {p.wind_ms:4.1f} m/s"
            f"  sun {p.radiation_wm2:5.0f} W/m²  cloud {p.cloud_pct:3.0f} %"
        )


async def probe_mill(cfg: dict, args) -> None:
    from .adapters.mill_local import MillLocalAdapter

    dev = _device_cfg(cfg, "mill_local")
    mill = MillLocalAdapter(args.host or dev["host"])
    try:
        info = await mill.info()
        print(f"== Mill {info.get('name')} firmware {info.get('version')}")
        _print_state("Mill state", await mill.read())
        if args.take_control:
            await mill.take_control()
            print("  → operation mode set to 'Control individually'")
        if args.set is not None:
            await mill.set_setpoint(args.set)
            print(f"  → setpoint {args.set} °C sent")
            await asyncio.sleep(2)
            _print_state("Mill state after set", await mill.read(), raw=False)
        if args.release_control:
            await mill.release_control()
            print("  → operation mode set back to 'Weekly program'")
            print(f"  mode now: {await mill.operation_mode()}")
    finally:
        await mill.aclose()


async def probe_adax(cfg: dict, secrets: dict, args) -> None:
    from .adapters.adax_cloud import AdaxCloudAdapter, from_centi

    dev = _device_cfg(cfg, "adax_cloud")
    account_id, api_secret = require(secrets.get("adax") or {}, "account_id", "api_secret", where="secrets.yaml → adax")
    adax = AdaxCloudAdapter(account_id, api_secret, room_name=dev.get("room_name"))
    try:
        content = await adax.content()
        print("== Adax rooms on account:")
        for r in content.get("rooms") or []:
            print(
                f"  id={r.get('id')} name={r.get('name')!r} temp={from_centi(r.get('temperature'))} "
                f"target={from_centi(r.get('targetTemperature'))} heatingEnabled={r.get('heatingEnabled')}"
            )
        _print_state("Adax selected room", await adax.read())
        if args.set is not None:
            await adax.set_setpoint(args.set)
            print(f"  → setpoint {args.set} °C sent (cloud may take a minute to reflect it)")
            await asyncio.sleep(5)
            _print_state("Adax after set", await adax.read(), raw=False)
    finally:
        await adax.aclose()


async def probe_melcloud(cfg: dict, secrets: dict, args) -> None:
    from .adapters.melcloud import MelcloudAdapter

    dev = _device_cfg(cfg, "melcloud")
    email, password = require(secrets.get("melcloud") or {}, "email", "password", where="secrets.yaml → melcloud")
    mel = MelcloudAdapter(email, password, device_name=dev.get("device_name"))
    try:
        _print_state("Mitsubishi", await mel.read())
        print(f"  setpoint range: {mel.capabilities.setpoint_min}–{mel.capabilities.setpoint_max} step {mel.capabilities.setpoint_step}")
        if args.set is not None:
            await mel.set_setpoint(args.set)
            print(f"  → setpoint {args.set} °C sent")
    finally:
        await mel.aclose()


async def probe_sensor(cfg: dict, secrets: dict, args) -> None:
    from .sensors.esphome import read_esphome

    sensor = (cfg.get("sensors") or {}).get(args.name)
    if not sensor:
        raise ConfigError(f"no sensor {args.name!r} in config.yaml")
    key = (secrets.get("esphome") or {}).get(args.name)
    readings = await read_esphome(sensor["host"], key, seconds=args.seconds)
    print(f"== ESPHome {args.name} ({sensor['host']}): {len(readings)} sensors")
    for r in readings:
        print(f"  {r.name:<20} {r.value:8.2f} {r.unit}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="maska-probe", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="target", required=True)
    sub.add_parser("prices")
    w = sub.add_parser("weather")
    w.add_argument("--every", type=int, default=3, help="print every Nth hour")
    m = sub.add_parser("mill")
    m.add_argument("--host", help="override host from config.yaml")
    m.add_argument("--take-control", action="store_true", help="switch to 'Control individually' (overrides Mill app weekly program)")
    m.add_argument("--set", type=float, help="set temperature °C (requires control)")
    m.add_argument("--release-control", action="store_true", help="switch back to 'Weekly program'")
    a = sub.add_parser("adax")
    a.add_argument("--set", type=float)
    mc = sub.add_parser("melcloud")
    mc.add_argument("--set", type=float)
    s = sub.add_parser("sensor")
    s.add_argument("name", help="sensor name from config.yaml, e.g. living")
    s.add_argument("--seconds", type=float, default=20)
    args = parser.parse_args(argv)

    try:
        cfg = load_config()
        secrets = load_secrets()
        if args.target == "prices":
            coro = probe_prices(cfg, args)
        elif args.target == "weather":
            coro = probe_weather(cfg, args)
        elif args.target == "mill":
            coro = probe_mill(cfg, args)
        elif args.target == "adax":
            coro = probe_adax(cfg, secrets, args)
        elif args.target == "melcloud":
            coro = probe_melcloud(cfg, secrets, args)
        else:
            coro = probe_sensor(cfg, secrets, args)
        asyncio.run(coro)
    except (ConfigError, DeviceError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
