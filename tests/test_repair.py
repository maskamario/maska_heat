from datetime import datetime
from zoneinfo import ZoneInfo

from maska_heat.db import Reading
from maska_heat.energy import power_hours
from maska_heat.repair import fix_hot_water
from maska_heat.settings import Settings

TZ = ZoneInfo("Europe/Tallinn")
SRC = "device:water_heater"
ON_SCHEDULE = "Automatic, heats on schedule"


def with_boiler(settings: Settings) -> Settings:
    raw = settings.model_dump(mode="json")
    raw["devices"]["water_heater"] = {"type": "cozytouch", "power_w": 2250}
    raw["zones"]["hot_water"] = {"name": "Hot water", "device": "water_heater"}
    raw["hot_water"] = {}
    return Settings.model_validate(raw)


def ts(day: int, hour: int, minute: int = 0) -> int:
    return int(datetime(2026, 11, day, hour, minute, tzinfo=TZ).timestamp())


def log(db, day, start, end, temp_from, temp_to, power=2250.0, mode=ON_SCHEDULE, heating=True):
    """Readings every 10 minutes from start to end (hours, end exclusive), temperature moving linearly."""
    times = list(range(ts(day, 0) + int(start * 3600), ts(day, 0) + int(end * 3600), 600))
    db.add_readings(
        [
            Reading(t, SRC, temp=temp_from + (temp_to - temp_from) * i / max(len(times) - 1, 1), setpoint=70, power_on=True,
                    is_heating=heating, power_w=power, mode=mode)
            for i, t in enumerate(times)
        ]
    )


def day_11(db):
    """11.11.2026 as logged: slots 02:00–07:00 and 18:00–19:00; the tank asks for heat from 12:50."""
    db.add_event("info", "control:hot_water", "Hot water: Wed 11.11 heats 02:00–07:00, 18:00–19:00", ts=ts(10, 14))
    log(db, 11, 0, 2, 66.9, 65.9, power=0.0, heating=False)
    log(db, 11, 2, 2.5, 65.9, 72.9)  # real: inside the night slot
    log(db, 11, 2.5, 12.5, 72.9, 65.7, power=0.0, heating=False)
    log(db, 11, 12.5 + 1 / 3, 18, 65.7, 62.5)  # phantom: waits for 18:00, the water keeps cooling
    log(db, 11, 18, 18.25, 62.5, 64.0)  # real, rated power assumed
    log(db, 11, 18.25, 19, 64.0, 73.0, power=1000.0)  # real, power as reported
    log(db, 11, 19, 24, 73.0, 71.0, power=0.0, heating=False)


def kwh(db, start, end):
    return sum(power_hours(db, SRC, start, end, 600).values())


def test_dry_run_lists_the_phantom_stretch_and_changes_nothing(settings, db):
    s = with_boiler(settings)
    day_11(db)
    before = kwh(db, ts(11, 0), ts(12, 0))
    report = fix_hot_water(s, db, ts(9, 0), ts(12, 0))
    assert not report.applied and kwh(db, ts(11, 0), ts(12, 0)) == before
    [stretch] = report.stretches
    assert stretch.corrected and (stretch.start, stretch.end) == (ts(11, 12, 50), ts(11, 18))
    assert stretch.rows == 31 and round(stretch.kwh, 1) == 11.6
    text = report.text(TZ)
    assert "Wed 11.11 12:50" in text and "Would correct 31 readings" in text and "--apply" in text


def test_apply_zeroes_only_the_phantom_readings(settings, db):
    s = with_boiler(settings)
    day_11(db)
    before = kwh(db, ts(11, 0), ts(12, 0))
    report = fix_hot_water(s, db, ts(9, 0), ts(12, 0), apply=True)
    assert report.applied and "Corrected 31 readings" in report.text(TZ)
    after = kwh(db, ts(11, 0), ts(12, 0))
    assert round(before - after, 1) == 11.6
    assert kwh(db, ts(11, 12), ts(11, 18)) == 0  # nothing left in the peak hours
    assert round(kwh(db, ts(11, 2), ts(11, 3)), 2) == round(2.25 * 0.5, 2)  # the night slot is untouched
    assert kwh(db, ts(11, 18), ts(11, 19)) > 1.2  # and so is the evening slot, reported power included
    assert "corrected 31 readings" in db.events(1)[0]["message"]
    assert fix_hot_water(s, db, ts(9, 0), ts(12, 0), apply=True).rows == 0  # running it again finds nothing


def test_real_heating_outside_the_slots_is_kept(settings, db):
    s = with_boiler(settings)
    db.add_event("info", "control:hot_water", "Hot water: Tue 10.11 heats 03:00–07:00, 13:00–14:00", ts=ts(9, 14))
    log(db, 10, 9, 10, 45.0, 60.0)  # a boost: the water gets warmer
    report = fix_hot_water(s, db, ts(9, 0), ts(12, 0), apply=True)
    [stretch] = report.stretches
    assert not stretch.corrected and report.rows == 0 and "kept" in report.text(TZ)
    assert round(kwh(db, ts(10, 9), ts(10, 10)), 2) == 2.25


def test_only_schedule_mode_rated_power_and_known_days_are_touched(settings, db):
    s = with_boiler(settings)
    db.add_event("info", "control:hot_water", "Hot water: Tue 10.11 heats 03:00–07:00, 13:00–14:00", ts=ts(9, 14))
    log(db, 10, 9, 10, 66.0, 65.0, mode="Automatic, heats any time")  # not on schedule: it could heat
    log(db, 10, 15, 16, 66.0, 65.0, power=1000.0)  # a power the boiler reported itself
    log(db, 9, 9, 10, 66.0, 65.0)  # no schedule event for 9.11
    report = fix_hot_water(s, db, ts(9, 0), ts(12, 0), apply=True)
    assert report.rows == 0 and report.days_without_schedule == {"Mon 09.11"}
    assert "Mon 09.11: no schedule in the event log" in report.text(TZ)


def test_a_schedule_rewritten_during_the_day_counts_from_then_on(settings, db):
    s = with_boiler(settings)
    db.add_event("info", "control:hot_water", "Hot water: Tue 10.11 heats 03:00–07:00", ts=ts(9, 14))
    db.add_event("info", "control:hot_water", "Hot water: Tue 10.11 heats 03:00–07:00, 11:00–12:00", ts=ts(10, 10, 55))
    log(db, 10, 10, 11, 66.0, 65.5)  # 10:00–10:50 outside the first plan → phantom
    log(db, 10, 11, 12, 65.5, 72.0)  # inside the slot added at 10:55 → real
    report = fix_hot_water(s, db, ts(9, 0), ts(12, 0), apply=True)
    assert report.rows == 6 and kwh(db, ts(10, 11), ts(10, 12)) > 2


def test_cli_lists_by_default(settings, db, tmp_path, capsys, monkeypatch):
    import maska_heat.__main__ as cli

    s = with_boiler(settings)
    monkeypatch.setattr(cli, "load_settings", lambda path: s)
    monkeypatch.setattr(cli, "load_secrets", lambda path: {})
    monkeypatch.setattr("maska_heat.db.Database", lambda path: db)
    day_11(db)
    assert cli.main(["fix-hot-water"]) == 0
    out = capsys.readouterr().out
    assert "Would correct" in out and "Nothing was changed" in out
