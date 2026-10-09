# maska_heat — Design

Raspberry Pi app that controls the house's electric heating (radiators, air-to-air heat pump, later bathroom
floor heating) based on electricity spot price, weather forecast and learned per-zone thermal behaviour.
It also advises when heating with wood is cheaper, sends phone notifications, and has a local web UI.

Status: **design — no code yet**. Remaining open questions in §15.

---

## 1. House & zones

```
        ┌──────────────┬────────┬──────────────┐
        │ Madli's      │  Hall  │ Matis's      │
        │ bedroom      │(no     │ bedroom      │
        │ Adax 600 W   │ heater)│ Mill 1500 W  │
        └──────────────┴────────┴──────────────┘
        ┌──────────────────────────────────────┐
        │ Living area (living room + kitchen)  │
        │ Mitsubishi heat pump                  │
        │ Masonry heater · Wood cookstove +     │
        │ thermal mass wall                     │
        └──────────────────────────────────────┘
        Bathroom(s): electric floor heating — later
```

| Zone | Controlled device | Uncontrolled heat sources |
|---|---|---|
| **Madli's bedroom** | ADAX Clea H 06 WiFi, panel heater, 600 W | — |
| **Matis's bedroom** | Mill OIL1500WIFI3 (Gen 3), oil radiator, 1500 W | — |
| **Living area** (living room + kitchen) | Mitsubishi MSZ-LN35VG2W / MUZ-LN35VGHZ2 heat pump (Hyper Heating) + **independent WiFi temperature sensor** (§3.6) | Masonry heater, wood cookstove with thermal mass wall |
| **Hall** (between kids' rooms) | none — passive | Gets heat from neighbouring zones. Optional sensor later |
| **Bathroom(s)** | Floor heating + thermostat — **deferred**, design hook only (§3.5) | — |

Weather location: Luige, Kiili vald, Harjumaa (≈ 59.33 N, 24.74 E, exact value in config).

---

## 2. Platform

| Topic | Decision |
|---|---|
| Hardware | Raspberry Pi 3 Model B+ (1 GB RAM), wired Ethernet |
| OS | Raspberry Pi OS Lite 64-bit |
| Runtime | Python 3.11+, FastAPI + Uvicorn, APScheduler, SQLite (WAL), `systemd` service in a venv. No Docker |
| UI | Server-rendered HTML + HTMX + Chart.js (static files). No Node build on the Pi |
| Notifications | **ntfy** — see §9 |
| Remote access | Later: Tailscale (also lets notification action buttons reach the Pi from outside home) |
| Optional later | Mosquitto + Zigbee2MQTT if Zigbee floor thermostats are added |

**Standalone app, not Home Assistant**: HA struggles on a Pi 3 B+ and the learning/planning would be custom anyway.
Device access is behind adapters, so an `HomeAssistantAdapter` can be added later without touching the planner.

**SD-card care**: SQLite WAL, batched writes, raw readings 90 days then 15-min averages forever, journald cap.

---

## 3. Device integrations

### 3.1 Common adapter interface

```python
class Capabilities:
    kind: Literal["resistive", "heat_pump", "floor"]
    setpoint_min: float; setpoint_max: float; setpoint_step: float
    reports_power: bool; reports_energy: bool
    sensor: Literal["air", "floor", "air+floor"]
    writable: bool                       # False for ManualAdapter

class DeviceAdapter(Protocol):
    capabilities: Capabilities
    async def read(self) -> DeviceState: ...   # temps, setpoint, power_on, is_heating, power_w, energy_kwh, online
    async def set_setpoint(self, temp_c: float) -> None: ...
```

Adapters **never switch a device's power on or off** — only setpoints. Power state belongs to the user.

`SimulatedAdapter` (room driven by the thermal model) lets the whole app run on a laptop.

### 3.2 Adax Clea H 06 WiFi — cloud
- `https://api-1.adax.no/client-api`, OAuth2 with account ID + API secret from the Adax app (Account → Remote API).
- Controls an **Adax "room"** → map zone → Adax room ID. Poll every 5–10 min, write only on change, cache token.
- Phase 0: check if this model also offers Adax local API; prefer it if so.

### 3.3 Mill OIL1500WIFI3 — local
- LAN HTTP API (`/control-status`, `/set-temperature`), static DHCP lease on router. Poll every 1–2 min.
- Oil radiator = slow emitter: modelled with heat-release lag (§7).

### 3.4 Mitsubishi MSZ-LN35VG2W — MELCloud (classic)

The unit is registered in **classic MELCloud**, which has a well-known (unofficial) API used by the
`pymelcloud` library and Home Assistant's core `melcloud` integration. Three-step fallback:

| Level | How | When |
|---|---|---|
| **1. MELCloud adapter** (default) | `pymelcloud`-style API: read room temp, setpoint, power, energy; set setpoint | Start here. Poll every 5 min (respect rate limits) |
| **2. ESP32 on CN105** (local) | ESPHome `MitsubishiCN105ESPHome` firmware, our app talks via `aioesphomeapi` | If MELCloud breaks, rate-limits us, or Mitsubishi forces migration to MELCloud Home. Reported working on this exact model. ~15 € hardware |
| **3. Manual adapter** | App reads nothing / reads via MELCloud only, and **sends a notification** with the recommended setpoint: *"Living area: set heat pump to 20 °C (expensive 17–21)"* | If no integration works. Rate-limited to meaningful changes, max ~3/day, quiet hours at night |

**Heat pump specifics**
- Modulating, not on/off. Small setpoint shifts only (±2 °C from preferred, ≤ 1 change per hour).
- **Setpoint range in heating is limited (≈ 16–31 °C; 10 °C only via "i-save" if the API exposes it)** — so the
  living area cannot follow Away 14 °C; it bottoms out at the device minimum.
- Default COP curve (refined from MELCloud energy data later):

  | Outdoor °C | +10 | +7 | +2 | −7 | −15 | −20 | −25 |
  |---|---|---|---|---|---|---|---|
  | COP (approx.) | 4.8 | 4.4 | 3.5 | 2.7 | 2.1 | 1.8 | 1.5 |

- **Summer**: user switches it off. If the app finds it powered off during heating season it does **not** turn it
  on — it marks the zone "off by user" and sends one notification.

### 3.5 Bathroom floor heating — deferred
Design hook only: `kind="floor"` zones, controlled by **floor** temperature, with their own modes (§5.3) and a
mold-prevention minimum. Thermostat model and integration chosen when bathrooms are built. Preference: a
thermostat with a **local** API and floor sensor — Zigbee (via a USB stick + Zigbee2MQTT, which can also serve
future room sensors) or Tuya WiFi via local protocol.

---

### 3.6 Independent temperature sensors (WiFi)

Why: the living area must have a reliable temperature even if the Mitsubishi integration fails (otherwise there is
no data to learn the masonry heater / cookstove effect), and the heat pump's own sensor sits near the ceiling in the
unit's airflow and is biased. Independent sensors also make every zone model better.
Bluetooth was ruled out (range through the house's walls).

**Decision: ESP32 + SHT4x sensor running ESPHome, USB-powered (~15–25 € per sensor).** Shelly and Zigbee below are kept as documented alternatives.
- No-solder option: M5Stack Atom Lite/AtomS3 Lite + ENV IV unit (SHT40 + pressure) on a Grove cable.
  DIY option: any ESP32 dev board + SHT40/SHT45 breakout.
- Flashed once from the browser (web.esphome.io). Reports every 30–60 s over WiFi; our app reads it with
  `aioesphomeapi` (same library as the Mitsubishi ESP32 fallback) — no broker, no cloud.
- USB-powered → always online, no batteries. Keep the sensor on its cable a few cm away from the ESP32 board
  (the board's own heat skews readings).
- Outdoor: ESP32 indoors (or in a box under the eaves) with a waterproof **DS18B20** probe through the wall /
  window frame, or an SHT4x in a small radiation shield.

**Alternative (no DIY): Shelly H&T Gen3 (~35 €, battery 4×AA, e-ink display).**
- WiFi, local. Sleeps between reports → sends on change (threshold configurable, e.g. 0.2 °C) and periodically;
  configured to call a **webhook on the Pi** (`POST /api/sensors/shelly`), so no MQTT broker needed.
- Indoor only. Coarser time resolution than ESPHome, but fine for zone models.

**Alternative (mesh): Zigbee sensors** — e.g. Sonoff SNZB-02D / Aqara (~10–15 €, coin/AAA battery, 1–2 years).
- Needs a coordinator: USB stick on the Pi (Sonoff ZBDongle-E/P, ~25 €) or a network coordinator (SLZB-06,
  Ethernet/PoE, ~40 €) placed centrally in the house. Software: Zigbee2MQTT + Mosquitto on the Pi
  (~150–250 MB RAM, OK on 1 GB); our app subscribes to MQTT.
- Mesh: every mains-powered Zigbee device (smart plug ~10 €, later the floor thermostats) is a router that
  extends range through thick walls. Battery sensors don't route.
- Best fit if the bathroom floor thermostats are Zigbee too — one local ecosystem for everything.

Not recommended: Tuya/Smart Life WiFi sensors (cloud-bound, local keys break with firmware updates).

`SensorAdapter` implementations: `EsphomeSensorAdapter`, `ShellyWebhookSensorAdapter`, `Zigbee2MqttSensorAdapter`. Each maps device → zone.

**Placement & priority**

| Sensor | Priority | Placement |
|---|---|---|
| Living area | **Buy now** | Interior wall, ~1.5 m high, away from masonry heater, cookstove, heat pump airflow, windows and direct sun |
| Outdoor | Recommended | Shaded north wall, under eaves — local outdoor temp beats the forecast for model fitting |
| Hall | Nice to have | Mid-hall — shows heat flow between kids' rooms |
| Kids' bedrooms | Later | Improves those models vs heater-built-in sensors |

**Zone temperature source** is a priority list per zone, e.g. living area = `[esphome:living, mitsubishi]`:
the planner/model uses the first available source; all sources are logged. Humidity is logged too (mold
monitoring, later bathroom drying logic).

If the Mitsubishi runs in Manual mode (§3.4 level 3), the living-area sensor still lets the app learn, plan, and
send meaningful setpoint instructions.

## 4. Electricity cost

### 4.1 Spot price — Elering
- `GET https://dashboard.elering.ee/api/nps/price?start=...&end=...` — EE, 15-min resolution.
- Next-day prices ~14:00 EET; fetch 14:15, retry every 15 min until complete; also at startup.
- **Slot = 60 min**: `price_hour = mean(4 quarter prices)` (hourly meter). Configurable for 15-min later.

### 4.2 Network tariff — Elektrilevi Võrk 4
- **Day**: Mon–Fri 07:00–22:00 (except public holidays). **Night**: other hours, weekends, holidays.
- Rates, excise, renewable fee, seller margin, VAT in config (time windows too). Estonian holidays computed.

```
elec_cost(slot) [€/kWh] = (spot/1000 + seller_margin + network(day|night) + excise + renewable_fee) × (1 + VAT)
```

### 4.3 Cost of heat

```
heat_cost(zone, slot) = elec_cost(slot) / COP(zone, T_out(slot))     # COP = 1 for resistive & floor
```

The heat pump is naturally favoured and prefers warmer hours (better COP).

### 4.4 Price tiers (per slot, on `elec_cost`, stricter of absolute/relative rule)

| Tier | Default rule | Effect |
|---|---|---|
| Very cheap | ≤ 5 c/kWh or bottom 10 % of horizon | Store heat up to `max` |
| Normal | — | Normal planning |
| Expensive | ≥ 25 c/kWh or top 15 % | Avoid; coast toward `min` |
| Extreme | ≥ 40 c/kWh | Flex mode (§5.5) where allowed + wood advice (§8) |

---

## 5. Comfort

### 5.1 Air modes

| Mode | Min | Preferred | Max | Used for |
|---|---|---|---|---|
| **Day** | 20 | 22 | 24 | Someone in the room / home and awake |
| **Night** | 18 | 19 | 21 | Sleeping |
| **Out** | 18 | 19 | 22 | Room temporarily empty during the day (kids at school) — short absences |
| **Away** | 14 | 15 | 18 | House empty for days (weekend trip, vacation) |

- `min` hard floor, `preferred` normal target, `max` ceiling for storing heat.
- Values are global defaults, overridable per zone. Device limits apply (living area ≥ 16 °C).

### 5.2 Schedules

**House schedule** (all zones unless overridden):

| Day type | Schedule |
|---|---|
| Weekday | 06:30 Day → 23:00 Night |
| Weekend & public holiday | 08:00 Day → 23:00 Night |

**Kids' rooms** — school days:

| | Default |
|---|---|
| Morning readiness | room at **Day min (20 °C)** by 06:30 — enough for the half hour before school |
| Out block | **07:00–13:00** default (per kid per weekday, editable in UI as the school timetable changes) |
| Return readiness | room at Day `preferred` (22 °C) by return time |
| School holidays | config list of date ranges (Estonian school holidays) → no Out block |
| "Home today" | one-tap UI override per kid for sick days / days off |

**Away (irregular)** — no regular Away schedule:
- UI **"Away from … until …"** for the house (weekends, vacations); house returns to schedule at the end
  and is pre-heated for arrival.
- Optional later: ICS calendar subscription (`Away: house` events).

### 5.3 Floor modes (for later bathroom zones)

| Mode | When (default) | Floor temp |
|---|---|---|
| Comfort | wake time − 45 min → wake + 1.5 h; 21:00 → 23:30 | warm (e.g. 26 °C, TBD) |
| Eco | rest of the day/night | cooler (e.g. 21 °C, TBD) |
| Away | house Away | Eco minimum |

Hard **mold-prevention minimum** (floor ≥ ~20 °C, TBD) always applies — never lower even during Extreme prices.
Floors have high thermal mass, so Comfort periods are pre-heated in cheap hours. Optional later: humidity sensor
→ drying boost after showers.

### 5.4 Readiness targets (pre-heating)
A mode's `preferred` must already be reached when the mode starts (exception: kids' rooms on school-day mornings → Day min 20 °C): wake-up (06:30 / 08:00), kids' return from
school, arrival after Away, floor Comfort start. The planner back-simulates from each target with the learned
model and picks the cheapest-heat hours. Day → Night needs no pre-heat (coast down).

### 5.5 Flex mode (extreme prices)

| Zone | Flex allowed |
|---|---|
| Living area | Day min 20 → 18.5, Night 18 → 17 (device min 16 still applies) |
| Madli's / Matis's bedrooms | **No flex** — bands stay fixed |
| Bathroom floors | No flex below mold-prevention minimum |

Max 6 h continuous; readiness targets still apply afterwards; notification + dashboard banner; "Flex off today"
button. Usually a lit masonry heater makes flex unnecessary in the living area (§8).

### 5.6 Ad-hoc controls (UI)
Away from/until (house), Home today (kid), Boost (zone, N h), Hold (until cancelled), Flex off today, fire logged.
Precedence: Hold > Boost > Away > Home-today > Flex > Plan > Schedule fallback. All expire and are visible.

---

## 6. Logging & data

| Table | Key columns |
|---|---|
| `zones` | id, name, kind, adapter config, schedule refs, mode overrides, flex rules |
| `readings` / `readings_15m` | ts, zone_id, air/floor temp, setpoint, power_on, is_heating, power_w, energy_kwh |
| `prices` | ts_start, resolution_min, eur_mwh |
| `tariffs` | valid_from, components |
| `weather` | ts, issued_at, temp, wind, radiation, cloud |
| `fires` | id, stove (masonry / cookstove), lit_at, loads (default 2), note |
| `models` | zone_id, fitted_at, params JSON, rmse, n_samples, confidence |
| `plans` | created_at, zone_id, slot_start, mode, tier, setpoint, predicted_temp, kwh, cost |
| `overrides` | id, scope, type, value, start, end |
| `notifications` | ts, type, message, delivered |
| `events` | ts, level, source, message |

Polling: local devices 1–2 min, cloud devices 5 min. Weather + price hourly.

---

## 7. Thermal model (per zone)

15-min steps:

```
T[k+1] = T[k] + Δt · ( a·u_eff[k]  −  b·(T[k] − T_out[k])  +  c·solar[k]  +  Σ_f w_f·fire_f(k) )
```

| Param | Meaning |
|---|---|
| `u` | heating effort 0..1 (on-fraction for resistive; power/max for heat pump, estimated if unknown) |
| `a` | heating rate at full power (°C/h) |
| `b` | loss coefficient (1/h); τ = 1/b |
| `c` | solar gain |
| `u_eff` | `u` through lag `τ_lag` — Mill oil radiator and floors; 0 for Adax and heat pump |
| `fire_f(k)` | release kernel of stove f after lighting (rise 1–2 h, decay τ_fire ≈ 8–16 h for masonry, shorter for cookstove) |
| `w_f` | effect of stove f on this zone (large for living area, ~0 for bedrooms — learned, not assumed) |

- Rolling 14-day least squares, nightly refit; staged: `b,c` from idle no-fire periods → `a, τ_lag` → fire params.
- Outlier rejection (windows, doors, cooking, showers, first minutes after setpoint change).
- Confidence score; low confidence → conservative defaults (pre-heat earlier).
- Kids' rooms models implicitly include heat exchange with the unheated hall.
- Built-in sensors are biased (Mitsubishi near ceiling, radiators near the heater); models learn in "device-sensor"
  units, consistent with what the thermostats regulate. Refit against external sensors if added.

**Learned fire value**: comparing heat pump energy (MELCloud energy data) on fire vs no-fire days at similar outdoor
temperatures gives the **useful electric kWh a fire actually displaces** — this replaces the theoretical wood
estimate in §8 once there's enough data.

---

## 8. Wood heating advisor

### 8.1 Economics

| | Value |
|---|---|
| Wood price | 80 € per stacked m³ (laotud ruumimeeter) |
| Per firing | 2 loads ≈ 0.048 stacked m³ → **≈ 3.84 €** |
| Fuel energy | stacked m³ ≈ 1 300 (alder) – 1 700 (birch) kWh at ~20 % moisture → ≈ 62–82 kWh per firing (default 72) |
| Useful heat per firing | masonry heater ~80 % efficiency → **≈ 50–65 kWh** (default 58); cookstove ~65 % → ≈ 47 kWh |
| Wood heat cost | **≈ 6–8 c/kWh of heat** (masonry ≈ 6.6, cookstove ≈ 8.2) — defaults in config, replaced by learned values |

Compared to the heat pump in the living area: heat costs `elec_cost / COP`. Example at −7 °C (COP ≈ 2.7):
electricity at 20 c/kWh all-in → 7.4 c/kWh heat (≈ wood); at 40 c/kWh → 14.8 c/kWh heat (wood clearly cheaper).
So **wood mostly wins when prices are high and/or it's very cold** — and a fire only saves what the heat pump
would actually have used in that period (excess heat that overheats the room is not a saving).

### 8.2 Logic
1. After prices arrive (~14:15) and each morning (configurable, e.g. 06:00): simulate the living area
   - electricity only, vs
   - fire lit at candidate times (e.g. 06:00, 15:00, 18:00), using learned fire effect.
2. `saving = displaced electricity cost − 3.84 € − effort_margin` (effort margin default 1 €, configurable).
3. If saving > 0 → notification with best time and estimated saving. Also always notify when an Extreme period
   coincides with outdoor < 0 °C.
4. When a fire is logged, the planner replans and lowers the heat pump setpoint for the fire's effect period.
5. Savings page reports actual wood vs electricity cost per month.

Example:
> 🔥 Tomorrow 07–11 and 16–21: 48 c/kWh, −14 °C. Light the masonry heater by ~15:00 — est. saving 2.10 €.

---

## 9. Notifications

**ntfy** (free, open source):
- Both your and your wife's phones install the ntfy app and subscribe to the same **private topic** (long random
  name, or self-hosted / access token) → simultaneous delivery to both.
- Priorities: wood advice & flex = default, device offline / temp below min = high, heat pump manual
  instructions = default.
- Action buttons (e.g. "Masonry lit") call the Pi's API — works on home WiFi; from outside only with Tailscale.
- Quiet hours (23:00–06:30) except high-priority alerts.

Notification types: wood advice, flex activated, device offline > 30 min, zone below hard min, heat pump found off
in heating season, manual heat pump instruction (if Level 3), prices missing by 16:00, model confidence dropped.

---

## 10. Planner

**Inputs**: hourly heat costs (§4.3) over the known horizon (10–34 h), weather forecast, zone models + current
temps, bands per slot (schedule + overrides + readiness + flex), logged fires.

**Output per zone per slot**: setpoint (+ predicted temp, kWh, cost, tier). Lowest setpoint ever sent = current
(possibly flexed) `min`, clamped to device range.

**v1 — heuristic (MVP)**
1. Readiness targets: back-simulate latest feasible start at full power + margin → pre-heat window; choose
   cheapest-heat hours until target reached in time.
2. Other slots: Very cheap → `max`; Expensive → `min` if simulation stays ≥ min; Extreme → flexed `min` where
   allowed; otherwise `preferred`.
3. Forward-simulate, repair any `min` violation in the cheapest earlier slots, repeat.
4. Heat pump constraints (§3.4).

**v2 — dynamic programming**: state = temperature (0.1 °C), actions = candidate setpoints, cost = kWh × heat cost +
comfort penalty + hard penalties. Per zone, < 1 s on the Pi 3. Zones independent (no fuse limit).

**Replan**: new prices, hourly, schedule/override change, fire logged, deviation > 0.7 °C.

---

## 11. Executor & safety

- Every 5 min: apply slot setpoint per zone **only if changed**; read back to confirm.
- Never power devices on/off. Device found off → skip zone, notify once.
- Fallbacks: no prices → schedule `preferred`; low-confidence model → conservative; device offline → device
  keeps last setpoint (always ≥ some mode min) + notify; measured < min − 0.5 → force `preferred`.
- Graceful shutdown → every zone to schedule `preferred`.
- Absolute limits: air 10–26 °C (and device range); floor per material limit.
- **Season**: Heating (active) / Off (log only). Suggests switching from the 7-day forecast.

---

## 12. Local UI

`http://<pi>:8000`, LAN only in v1.

1. **Dashboard** — zones (temp, setpoint, mode, state), price/tier/tariff now, overrides, wood advice banner,
   buttons: Masonry lit / Cookstove lit / Away / Home today.
2. **Plan** — next 24–34 h prices by tier + per-zone predicted temps vs bands.
3. **History** — temps, outdoor, heating, prices, fires.
4. **Zones** — learned params, confidence, predicted vs actual, pre-heat calculator, fire effect.
5. **Schedules** — house schedule, kids' school times per weekday, school holidays, modes, flex rules.
6. **Settings** — devices, tariffs, wood price, location, notifications, season, slot length.
7. **Savings** — vs fixed-schedule baseline; wood vs electricity.

---

## 13. Scheduled jobs

| Job | Interval |
|---|---|
| Poll + log | 1–2 min local, 5 min cloud |
| Executor | 5 min |
| Weather | hourly |
| Prices | 14:15 + retries; startup |
| Replan | hourly + events |
| Wood advisor | after prices + morning |
| Fit models | nightly 03:00 |
| Downsample / prune | nightly |

---

## 14. Roadmap

| Phase | Deliverable |
|---|---|
| **0. Discovery** | Read & set Adax (cloud; check local), Mill (local), Mitsubishi (MELCloud). Confirm setpoint ranges and what each reports (power/energy). Buy + flash WiFi sensors (living area, outdoor), read them from the Pi. |
| **1. Observe** | Config, DB, adapters, logger, prices, weather, tariffs, read-only dashboard + history, fire buttons, ntfy test. Run 1–2 weeks on the Pi. |
| **2. Learn** | Thermal models incl. lag and fire effect; zone insights. |
| **3. Control (MVP)** | Modes, schedules (house, kids, away), tiers, heuristic planner, executor, overrides, safety, notifications. |
| **4. Optimise** | Wood advisor, flex mode, DP planner, savings, COP refinement from energy data. |
| **5. Extend** | Bathroom floor zones, room/hall sensors, ICS calendar, ESP32 for Mitsubishi if needed, 15-min slots, Tailscale. |

---

## 15. Open questions

None blocking. Decided later:
- Bathroom floor heating: floor temps, wattage, thermostat model — at build time.
- Kids' per-weekday school times — set in the UI as timetables change.
- Wood type (birch vs alder) — only refines the default until fire value is learned from data.
