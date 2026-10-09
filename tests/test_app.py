import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from maska_heat.collector import Collector
from maska_heat.dashboard import upcoming_prices, zone_views
from maska_heat.db import Reading
from maska_heat.notify import Notifier
from maska_heat.settings import Settings
from maska_heat.web.app import create_app

TZ = ZoneInfo("Europe/Tallinn")


def test_settings_reject_unknown_source(settings):
    raw = settings.model_dump(mode="json")
    raw["zones"]["living"]["temp_sources"] = ["sensor:nope"]
    with pytest.raises(ValueError, match="sensor:nope"):
        Settings.model_validate(raw)


def test_zone_defaults_to_device_source(settings):
    assert settings.zones["matis"].temp_sources == ["device:matis_mill"]


def test_zone_view_prefers_fresh_sensor_then_device(settings, db):
    now = time.time()
    db.add_readings(
        [
            Reading(int(now) - 30, "device:living_mitsubishi", temp=23.0, setpoint=21.0, power_on=True),
            Reading(int(now) - 30, "sensor:living", temp=21.4, humidity=40.0),
        ]
    )
    living = next(z for z in zone_views(settings, db, now) if z.key == "living")
    assert (living.temp, living.temp_source, living.setpoint, living.stale) == (21.4, "sensor:living", 21.0, False)

    later = now + 600  # sensor stale after 5 min, device still fresh (3 × 300 s)
    living = next(z for z in zone_views(settings, db, later) if z.key == "living")
    assert (living.temp, living.temp_source) == (23.0, "device:living_mitsubishi")


def test_zone_view_marks_stale_device(settings, db):
    now = time.time()
    db.add_readings([Reading(int(now) - 3600, "device:matis_mill", temp=20.0, setpoint=10.0, is_heating=False)])
    matis = next(z for z in zone_views(settings, db, now) if z.key == "matis")
    assert matis.stale and matis.temp == 20.0 and matis.device_online is False


def test_upcoming_prices_hourly_with_cost(settings, db):
    start = datetime(2026, 10, 9, 12, 0, tzinfo=TZ)  # Friday, day tariff
    db.upsert_prices([(int((start + timedelta(minutes=15 * i)).timestamp()), 15, 100.0 + i) for i in range(8)])
    now = (start + timedelta(minutes=20)).timestamp()
    slots = upcoming_prices(settings, db, now)
    assert [s.eur_mwh for s in slots] == [101.5, 105.5]
    assert slots[0].is_now and not slots[1].is_now
    assert slots[0].cost_total == pytest.approx((0.1015 + 0.06) * 1.24, abs=1e-5)


async def test_collector_logs_device_and_tracks_health(settings, db):
    notifier = Notifier(settings.notify, None, db, TZ)
    secrets = {"adax": {"account_id": "1", "api_secret": "s"}}
    c = Collector(settings, secrets, db, notifier)
    assert set(c.devices) == {"matis_mill", "madli_adax"}  # mitsubishi disabled

    class Broken:
        async def read(self):
            raise RuntimeError("no route to host")

    c.devices["matis_mill"] = Broken()
    await c.poll_device("matis_mill")
    h = c.health["device:matis_mill"]
    assert h.failing and "no route" in h.last_error
    assert db.events(1)[0]["level"] == "warning"

    h.last_ok -= 31 * 60
    await c.check_health()
    assert h.alerted and db.events(1)[0]["level"] == "error"


def test_collector_skips_adax_without_secrets(settings, db):
    c = Collector(settings, {}, db, Notifier(settings.notify, None, db, TZ))
    assert "madli_adax" not in c.devices
    assert "secrets.yaml" in db.events(1)[0]["message"]


def test_web_pages_and_fire_logging(settings, db):
    app = create_app(settings, {}, start_jobs=False, db=db)
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert client.get("/history").status_code == 200
        assert client.get("/partials/status").status_code == 200

        r = client.post("/fires", data={"stove": "masonry", "minutes_ago": "60"})
        assert r.status_code == 200 and "Masonry heater" in r.text
        fire = db.fires(0)[0]
        assert abs(fire["lit_at"] - (time.time() - 3600)) < 5

        assert client.post("/fires", data={"stove": "sauna"}).status_code == 400
        client.post(f"/fires/{fire['id']}/delete")
        assert db.fires(0) == []

        hist = client.get("/api/history", params={"zone": "matis", "hours": 24}).json()
        assert hist["name"] == "Matis's bedroom" and "device:matis_mill" in hist["series"]
        assert client.get("/api/history", params={"zone": "nope"}).status_code == 404
        assert "ntfy topic not configured" in client.post("/notify/test").text
