import asyncio

from maska_heat.control import ControlStore, ZoneControl
from maska_heat.executor import Executor
from maska_heat.jobs import Jobs

from tests.test_executor import FakeDevice, reading


async def test_a_crashing_job_keeps_running():
    runs = []

    async def job():
        runs.append(1)
        if len(runs) == 1:
            raise RuntimeError("first round fails")

    jobs = Jobs()
    jobs.every(0.01, job, "test")
    await asyncio.sleep(0.05)
    await jobs.stop()
    assert len(runs) >= 2


async def test_nothing_starts_after_stop():
    jobs = Jobs()
    started = asyncio.Event()

    async def slow():
        started.set()
        await asyncio.sleep(10)

    task = jobs.spawn(slow(), "slow")
    await started.wait()
    await jobs.stop()
    assert task.cancelled()
    ran = []

    async def late():
        ran.append(1)

    assert jobs.spawn(late(), "late") is None
    await asyncio.sleep(0)
    assert ran == []


class SlowDevice(FakeDevice):
    async def set_setpoint(self, t: float) -> None:
        await asyncio.sleep(0.05)  # a cloud heater taking its time
        self.sent.append(t)


async def test_quick_switch_back_is_not_lost(settings, db):
    """Hands-off then straight back to Smart: the slower hand-back must not overwrite the new mode."""
    adax = SlowDevice()
    store = ControlStore(db, ["madli", "matis"])
    ex = Executor(settings, db, store, {"madli_adax": adax, "matis_mill": FakeDevice()})
    reading(db, "madli_adax", 22)
    store.set_zone("madli", ZoneControl(mode="handsoff", engaged=True, restore_setpoint=22))
    release = asyncio.create_task(ex.apply_zone("madli"))
    await asyncio.sleep(0.01)
    ctl = store.zone("madli")
    ctl.mode = "smart"
    store.set_zone("madli", ctl)  # what the UI does
    await release
    assert store.zone("madli").mode == "smart" and not store.zone("madli").engaged
