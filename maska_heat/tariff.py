"""Electricity cost per slot: spot price + seller margin + network (day/night) + excise + renewable fee, plus VAT."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache

from .settings import NetworkTariff, TariffSettings


def easter_sunday(year: int) -> date:
    """Gregorian Easter (anonymous algorithm)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


@lru_cache(maxsize=16)
def estonian_holidays(year: int) -> frozenset[date]:
    easter = easter_sunday(year)
    fixed = [(1, 1), (2, 24), (5, 1), (6, 23), (6, 24), (8, 20), (12, 24), (12, 25), (12, 26)]
    return frozenset(
        [date(year, m, d) for m, d in fixed]
        + [easter - timedelta(days=2), easter, easter + timedelta(days=49)]  # Good Friday, Easter, Pentecost
    )


def is_day_tariff(local: datetime, net: NetworkTariff) -> bool:
    if local.weekday() not in net.day_weekdays:
        return False
    if net.holidays_are_night and local.date() in estonian_holidays(local.year):
        return False
    return net.day_start <= local.time() < net.day_end


@dataclass(frozen=True)
class Cost:
    spot: float       # €/kWh, excl. VAT
    network: float
    other: float      # margin + excise + renewable fee
    vat: float
    is_day: bool

    @property
    def total(self) -> float:
        """€/kWh including VAT."""
        return self.spot + self.network + self.other + self.vat


def elec_cost(eur_mwh: float, local: datetime, t: TariffSettings) -> Cost:
    day = is_day_tariff(local, t.network)
    spot = eur_mwh / 1000.0
    network = t.network.day_eur_kwh if day else t.network.night_eur_kwh
    other = t.seller_margin_eur_kwh + t.excise_eur_kwh + t.renewable_fee_eur_kwh
    return Cost(spot, network, other, (spot + network + other) * t.vat, day)
