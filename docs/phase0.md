# Phase 0 — Discovery

Goal: confirm every device and data source can be read (and where needed, set) before building the app.

## Setup (Windows dev machine or Pi)

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev,esphome,melcloud]"   # Pi: .venv/bin/python
cp config.example.yaml config.yaml      # adjust hosts
cp secrets.example.yaml secrets.yaml    # fill in credentials yourself — git-ignored
python -m maska_heat.probe --help
```

## Checklist

| # | Item | Status | Notes |
|---|---|---|---|
| 1 | Elering prices | ✅ | 15-min points, aggregated to hourly slots. Next day appears ~14:00 |
| 2 | Open-Meteo weather | ✅ | Hourly temp, wind (m/s), radiation, cloud for Luige |
| 3 | Mill — read | ✅ | Local API OK at 192.168.1.229 (→ 192.168.1.12 after lease). Model `HeaterGen3OilV2` |
| 4 | Mill — set | ✅ | Instant. Take control → set 20 → release back to weekly program all worked |
| 5 | Adax — local API | ❌ | Heater at 192.168.1.193 (→ .11) runs a local HTTP server, but it requires a token set via Bluetooth provisioning. Staying on cloud |
| 6 | Adax — cloud read | ✅ | One room ("Living Room" in the Adax app = Madli's bedroom), one heater |
| 6b | Adax — cloud set | ✅ | Works, but takes effect only on the heater's next cloud sync (~5 min) |
| 7 | Mitsubishi — MELCloud | ⏳ | Needs MELCloud login (reset password at app.melcloud.com). Check whether the account was migrated to MELCloud Home |
| 8 | ESPHome living sensor | ⏳ | Buy M5Stack Atom Lite + ENV IV, flash `esphome/living-sensor.yaml` |
| 9 | ESPHome outdoor sensor | ⏳ | Atom Lite + DS18B20 probe, `esphome/outdoor-sensor.yaml` |
| 10 | Heater wattages & setpoint ranges confirmed | ⏳ | Mill 1500 W, Adax 600 W; Mitsubishi range read from MELCloud |

## Findings

### Mill (2026-10-09)
- `operation_mode` is **"Weekly program"** with `set_temperature` 10 °C — the Mill app's schedule is driving it.
- Setpoints sent through the API are ignored unless the mode is **"Control individually"**.
  → The app must switch the mode when it takes over (phase 3), and should switch back to "Weekly program" on
  graceful shutdown so the Mill app schedule acts as the fallback.
- `switched_on` is false while idle and `current_power` is 0 — `current_power > 0` is used as "is heating".
- No energy counter in the local API; energy will be estimated from `current_power` samples.
- Write test: setpoint 20 °C at room 20.35 °C → heater went straight to 1500 W, so the Mill regulates on its own
  (PI-style) logic, not a simple "heat while below setpoint" rule. The model learns this from data.
- Releasing control restores the weekly program and its setpoint (10 °C) immediately.

### Adax
- Local HTTP server answers `[{"errorTypeId":404},null]` without a token. Local control (Adax "local API")
  needs Bluetooth provisioning, which typically detaches the heater from the Adax app. Not worth it now.

### Adax cloud (2026-10-09)
- Auth needs the **numeric Account ID** (Adax app → Account) + the Remote API credential, not the app login.
- Temperatures confirmed in hundredths of °C (`temperature: 1979` = 19.79 °C).
- `energyWh` is per device with an `energyTime` timestamp (ms) — reads 0 so far; check whether it is the energy
  of the last interval or a counter once the heater has been running.
- No "is heating" flag in the cloud API → inferred from the model / energy.
- **Latency**: the heater syncs with the cloud about every 5 minutes (`energyTime` and `temperature` update together).
  A setpoint change shows up 2–5 min after sending, and room temperature has the same 5-min resolution.
  → executor must not re-send while a change is pending; read-back verification waits ≥ 6 min.
- `energyWh` stayed 0 for 10 min although the heater **was heating** (confirmed on site) → energy is reported with
  a delay or aggregated over longer periods. Re-check over a day before relying on it; until then estimate energy
  from the model (600 W × heating time).

### Prices
- Elering returns `data.ee[]` with `timestamp` (unix, UTC) and `price` (€/MWh) every 15 minutes.

## Commands

```bash
python -m maska_heat.probe prices
python -m maska_heat.probe weather
python -m maska_heat.probe mill                                   # read only
python -m maska_heat.probe mill --take-control --set 20 --release-control   # write test, ends back on weekly program
python -m maska_heat.probe adax                                   # lists rooms, reads selected room
python -m maska_heat.probe adax --set 20
python -m maska_heat.probe melcloud
python -m maska_heat.probe sensor living
```
