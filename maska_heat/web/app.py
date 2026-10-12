"""FastAPI app: dashboard, control (hands-off / manual / smart), settings, history, fire logging and a JSON API."""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import ValidationError
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import __version__
from ..collector import Collector
from ..control import COMFORT_MODES, SETPOINT_RANGE, Away, Band, Comfort, ControlStore, DateRange, HouseSchedule, KidSchool, SchoolDay
from ..dashboard import as_dict, history, upcoming_prices, zone_views
from ..db import Database
from ..executor import Executor, describe_mode
from ..notify import Notifier
from ..schedule import MODE_LABEL
from ..settings import Settings

HERE = Path(__file__).parent
STOVES = {"masonry": "Masonry heater", "cookstove": "Cookstove"}
FIRE_DAYS = 7
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def create_app(settings: Settings, secrets: dict[str, Any], start_jobs: bool = True, db: Database | None = None) -> FastAPI:
    tz = ZoneInfo(settings.location.timezone)
    templates = Jinja2Templates(directory=str(HERE / "templates"))

    def localtime(ts: int | float | None, fmt: str = "%H:%M") -> str:
        return "—" if ts is None else datetime.fromtimestamp(ts, tz).strftime(fmt)

    def age(ts: int | float | None) -> str:
        if ts is None:
            return "never"
        s = int(time.time() - ts)
        if s < 90:
            return "just now"
        if s < 5400:
            return f"{s // 60} min ago"
        if s < 172800:
            return f"{s // 3600} h ago"
        return f"{s // 86400} d ago"

    templates.env.filters["localtime"] = localtime
    templates.env.filters["age"] = age
    templates.env.filters["cents"] = lambda eur_kwh: "—" if eur_kwh is None else f"{eur_kwh * 100:.1f}"
    templates.env.filters["hhmm"] = lambda t: "" if t is None else t.strftime("%H:%M")
    templates.env.globals["mode_name"] = describe_mode
    templates.env.globals["comfort_label"] = MODE_LABEL
    # Cache-busting token for static files: changes whenever a static file changes
    templates.env.globals["static_v"] = int(max(f.stat().st_mtime for f in (HERE / "static").iterdir()))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        own_db = db is None
        app.state.db = db or Database(settings.database_path)
        topic = (secrets.get("ntfy") or {}).get("topic")
        app.state.notifier = Notifier(settings.notify, topic, app.state.db, tz)
        app.state.collector = Collector(settings, secrets, app.state.db, app.state.notifier)
        kid_zones = [k for k, z in settings.zones.items() if z.school]
        app.state.store = ControlStore(app.state.db, kid_zones)
        app.state.executor = Executor(settings, app.state.db, app.state.store, app.state.collector.devices)
        if start_jobs:
            app.state.collector.start()
            app.state.executor.start()
        yield
        await app.state.executor.stop()
        await app.state.collector.stop()
        await app.state.notifier.aclose()
        if own_db:
            app.state.db.close()

    app = FastAPI(title="maska_heat", version=__version__, lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    def _db(request: Request) -> Database:
        return request.app.state.db

    def _store(request: Request) -> ControlStore:
        return request.app.state.store

    def _executor(request: Request) -> Executor:
        return request.app.state.executor

    def _apply(request: Request, zone_key: str | None = None) -> None:
        if start_jobs:
            _executor(request).trigger(zone_key)

    def decisions(request: Request) -> dict:
        ex = _executor(request)
        snap = ex.snapshot()
        return {k: ex.decide(k, snap=snap) for k in settings.zones}

    def status_context(request: Request) -> dict:
        now = time.time()
        db = _db(request)
        prices = upcoming_prices(settings, db, now)
        weather = db.weather_at(int(now))
        outdoor = None
        if settings.outdoor_sensor:
            row = db.latest_reading(f"sensor:{settings.outdoor_sensor}")
            if row and now - row["ts"] < 600:
                outdoor = {"temp": row["temp"], "source": "sensor"}
        if outdoor is None and weather:
            outdoor = {"temp": weather["temp"], "source": "forecast"}
        return {
            "request": request,
            "zones": zone_views(settings, db, now),
            "price_now": next((p for p in prices if p.is_now), None),
            "prices": prices,
            "outdoor": outdoor,
            "weather": weather,
            "tariff_configured": settings.tariff.configured,
            "decisions": decisions(request),
            "now_ts": now,
            "away": _store(request).away(),
        }

    def fires_context(request: Request) -> dict:
        return {
            "request": request,
            "fires": _db(request).fires(int(time.time()) - FIRE_DAYS * 86400),
            "stoves": STOVES,
        }

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        ctx = status_context(request) | fires_context(request)
        ctx["events"] = _db(request).events(15)
        ctx["prices_json"] = [as_dict(p) for p in ctx["prices"]]
        ctx["notify_available"] = request.app.state.notifier.available
        return templates.TemplateResponse(request, "dashboard.html", ctx)

    @app.get("/partials/status", response_class=HTMLResponse)
    async def status_partial(request: Request):
        return templates.TemplateResponse(request, "_status.html", status_context(request))

    @app.get("/history", response_class=HTMLResponse)
    async def history_page(request: Request, zone: str | None = None, hours: int = 48):
        zone = zone or next(iter(settings.zones), None)
        return templates.TemplateResponse(
            request, "history.html", {"request": request, "zones": settings.zones, "zone": zone, "hours": hours}
        )

    @app.post("/fires", response_class=HTMLResponse)
    async def add_fire(request: Request, stove: str = Form(...), minutes_ago: int = Form(0)):
        if stove not in STOVES:
            raise HTTPException(400, "unknown stove")
        db = _db(request)
        db.add_fire(stove, int(time.time()) - max(0, minutes_ago) * 60, loads=2)
        db.add_event("info", "fire", f"{STOVES[stove]} lit" + (f" {minutes_ago} min ago" if minutes_ago else ""))
        return templates.TemplateResponse(request, "_fires.html", fires_context(request))

    @app.post("/fires/{fire_id}/delete", response_class=HTMLResponse)
    async def delete_fire(request: Request, fire_id: int):
        _db(request).delete_fire(fire_id)
        return templates.TemplateResponse(request, "_fires.html", fires_context(request))

    @app.post("/notify/test", response_class=HTMLResponse)
    async def notify_test(request: Request):
        notifier: Notifier = request.app.state.notifier
        if not notifier.available:
            return HTMLResponse('<span class="muted">ntfy topic not configured in secrets.yaml</span>')
        ok = await notifier.send("test", "maska_heat test", "Notifications work 👍", priority="high", tags="fire")
        return HTMLResponse("<span>Sent ✓</span>" if ok else '<span class="bad">Sending failed — see events</span>')

    # control ------------------------------------------------------------------

    def zone_card_context(request: Request, zone_key: str) -> dict:
        views = {v.key: v for v in zone_views(settings, _db(request), time.time())}
        dev = settings.zones[zone_key].device
        return {
            "request": request,
            "key": zone_key,
            "zone": settings.zones[zone_key],
            "view": views[zone_key],
            "ctl": _store(request).zone(zone_key),
            "decision": _executor(request).decide(zone_key),
            "controllable": bool(dev) and dev in request.app.state.collector.devices,
        }

    def control_context(request: Request, error: str | None = None) -> dict:
        store = _store(request)
        school = store.school()
        today = datetime.now(tz).date()
        return {
            "request": request,
            "cards": [zone_card_context(request, k) for k in settings.zones],
            "away": store.away(),
            "kids": [k for k, kid in school.zones.items() if kid.enabled and k in settings.zones],
            "home_today": {k for k, d in store.home_today().items() if d == today},
            "zones": settings.zones,
            "now_local": datetime.now(tz),
            "error": error,
        }

    def _zone_or_404(zone_key: str) -> None:
        if zone_key not in settings.zones:
            raise HTTPException(404, "unknown zone")

    def _set_mode(request: Request, zone_key: str, mode: str) -> None:
        if mode not in ("handsoff", "manual", "smart"):
            raise HTTPException(400, "unknown mode")
        store = _store(request)
        ctl = store.zone(zone_key)
        if ctl.mode == mode:
            return
        if mode == "manual" and ctl.manual_setpoint is None:
            latest = _db(request).latest_reading(f"device:{settings.zones[zone_key].device}")
            has_sp = latest is not None and latest["setpoint"] is not None
            ctl.manual_setpoint = latest["setpoint"] if has_sp else store.comfort().day.preferred
        ctl.mode = mode
        store.set_zone(zone_key, ctl)
        _db(request).add_event("info", f"control:{zone_key}", f"{settings.zones[zone_key].name}: mode → {describe_mode(mode)}")

    @app.get("/control", response_class=HTMLResponse)
    async def control_page(request: Request, error: str | None = None):
        return templates.TemplateResponse(request, "control.html", control_context(request, error))

    @app.post("/control/all")
    async def control_all(request: Request, mode: str = Form(...)):
        for k in settings.zones:
            _set_mode(request, k, mode)
        _apply(request)
        return Response(headers={"HX-Refresh": "true"})

    @app.post("/control/{zone_key}/mode", response_class=HTMLResponse)
    async def control_mode(request: Request, zone_key: str, mode: str = Form(...)):
        _zone_or_404(zone_key)
        _set_mode(request, zone_key, mode)
        _apply(request, zone_key)
        return templates.TemplateResponse(request, "_control_zone.html", zone_card_context(request, zone_key))

    @app.post("/control/{zone_key}/setpoint", response_class=HTMLResponse)
    async def control_setpoint(
        request: Request, zone_key: str, setpoint: float | None = Form(None), delta: float | None = Form(None)
    ):
        _zone_or_404(zone_key)
        store = _store(request)
        ctl = store.zone(zone_key)
        base = ctl.manual_setpoint if ctl.manual_setpoint is not None else store.comfort().day.preferred
        value = base + delta if delta is not None else setpoint
        if value is None:
            raise HTTPException(400, "setpoint missing")
        lo, hi = SETPOINT_RANGE
        ctl.manual_setpoint = round(min(max(value, lo), hi) * 2) / 2
        store.set_zone(zone_key, ctl)
        _apply(request, zone_key)
        return templates.TemplateResponse(request, "_control_zone.html", zone_card_context(request, zone_key))

    @app.post("/away")
    async def set_away(request: Request, start: str = Form(""), end: str = Form(...)):
        try:
            end_dt = datetime.fromisoformat(end).replace(tzinfo=tz)
            start_dt = datetime.fromisoformat(start).replace(tzinfo=tz) if start else datetime.now(tz)
        except ValueError:
            return RedirectResponse("/control?error=Invalid+date", status_code=303)
        if end_dt <= start_dt:
            return RedirectResponse("/control?error=Away+must+end+after+it+starts", status_code=303)
        _store(request).set_away(Away(start=start_dt, end=end_dt))
        _db(request).add_event("info", "control", f"Away {start_dt:%a %d.%m %H:%M} → {end_dt:%a %d.%m %H:%M}")
        _apply(request)
        return RedirectResponse("/control", status_code=303)

    @app.post("/away/cancel")
    async def cancel_away(request: Request):
        _store(request).set_away(Away())
        _db(request).add_event("info", "control", "Away cancelled")
        _apply(request)
        return RedirectResponse("/control", status_code=303)

    @app.post("/home-today/{zone_key}")
    async def home_today(request: Request, zone_key: str, on: int = Form(...)):
        _zone_or_404(zone_key)
        _store(request).set_home_today(zone_key, datetime.now(tz).date() if on else None)
        _apply(request, zone_key)
        return RedirectResponse("/control", status_code=303)

    # settings -----------------------------------------------------------------

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request, saved: str | None = None, error: str | None = None):
        store = _store(request)
        return templates.TemplateResponse(
            request,
            "settings.html",
            {
                "request": request,
                "comfort": store.comfort(),
                "modes": COMFORT_MODES,
                "house": store.house(),
                "school": store.school(),
                "zones": settings.zones,
                "weekdays": WEEKDAYS,
                "saved": saved,
                "error": error,
            },
        )

    def _back(saved: str | None = None, error: str | None = None) -> RedirectResponse:
        q = urlencode({k: v for k, v in (("saved", saved), ("error", error)) if v})
        return RedirectResponse(f"/settings?{q}" if q else "/settings", status_code=303)

    def _err(e: Exception) -> str:
        if isinstance(e, ValidationError):
            return "; ".join(err["msg"].removeprefix("Value error, ") for err in e.errors())
        return str(e)

    @app.post("/settings/comfort")
    async def save_comfort(request: Request):
        form = await request.form()
        try:
            comfort = Comfort(**{
                m: Band(min=float(form[f"{m}_min"]), preferred=float(form[f"{m}_preferred"]), max=float(form[f"{m}_max"]))
                for m in COMFORT_MODES
            })
        except (KeyError, ValueError, ValidationError) as e:
            return _back(error=f"Comfort ranges not saved: {_err(e)}")
        _store(request).set_comfort(comfort)
        _db(request).add_event("info", "settings", "Comfort ranges updated")
        _apply(request)
        return _back(saved="comfort")

    @app.post("/settings/house")
    async def save_house(
        request: Request, weekday_wake: str = Form(...), weekend_wake: str = Form(...), night_start: str = Form(...)
    ):
        try:
            house = HouseSchedule(weekday_wake=weekday_wake, weekend_wake=weekend_wake, night_start=night_start)
        except ValidationError as e:
            return _back(error=f"Schedule not saved: {_err(e)}")
        _store(request).set_house(house)
        _db(request).add_event("info", "settings", "House schedule updated")
        _apply(request)
        return _back(saved="house")

    @app.post("/settings/school")
    async def save_school(request: Request):
        form = await request.form()
        store = _store(request)
        school = store.school()
        try:
            zones = {}
            for k in settings.zones:
                days = {
                    d: SchoolDay(leave=form.get(f"{k}_{d}_leave") or None, back=form.get(f"{k}_{d}_back") or None)
                    for d in range(5)
                }
                zones[k] = KidSchool(enabled=form.get(f"{k}_enabled") == "on", days=days)
            school.zones = zones
            school.morning_target = "min" if form.get("morning_target") == "min" else "preferred"
        except ValidationError as e:
            return _back(error=f"School times not saved: {_err(e)}")
        store.set_school(school)
        _db(request).add_event("info", "settings", "School timetable updated")
        _apply(request)
        return _back(saved="school")

    @app.post("/settings/holidays")
    async def add_holiday(request: Request, start: date = Form(...), end: date = Form(...)):
        store = _store(request)
        school = store.school()
        try:
            school.holidays = sorted([*school.holidays, DateRange(start=start, end=end)], key=lambda r: r.start)
        except ValidationError as e:
            return _back(error=f"Holiday not added: {_err(e)}")
        store.set_school(school)
        _apply(request)
        return _back(saved="school")

    @app.post("/settings/holidays/delete")
    async def delete_holiday(request: Request, index: int = Form(...)):
        store = _store(request)
        school = store.school()
        if 0 <= index < len(school.holidays):
            del school.holidays[index]
            store.set_school(school)
            _apply(request)
        return _back(saved="school")

    # JSON API -----------------------------------------------------------------

    @app.get("/api/status")
    async def api_status(request: Request):
        ctx = status_context(request)
        return {
            "zones": [as_dict(z) for z in ctx["zones"]],
            "price_now": as_dict(ctx["price_now"]) if ctx["price_now"] else None,
            "outdoor": ctx["outdoor"],
        }

    @app.get("/api/prices")
    async def api_prices(request: Request):
        return [as_dict(p) for p in upcoming_prices(settings, _db(request), time.time())]

    @app.get("/api/history")
    async def api_history(request: Request, zone: str, hours: int = 48):
        if zone not in settings.zones:
            raise HTTPException(404, "unknown zone")
        return JSONResponse(history(settings, _db(request), zone, min(max(hours, 1), 24 * 31), time.time()))

    @app.get("/healthz")
    async def healthz(request: Request):
        collector: Collector = request.app.state.collector
        return {
            "version": __version__,
            "sources": {s: {"failing": h.failing, "last_ok": int(h.last_ok), "error": h.last_error} for s, h in collector.health.items()},
        }

    return app
