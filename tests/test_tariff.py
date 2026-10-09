from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from maska_heat.settings import NetworkTariff, TariffSettings
from maska_heat.tariff import easter_sunday, elec_cost, estonian_holidays, is_day_tariff

TZ = ZoneInfo("Europe/Tallinn")
NET = NetworkTariff(day_eur_kwh=0.06, night_eur_kwh=0.035)


@pytest.mark.parametrize("year,expected", [(2025, date(2025, 4, 20)), (2026, date(2026, 4, 5)), (2027, date(2027, 3, 28))])
def test_easter(year, expected):
    assert easter_sunday(year) == expected


def test_holidays_include_movable_and_fixed():
    h = estonian_holidays(2026)
    assert date(2026, 4, 3) in h  # Good Friday
    assert date(2026, 2, 24) in h  # Independence Day
    assert date(2026, 6, 23) in h and date(2026, 6, 24) in h


@pytest.mark.parametrize(
    "local,expected",
    [
        (datetime(2026, 10, 9, 6, 59, tzinfo=TZ), False),  # Fri before 07
        (datetime(2026, 10, 9, 7, 0, tzinfo=TZ), True),
        (datetime(2026, 10, 9, 21, 59, tzinfo=TZ), True),
        (datetime(2026, 10, 9, 22, 0, tzinfo=TZ), False),
        (datetime(2026, 10, 10, 12, 0, tzinfo=TZ), False),  # Saturday
        (datetime(2026, 4, 3, 12, 0, tzinfo=TZ), False),  # Good Friday
        (datetime(2026, 12, 24, 12, 0, tzinfo=TZ), False),  # Christmas Eve, Thursday
    ],
)
def test_day_tariff(local, expected):
    assert is_day_tariff(local, NET) is expected


def test_cost_includes_all_components_and_vat():
    t = TariffSettings(vat=0.24, seller_margin_eur_kwh=0.005, excise_eur_kwh=0.002, renewable_fee_eur_kwh=0.008, network=NET)
    c = elec_cost(100.0, datetime(2026, 10, 9, 12, tzinfo=TZ), t)  # 100 €/MWh, weekday day
    assert c.is_day and c.network == 0.06
    assert c.total == pytest.approx((0.1 + 0.06 + 0.015) * 1.24)


def test_negative_spot_price():
    t = TariffSettings(network=NET)
    c = elec_cost(-20.0, datetime(2026, 10, 10, 3, tzinfo=TZ), t)
    assert c.spot == -0.02 and not c.is_day
    assert c.total == pytest.approx((-0.02 + 0.035) * 1.24)


def test_configured_flag():
    assert not TariffSettings().configured
    assert TariffSettings(network=NET).configured


def test_day_window_parses_strings():
    n = NetworkTariff.model_validate({"day_start": "08:00", "day_end": "23:00"})
    assert n.day_start == time(8) and n.day_end == time(23)
