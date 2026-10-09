"""Hourly weather forecast from Open-Meteo (no API key)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

API_URL = "https://api.open-meteo.com/v1/forecast"
HOURLY = "temperature_2m,wind_speed_10m,shortwave_radiation,cloud_cover"


@dataclass(frozen=True)
class WeatherPoint:
    start: datetime  # UTC
    temp_c: float
    wind_ms: float
    radiation_wm2: float
    cloud_pct: float


async def fetch_forecast(
    latitude: float, longitude: float, days: int = 2, client: httpx.AsyncClient | None = None
) -> list[WeatherPoint]:
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=15.0)
    try:
        r = await client.get(
            API_URL,
            params={
                "latitude": latitude,
                "longitude": longitude,
                "hourly": HOURLY,
                "wind_speed_unit": "ms",
                "timezone": "UTC",
                "forecast_days": days,
            },
        )
        r.raise_for_status()
        h = r.json()["hourly"]
    finally:
        if own_client:
            await client.aclose()
    return [
        WeatherPoint(
            start=datetime.fromisoformat(t).replace(tzinfo=timezone.utc),
            temp_c=temp,
            wind_ms=wind,
            radiation_wm2=rad,
            cloud_pct=cloud,
        )
        for t, temp, wind, rad, cloud in zip(
            h["time"], h["temperature_2m"], h["wind_speed_10m"], h["shortwave_radiation"], h["cloud_cover"]
        )
    ]
