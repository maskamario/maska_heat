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

SCHEMA_VERSION = 7

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    ts          INTEGER NOT NULL,
    source      TEXT    NOT NULL,      -- 'device:<name>' or 'sensor:<name>'
    temp        REAL,
    setpoint    REAL,
    power_on    INTEGER,
    is_heating  INTEGER,
    power_w     REAL,
    energy_kwh  REAL,                  -- device counter; unreliable for MELCloud (use the energy table)
    humidity    REAL,
    mode        TEXT,                  -- device operating mode as reported (e.g. heat, Control individually)
    floor_temp  REAL,                  -- floor sensor of a floor heating thermostat
    level_pct   REAL                   -- water heater: hot water left, % (state of charge)
);
CREATE INDEX IF NOT EXISTS readings_source_ts ON readings(source, ts);

-- Electricity use per period from the device's own energy report (MELCloud: hourly)
CREATE TABLE IF NOT EXISTS energy (
    source      TEXT    NOT NULL,      -- 'device:<name>'
    ts          INTEGER NOT NULL,      -- period start
    minutes     INTEGER NOT NULL,
    kwh         REAL    NOT NULL,
    fetched_at  INTEGER NOT NULL,
    PRIMARY KEY (source, ts, minutes)
);

CREATE TABLE IF NOT EXISTS prices (
    ts       INTEGER NOT NULL,
    minutes  INTEGER NOT NULL,
    eur_mwh  REAL    NOT NULL,
    PRIMARY KEY (ts, minutes)
);

-- Door open/closed changes (one row per change)
CREATE TABLE IF NOT EXISTS doors (
    ts       INTEGER NOT NULL,
    source   TEXT    NOT NULL,         -- 'door:<name>'
    is_open  INTEGER NOT NULL,
    PRIMARY KEY (source, ts)
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
    mode: str | None = None
    floor_temp: float | None = None
    level_pct: float | None = None


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
            old_version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            self._conn.executescript(SCHEMA)
            self._migrate(old_version)
            # never lower it: a newer build may have run on this database
            self._conn.execute(f"PRAGMA user_version={max(old_version, SCHEMA_VERSION)}")

    def _migrate(self, old_version: int = 0) -> None:
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(readings)")}
        if "mode" not in cols:  # added in schema 3
            self._conn.execute("ALTER TABLE readings ADD COLUMN mode TEXT")
        if "floor_temp" not in cols:  # added in schema 5
            self._conn.execute("ALTER TABLE readings ADD COLUMN floor_temp REAL")
        if "level_pct" not in cols:  # added in schema 6
            self._conn.execute("ALTER TABLE readings ADD COLUMN level_pct REAL")
        done = self._conn.execute("SELECT 1 FROM kv WHERE key='migrated_floor_temp'").fetchone()
        if 5 <= old_version < 7 and not done:
            # Schema 5-6 logged a floor thermostat's calibrated floor reading as temp and the raw sensor as
            # floor_temp. Only floor thermostats (on their floor sensor) wrote floor_temp.
            self._conn.execute(
                "UPDATE readings SET floor_temp = temp, temp = NULL WHERE floor_temp IS NOT NULL AND temp IS NOT NULL"
            )
        if not done:  # once only, even if an older build later resets user_version to 6
            self._conn.execute(
                "INSERT OR REPLACE INTO kv (key, value, updated_at) VALUES ('migrated_floor_temp', '1', strftime('%s','now'))"
            )

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
            "INSERT INTO readings (ts, source, temp, setpoint, power_on, is_heating, power_w, energy_kwh, humidity, mode,"
            " floor_temp, level_pct) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (r.ts, r.source, r.temp, r.setpoint, _b(r.power_on), _b(r.is_heating), r.power_w, r.energy_kwh, r.humidity, r.mode,
                 r.floor_temp, r.level_pct)
                for r in readings
            ],
        )

    def latest_reading(self, source: str) -> sqlite3.Row | None:
        rows = self._all("SELECT * FROM readings WHERE source=? ORDER BY ts DESC LIMIT 1", (source,))
        return rows[0] if rows else None

    def count_readings(self, source: str, since: int, until: int) -> int:
        return self._all("SELECT COUNT(*) AS n FROM readings WHERE source=? AND ts>=? AND ts<?", (source, since, until))[0]["n"]

    def readings(self, source: str, since: int, until: int | None = None, step: int = 0) -> list[sqlite3.Row]:
        """Readings in [since, until); with `step`, only the first reading of each `step`-second bucket."""
        if step:  # SQLite returns the bare columns of the row that holds MIN(ts)
            return self._all(
                "SELECT *, MIN(ts) AS ts FROM readings WHERE source=? AND ts>=? AND ts<? GROUP BY ts / ? ORDER BY ts",
                (source, since, until or 2**62, step),
            )
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

    # energy -------------------------------------------------------------------

    def upsert_energy(self, source: str, points: list[tuple[int, int, float]]) -> None:
        now = int(time.time())
        self._many(
            "INSERT OR REPLACE INTO energy (source, ts, minutes, kwh, fetched_at) VALUES (?,?,?,?,?)",
            [(source, ts, minutes, kwh, now) for ts, minutes, kwh in points],
        )

    def energy(self, source: str, since: int, until: int) -> list[sqlite3.Row]:
        return self._all(
            "SELECT * FROM energy WHERE source=? AND ts>=? AND ts<? ORDER BY ts", (source, since, until)
        )

    # doors --------------------------------------------------------------------

    def add_door_state(self, source: str, ts: int, is_open: bool) -> bool:
        """Record a door change. Returns False (and stores nothing) when it repeats the last known state."""
        last = self.door_state_at(source, 2**62)
        if last is not None and bool(last["is_open"]) == is_open:
            return False
        self._exec("INSERT OR REPLACE INTO doors (ts, source, is_open) VALUES (?,?,?)", (ts, source, int(is_open)))
        return True

    def door_state_at(self, source: str, ts: int) -> sqlite3.Row | None:
        rows = self._all("SELECT * FROM doors WHERE source=? AND ts<=? ORDER BY ts DESC LIMIT 1", (source, ts))
        return rows[0] if rows else None

    def door_changes(self, source: str, since: int, until: int) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM doors WHERE source=? AND ts>=? AND ts<? ORDER BY ts", (source, since, until))

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
