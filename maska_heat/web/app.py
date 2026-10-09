"""FastAPI app: read-only dashboard, history, fire logging and a JSON API."""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import __version__
from ..collector import Collector
from ..dashboard import as_dict, history, upcoming_prices, zone_views
from ..db import Database
from ..notify import Notifier
from ..settings import Settings

HERE = Path(__file__).parent
STOVES = {"masonry": "Masonry heater", "cookstove": "Cookstove"}
FIRE_DAYS = 7


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
    # Cache-busting token for static files: changes whenever a static file changes
    templates.env.globals["static_v"] = int(max(f.stat().st_mtime for f in (HERE / "static").iterdir()))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        own_db = db is None
        app.state.db = db or Database(settings.database_path)
        topic = (secrets.get("ntfy") or {}).get("topic")
        app.state.notifier = Notifier(settings.notify, topic, app.state.db, tz)
        app.state.collector = Collector(settings, secrets, app.state.db, app.state.notifier)
        if start_jobs:
            app.state.collector.start()
        yield
        await app.state.collector.stop()
        await app.state.notifier.aclose()
        if own_db:
            app.state.db.close()

    app = FastAPI(title="maska_heat", version=__version__, lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    def _db(request: Request) -> Database:
        return request.app.state.db

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
