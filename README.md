# maska_heat

Raspberry Pi app that runs the house's electric heating — Adax and Mill radiators, a Mitsubishi heat pump and
later bathroom floor heating — around the hourly electricity price, the weather forecast and what it learns about
how each room heats up and cools down. It also tells you when lighting the masonry heater is cheaper.

- Design: [docs/DESIGN.md](docs/DESIGN.md)
- Current phase: [docs/phase0.md](docs/phase0.md) — device and data source discovery

## Quick start

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev,esphome,melcloud]"   # on the Pi: .venv/bin/python
cp config.example.yaml config.yaml
cp secrets.example.yaml secrets.yaml   # credentials, git-ignored
python -m maska_heat.probe prices
python -m pytest
```
