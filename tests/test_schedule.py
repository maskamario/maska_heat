from datetime import date, datetime

import pytest
from zoneinfo import ZoneInfo

from maska_heat.control import Away, Comfort, DateRange, HouseSchedule, KidSchool, School, SchoolDay
from maska_heat.schedule import Snapshot, slot_at, smart_target

TZ = ZoneInfo("Europe/Tallinn")
MON = date(2026, 10, 12)  # an ordinary Monday


def at(d: date, hh: int, mm: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=TZ)


def snap(**over) -> Snapshot:
    from datetime import time

    week = {d: SchoolDay(leave=time(7), back=time(13)) for d in range(5)}
    base = dict(
        comfort=Comfort(),
        house=HouseSchedule(),
        school=School(zones={"madli": KidSchool(days=week)}),
        away=Away(),
        home_today={},
    )
    base.update(over)
    return Snapshot(**base)


@pytest.mark.parametrize(
    "zone,t,mode,sp",
    [
        ("living", at(MON, 5), "night", 19),
        ("living", at(MON, 6, 45), "day", 22),
        ("madli", at(MON, 6, 45), "day", 20),  # short school morning → Day min
        ("madli", at(MON, 10), "out", 19),
        ("living", at(MON, 10), "day", 22),
        ("madli", at(MON, 13, 30), "day", 22),
        ("madli", at(MON, 23, 30), "night", 19),
        ("madli", at(date(2026, 10, 17), 7), "night", 19),  # Saturday: wake 08:00
        ("madli", at(date(2026, 10, 17), 10), "day", 22),  # no school on Saturday
        ("madli", at(date(2026, 12, 24), 10), "day", 22),  # public holiday
    ],
)
def test_slot_at(zone, t, mode, sp):
    s = slot_at(zone, t, snap())
    assert (s.mode, s.setpoint) == (mode, sp)


def test_home_today_and_school_holiday_cancel_out_period():
    assert slot_at("madli", at(MON, 10), snap(home_today={"madli": MON})).mode == "day"
    holiday = School(zones=snap().school.zones, holidays=[DateRange(start=MON, end=date(2026, 10, 18))])
    assert slot_at("madli", at(MON, 10), snap(school=holiday)).mode == "day"


def test_away_overrides_everything():
    away = Away(start=at(MON, 9), end=at(date(2026, 10, 14), 18))
    assert slot_at("living", at(MON, 12), snap(away=away)).mode == "away"
    assert slot_at("living", at(date(2026, 10, 14), 18), snap(away=away)).mode == "day"


def test_preheat_starts_within_lead_time():
    # madli: 19 °C at night, wants 20 °C at 06:30 → 1 °C / 1.0 °C/h + 0.25 h margin = 1.25 h lead
    early = smart_target("madli", at(MON, 5), 19.0, "resistive", snap())
    assert not early.preheating and early.setpoint == 19
    on_time = smart_target("madli", at(MON, 5, 30), 19.0, "resistive", snap())
    assert on_time.preheating and on_time.setpoint == 20 and "06:30" in on_time.reason


def test_heat_pump_heats_faster_so_starts_later():
    # living: 19 → 22 °C at 06:30, heat pump 1.5 °C/h → 2.25 h lead
    assert not smart_target("living", at(MON, 4), 19.0, "heat_pump", snap()).preheating
    assert smart_target("living", at(MON, 4, 30), 19.0, "heat_pump", snap()).preheating


def test_preheat_before_return_from_school_and_next_change():
    t = smart_target("madli", at(MON, 10, 30), 19.0, "resistive", snap())
    assert t.preheating and t.setpoint == 22  # 3 °C → 3.25 h lead, return at 13:00
    calm = smart_target("madli", at(MON, 9), 19.0, "resistive", snap())
    assert not calm.preheating and calm.mode == "out"
    when, nxt = calm.next_change
    assert (when.hour, nxt.mode, nxt.setpoint) == (13, "day", 22)


def test_no_preheat_for_cooler_period():
    t = smart_target("living", at(MON, 22, 30), 22.0, "heat_pump", snap())
    assert not t.preheating and t.setpoint == 22
