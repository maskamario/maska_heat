import time

import pytest

from maska_heat.adapters.base import Capabilities
from maska_heat.control import ControlStore, ZoneControl
from maska_heat.db import Reading
from maska_heat.executor import Executor


class FakeDevice:
    def __init__(self, kind: str = "resistive"):
        self.capabilities = Capabilities(kind, 5, 35, 0.5, False, False)
        self.sent: list[float] = []
        self.calls: list[str] = []

    async def read(self):
        raise NotImplementedError

    async def set_setpoint(self, t: float) -> None:
        self.sent.append(t)

    async def take_control(self) -> None:
        self.calls.append("take")

    async def release_control(self) -> None:
        self.calls.append("release")


@pytest.fixture
def rig(settings, db):
    adax, mill = FakeDevice(), FakeDevice()
    store = ControlStore(db, ["madli", "matis"])
    ex = Executor(settings, db, store, {"madli_adax": adax, "matis_mill": mill})
    return ex, store, db, adax, mill


def reading(db, device: str, setpoint: float, power_on: bool = True, age: int = 10):
    db.add_readings([Reading(int(time.time()) - age, f"device:{device}", temp=20.5, setpoint=setpoint, power_on=power_on)])


async def test_handsoff_never_writes(rig):
    ex, store, db, adax, mill = rig
    reading(db, "madli_adax", 22)
    reading(db, "matis_mill", 10)
    await ex.run_all()
    assert adax.sent == [] and mill.sent == [] and mill.calls == []


async def test_manual_sends_once_then_restores_on_handsoff(rig):
    ex, store, db, adax, _ = rig
    reading(db, "madli_adax", 22)
    store.set_zone("madli", ZoneControl(mode="manual", manual_setpoint=20.3))
    await ex.apply_zone("madli")
    assert adax.sent == [20.5]  # clamped to the device's 0.5 step
    ctl = store.zone("madli")
    assert ctl.engaged and ctl.restore_setpoint == 22

    await ex.apply_zone("madli")  # cloud heater not synced yet → no repeat
    assert adax.sent == [20.5]
    reading(db, "madli_adax", 20.5, age=0)
    await ex.apply_zone("madli")
    assert adax.sent == [20.5]

    store.set_zone("madli", ctl.model_copy(update={"mode": "handsoff"}))
    await ex.apply_zone("madli")
    assert adax.sent == [20.5, 22]
    assert not store.zone("madli").engaged


async def test_mill_is_taken_and_released(rig):
    ex, store, db, _, mill = rig
    reading(db, "matis_mill", 10)
    store.set_zone("matis", ZoneControl(mode="smart"))
    await ex.apply_zone("matis")
    expected = ex.decide("matis").setpoint
    assert mill.calls == ["take"] and mill.sent == [expected]

    store.set_zone("matis", store.zone("matis").model_copy(update={"mode": "handsoff"}))
    await ex.apply_zone("matis")
    assert mill.calls == ["take", "release"]


async def test_mill_released_on_shutdown_and_retaken_after_restart(rig, settings):
    ex, store, db, _, mill = rig
    reading(db, "matis_mill", 21)
    store.set_zone("matis", ZoneControl(mode="manual", manual_setpoint=21))
    await ex.apply_zone("matis")
    await ex.stop()
    assert mill.calls == ["take", "release"]

    restarted = Executor(settings, db, store, {"matis_mill": mill})
    await restarted.apply_zone("matis")  # setpoint already equal, but the Mill is on its weekly program now
    assert mill.calls == ["take", "release", "take"]


async def test_switched_off_device_is_left_alone(rig):
    ex, store, db, adax, _ = rig
    reading(db, "madli_adax", 22, power_on=False)
    store.set_zone("madli", ZoneControl(mode="manual", manual_setpoint=20))
    await ex.apply_zone("madli")
    assert adax.sent == []
    assert "switched off" in db.events(1)[0]["message"]
    assert ex.decide("madli").blocked


async def test_stale_reading_blocks_writes(rig):
    ex, store, db, adax, _ = rig
    reading(db, "madli_adax", 22, age=3600)
    store.set_zone("madli", ZoneControl(mode="manual", manual_setpoint=20))
    await ex.apply_zone("madli")
    assert adax.sent == []


async def test_device_error_becomes_event(rig):
    ex, store, db, adax, _ = rig

    async def boom(t):
        raise RuntimeError("cloud down")

    adax.set_setpoint = boom
    reading(db, "madli_adax", 22)
    store.set_zone("madli", ZoneControl(mode="manual", manual_setpoint=20))
    await ex.apply_zone("madli")
    assert "cloud down" in db.events(1)[0]["message"]
