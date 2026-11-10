"""Background jobs: every periodic loop and fire-and-forget task runs through here.

A crashing job is logged and keeps its schedule instead of dying silently, triggered tasks are tracked (not left
to the garbage collector), and `stop()` cancels and waits for all of them — nothing starts after it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine

log = logging.getLogger(__name__)


class Jobs:
    def __init__(self) -> None:
        self._tasks: set[asyncio.Task] = set()
        self._stopped = False

    def every(
        self, seconds: float, job: Callable[[], Awaitable[None]], name: str, initial_delay: float = 0.0
    ) -> asyncio.Task | None:
        """Run `job` now (after `initial_delay`) and then every `seconds`; an exception is logged, not fatal."""

        async def loop() -> None:
            if initial_delay:
                await asyncio.sleep(initial_delay)
            while True:
                try:
                    await job()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("job %s crashed", name)
                await asyncio.sleep(seconds)

        return self.spawn(loop(), name)

    def spawn(self, coro: Coroutine, name: str) -> asyncio.Task | None:
        """Start a task in the background; ignored once stopping (so nothing acts on devices after shutdown)."""
        if self._stopped:
            coro.close()
            return None
        task = asyncio.get_running_loop().create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._done)
        return task

    def _done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("task %s failed", task.get_name(), exc_info=task.exception())

    async def stop(self) -> None:
        self._stopped = True
        tasks = list(self._tasks)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
