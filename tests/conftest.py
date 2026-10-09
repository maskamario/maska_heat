import pytest

from maska_heat.db import Database
from maska_heat.settings import Settings

CONFIG = {
    "location": {"latitude": 59.33, "longitude": 24.74},
    "tariff": {"network": {"day_eur_kwh": 0.06, "night_eur_kwh": 0.035}},
    "devices": {
        "matis_mill": {"type": "mill_local", "host": "mill.test"},
        "madli_adax": {"type": "adax_cloud"},
        "living_mitsubishi": {"type": "melcloud", "enabled": False},
    },
    "sensors": {"living": {"host": "living.test", "enabled": False}},
    "zones": {
        "madli": {"name": "Madli's bedroom", "device": "madli_adax"},
        "matis": {"name": "Matis's bedroom", "device": "matis_mill"},
        "living": {"name": "Living area", "device": "living_mitsubishi", "temp_sources": ["sensor:living", "device:living_mitsubishi"]},
    },
    "notify": {"enabled": False},
}


@pytest.fixture
def settings() -> Settings:
    return Settings.model_validate(CONFIG)


@pytest.fixture
def db():
    d = Database(":memory:")
    yield d
    d.close()
