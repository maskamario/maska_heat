# maska_heat

Raspberry Pi app that runs the house's electric heating — Adax and Mill radiators, a Mitsubishi heat pump and
later bathroom floor heating — around the hourly electricity price, the weather forecast and what it learns about
how each room heats up and cools down. It also tells you when lighting the masonry heater is cheaper.

- Design: [docs/DESIGN.md](docs/DESIGN.md)
- Phase 0 findings: [docs/phase0.md](docs/phase0.md)
- Raspberry Pi setup, phones, Tailscale, updating: [docs/deploy.md](docs/deploy.md)
- ESPHome sensor configs (optional outdoor probe): [esphome/](esphome/)

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

## Status & next steps (2026-10-12)

Running on the Raspberry Pi 3 B+ (`maska-heat`, systemd service `maska-heat`), reachable at home on port 8000 and
from the phones through Tailscale (`http://maska-heat:8000`).

| Area | State |
|---|---|
| Devices | Mill (local, `192.168.1.12`), Adax (cloud), Mitsubishi "Elutuba" (MELCloud) — all logging |
| Prices / tariff | Elering EE hourly; Võrk 4 + fees from the Sept 2026 Alexela/Elektrilevi invoice. Alexela margin unknown (0) |
| Notifications | ntfy, both phones subscribed |
| Control | Per-room Hands-off / Manual / Smart (schedule only, no price shifting yet) |
| Room sensors | **Pending** — IKEA DIRIGERA + TIMMERFLOTTE chosen; living area currently uses the heat pump's own (high-reading) sensor |
| Outdoor | Open-Meteo forecast |

Next, in order:

1. **IKEA sensors** (when in stock): set up the DIRIGERA and one TIMMERFLOTTE in the IKEA Home app, place it in the
   living area (interior wall, ~1.5 m, away from stoves, heat pump airflow, windows, sun). Then build the
   `DirigeraSensorAdapter` (docs/DESIGN.md §3.6), verify the sensor reads, enable `sensors.living`, and buy more for
   the kids' rooms / hall if it works.
2. **Keep logging** every masonry and cookstove fire with the dashboard buttons.
3. **Phase 2 — learn** room heating/cooling rates, heat pump COP and fire effect once there are 1–2 weeks of data.
4. **Phase 3 — price-aware Smart mode** and the wood-fire advisor.

Updating the Pi after a push: `cd ~/maska_heat && git pull && sudo systemctl restart maska-heat`.

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
