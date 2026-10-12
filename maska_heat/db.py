"""SQLite storage. Timestamps are unix seconds (UTC) everywhere.

A single connection is shared by the async jobs and the web app; every call is short, so it runs
inline under a lock instead of a thread pool.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    ts          INTEGER NOT NULL,
    source      TEXT    NOT NULL,      -- 'device:<name>' or 'sensor:<name>'
    temp        REAL,
    setpoint    REAL,
    power_on    INTEGER,
    is_heating  INTEGER,
    power_w     REAL,
    energy_kwh  REAL,
    humidity    REAL
);
CREATE INDEX IF NOT EXISTS readings_source_ts ON readings(source, ts);

CREATE TABLE IF NOT EXISTS prices (
    ts       INTEGER NOT NULL,
    minutes  INTEGER NOT NULL,
    eur_mwh  REAL    NOT NULL,
    PRIMARY KEY (ts, minutes)
);

CREATE TABLE IF NOT EXISTS weather (
    ts         INTEGER PRIMARY KEY,
    issued_at  INTEGER NOT NULL,
    temp       REAL,
    wind       REAL,
    radiation  REAL,
    cloud      REAL
);

CREATE TABLE IF NOT EXISTS fires (
    id      INTEGER PRIMARY KEY,
    stove   TEXT    NOT NULL,          -- 'masonry' | 'cookstove'
    lit_at  INTEGER NOT NULL,
    loads   REAL,
    note    TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY,
    ts       INTEGER NOT NULL,
    level    TEXT    NOT NULL,         -- 'info' | 'warning' | 'error'
    source   TEXT    NOT NULL,
    message  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);

-- UI-editable settings and per-zone control state, as JSON documents
CREATE TABLE IF NOT EXISTS kv (
    key         TEXT PRIMARY KEY,
    value       TEXT    NOT NULL,
    updated_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS notifications (
    id       INTEGER PRIMARY KEY,
    ts       INTEGER NOT NULL,
    kind     TEXT    NOT NULL,
    title    TEXT    NOT NULL,
    message  TEXT    NOT NULL,
    ok       INTEGER NOT NULL
);
"""


@dataclass
class Reading:
    ts: int
    source: str
    temp: float | None = None
    setpoint: float | None = None
    power_on: bool | None = None
    is_heating: bool | None = None
    power_w: float | None = None
    energy_kwh: float | None = None
    humidity: float | None = None


def _b(v: bool | None) -> int | None:
    return None if v is None else int(v)


class Database:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(SCHEMA)
            self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _exec(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, tuple(params))

    def _many(self, sql: str, rows: list[tuple]) -> None:
        if not rows:
            return
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.executemany(sql, rows)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def _all(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    # readings -----------------------------------------------------------------

    def add_readings(self, readings: list[Reading]) -> None:
        self._many(
            "INSERT INTO readings VALUES (?,?,?,?,?,?,?,?,?)",
            [
                (r.ts, r.source, r.temp, r.setpoint, _b(r.power_on), _b(r.is_heating), r.power_w, r.energy_kwh, r.humidity)
                for r in readings
            ],
        )

    def latest_reading(self, source: str) -> sqlite3.Row | None:
        rows = self._all("SELECT * FROM readings WHERE source=? ORDER BY ts DESC LIMIT 1", (source,))
        return rows[0] if rows else None

    def readings(self, source: str, since: int, until: int | None = None) -> list[sqlite3.Row]:
        return self._all(
            "SELECT * FROM readings WHERE source=? AND ts>=? AND ts<? ORDER BY ts",
            (source, since, until or 2**62),
        )

    # prices -------------------------------------------------------------------

    def upsert_prices(self, points: list[tuple[int, int, float]]) -> None:
        self._many("INSERT OR REPLACE INTO prices (ts, minutes, eur_mwh) VALUES (?,?,?)", points)

    def prices(self, since: int, until: int) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM prices WHERE ts>=? AND ts<? ORDER BY ts", (since, until))

    def last_price_ts(self) -> int | None:
        row = self._all("SELECT MAX(ts + minutes*60) AS end_ts FROM prices")[0]
        return row["end_ts"]

    # weather ------------------------------------------------------------------

    def upsert_weather(self, rows: list[tuple[int, int, float, float, float, float]]) -> None:
        self._many(
            "INSERT OR REPLACE INTO weather (ts, issued_at, temp, wind, radiation, cloud) VALUES (?,?,?,?,?,?)", rows
        )

    def weather(self, since: int, until: int) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM weather WHERE ts>=? AND ts<? ORDER BY ts", (since, until))

    def weather_at(self, ts: int) -> sqlite3.Row | None:
        rows = self._all("SELECT * FROM weather WHERE ts<=? ORDER BY ts DESC LIMIT 1", (ts,))
        return rows[0] if rows else None

    # fires --------------------------------------------------------------------

    def add_fire(self, stove: str, lit_at: int, loads: float | None = None, note: str | None = None) -> int:
        cur = self._exec("INSERT INTO fires (stove, lit_at, loads, note) VALUES (?,?,?,?)", (stove, lit_at, loads, note))
        return int(cur.lastrowid)

    def delete_fire(self, fire_id: int) -> None:
        self._exec("DELETE FROM fires WHERE id=?", (fire_id,))

    def fires(self, since: int, until: int | None = None) -> list[sqlite3.Row]:
        return self._all(
            "SELECT * FROM fires WHERE lit_at>=? AND lit_at<? ORDER BY lit_at DESC", (since, until or 2**62)
        )

    # key/value documents ------------------------------------------------------

    def get_kv(self, key: str) -> str | None:
        rows = self._all("SELECT value FROM kv WHERE key=?", (key,))
        return rows[0]["value"] if rows else None

    def set_kv(self, key: str, value: str) -> None:
        self._exec(
            "INSERT INTO kv (key, value, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, int(time.time())),
        )

    # events & notifications ---------------------------------------------------

    def add_event(self, level: str, source: str, message: str, ts: int | None = None) -> None:
        self._exec(
            "INSERT INTO events (ts, level, source, message) VALUES (?,?,?,?)",
            (ts or int(time.time()), level, source, message),
        )

    def events(self, limit: int = 20) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM events ORDER BY ts DESC, id DESC LIMIT ?", (limit,))

    def add_notification(self, kind: str, title: str, message: str, ok: bool) -> None:
        self._exec(
            "INSERT INTO notifications (ts, kind, title, message, ok) VALUES (?,?,?,?,?)",
            (int(time.time()), kind, title, message, int(ok)),
        )
