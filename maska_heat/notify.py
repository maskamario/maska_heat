"""Phone notifications via ntfy (https://ntfy.sh). Both phones subscribe to the same private topic."""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from .db import Database
from .settings import NotifySettings

log = logging.getLogger(__name__)

PRIORITY = {"low": 2, "default": 3, "high": 4, "urgent": 5}


class Notifier:
    def __init__(self, cfg: NotifySettings, topic: str | None, db: Database, tz: ZoneInfo, client: httpx.AsyncClient | None = None):
        self.cfg = cfg
        self.topic = topic
        self.db = db
        self.tz = tz
        self._client = client or httpx.AsyncClient(timeout=10.0)

    @property
    def available(self) -> bool:
        return self.cfg.enabled and bool(self.topic)

    def in_quiet_hours(self, now: datetime | None = None) -> bool:
        t = (now or datetime.now(self.tz)).time()
        start, end = self.cfg.quiet_start, self.cfg.quiet_end
        return (start <= t or t < end) if start > end else (start <= t < end)

    async def send(self, kind: str, title: str, message: str, priority: str = "default", tags: str = "") -> bool:
        """Send a notification. Quiet hours hold back everything below 'high'."""
        if not self.available:
            log.info("notification not sent (ntfy not configured): %s — %s", title, message)
            return False
        if self.in_quiet_hours() and PRIORITY[priority] < PRIORITY["high"]:
            log.info("notification suppressed (quiet hours): %s", title)
            return False
        payload = {"topic": self.topic, "title": title, "message": message, "priority": PRIORITY[priority]}
        if tags:
            payload["tags"] = [t.strip() for t in tags.split(",") if t.strip()]
        try:
            r = await self._client.post(self.cfg.ntfy_server.rstrip("/"), json=payload)
            r.raise_for_status()
            ok = True
        except httpx.HTTPError as e:
            log.warning("ntfy send failed: %s", e)
            ok = False
        self.db.add_notification(kind, title, message, ok)
        return ok

    async def aclose(self) -> None:
        await self._client.aclose()
