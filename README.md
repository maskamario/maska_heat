# maska_heat

Raspberry Pi app that runs the house's electric heating — Adax and Mill radiators, a Mitsubishi heat pump and
later bathroom floor heating — around the hourly electricity price, the weather forecast and what it learns about
how each room heats up and cools down. It also tells you when lighting the masonry heater is cheaper.

- Design: [docs/DESIGN.md](docs/DESIGN.md)
- Phase 0 findings: [docs/phase0.md](docs/phase0.md)
- Raspberry Pi setup: [docs/deploy.md](docs/deploy.md)

**Current phase: 1 — observe, plus manual / schedule control.** The app polls the heaters and sensors, logs them with
electricity prices and weather to SQLite, computes the real cost per hour (spot + Võrk 4 network + fees + VAT), sends
ntfy alerts when a device goes offline, and serves a dashboard with history and wood-fire logging.

Each room has a control mode on the **Control** page:

- **Hands-off** (default) — the heater follows its own app; maska-heat only watches.
- **Manual** — a fixed temperature set in the UI.
- **Smart** — follows the comfort modes (Day / Night / Out / Away), house schedule, kids' school timetable and
  away periods from the **Settings** page, pre-heating before warmer periods. Price-based shifting comes later.

Switching a room back to Hands-off hands the heater back: the Mill returns to its weekly program, the others get the
setpoint they had before maska-heat took over.

## Quick start

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev,esphome,melcloud]"   # on the Pi: .venv/bin/python
cp config.example.yaml config.yaml
cp secrets.example.yaml secrets.yaml   # credentials, git-ignored
python -m maska_heat.probe prices
python -m maska_heat          # dashboard on http://localhost:8000
python -m pytest
```
