"""SQLite storage with versioned migrations and idempotent upserts."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from itertools import pairwise
from pathlib import Path
from typing import Any

from energy_analyzer.models import DailyEnergy, PowerSample

# Append-only: never edit a released migration, add a new one.
MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE systems (
            sys_sn      TEXT PRIMARY KEY,
            info_json   TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        );
        CREATE TABLE power_samples (
            sys_sn              TEXT NOT NULL,
            ts                  TEXT NOT NULL,
            pv_w                REAL,
            load_w              REAL,
            grid_import_w       REAL,
            grid_export_w       REAL,
            battery_charge_w    REAL,
            battery_discharge_w REAL,
            soc                 REAL,
            PRIMARY KEY (sys_sn, ts)
        ) WITHOUT ROWID;
        CREATE TABLE daily_energy (
            sys_sn                  TEXT NOT NULL,
            day                     TEXT NOT NULL,
            pv_kwh                  REAL,
            load_kwh                REAL,
            grid_import_kwh         REAL,
            grid_export_kwh         REAL,
            battery_charge_kwh      REAL,
            battery_discharge_kwh   REAL,
            battery_grid_charge_kwh REAL,
            ev_kwh                  REAL,
            fetched_at              TEXT NOT NULL,
            PRIMARY KEY (sys_sn, day)
        ) WITHOUT ROWID;
        CREATE TABLE fetch_log (
            sys_sn      TEXT NOT NULL,
            day         TEXT NOT NULL,
            endpoint    TEXT NOT NULL,
            n_rows      INTEGER NOT NULL,
            fetched_at  TEXT NOT NULL,
            PRIMARY KEY (sys_sn, day, endpoint)
        ) WITHOUT ROWID;
        """,
    ),
]

SCHEMA_VERSION = MIGRATIONS[-1][0]

_POWER_COLS = (
    "pv_w",
    "load_w",
    "grid_import_w",
    "grid_export_w",
    "battery_charge_w",
    "battery_discharge_w",
    "soc",
)
_DAILY_COLS = (
    "pv_kwh",
    "load_kwh",
    "grid_import_kwh",
    "grid_export_kwh",
    "battery_charge_kwh",
    "battery_discharge_kwh",
    "battery_grid_charge_kwh",
    "ev_kwh",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _upsert_sql(table: str, keys: tuple[str, ...], cols: tuple[str, ...]) -> str:
    all_cols = keys + cols
    updates = ", ".join(f"{c} = excluded.{c}" for c in cols)
    return (
        f"INSERT INTO {table} ({', '.join(all_cols)}) "
        f"VALUES ({', '.join('?' for _ in all_cols)}) "
        f"ON CONFLICT ({', '.join(keys)}) DO UPDATE SET {updates}"
    )


class Storage:
    """Thin wrapper around one SQLite file. Use as a context manager or call close()."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.migrate()

    def __enter__(self) -> Storage:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.conn:
            yield self.conn

    # --- schema ---------------------------------------------------------------------------

    def schema_version(self) -> int:
        row = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
        ).fetchone()
        if row is None:
            return 0
        version = self.conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        return int(version or 0)

    def migrate(self) -> int:
        """Apply pending migrations; returns the number applied."""
        with self.conn:
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version ("
                "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
        current = self.schema_version()
        if current > SCHEMA_VERSION:
            raise RuntimeError(
                f"database schema v{current} is newer than this program (v{SCHEMA_VERSION}); "
                "upgrade energy-analyzer"
            )
        applied = 0
        for version, sql in MIGRATIONS:
            if version <= current:
                continue
            # One script = one transaction: schema change and version row commit together.
            self.conn.executescript(
                f"BEGIN;\n{sql}\n"
                f"INSERT INTO schema_version (version, applied_at) "
                f"VALUES ({int(version)}, '{_now()}');\nCOMMIT;"
            )
            applied += 1
        return applied

    # --- writes ---------------------------------------------------------------------------

    def upsert_system(self, sys_sn: str, info: dict[str, Any]) -> None:
        with self.conn:
            self.conn.execute(
                _upsert_sql("systems", ("sys_sn",), ("info_json", "updated_at")),
                (sys_sn, json.dumps(info, sort_keys=True), _now()),
            )

    def upsert_power(self, samples: Iterable[PowerSample]) -> int:
        sql = _upsert_sql("power_samples", ("sys_sn", "ts"), _POWER_COLS)
        rows = [(s.sys_sn, s.ts, *(getattr(s, c) for c in _POWER_COLS)) for s in samples]
        with self.conn:
            self.conn.executemany(sql, rows)
        return len(rows)

    def upsert_daily(self, days: Iterable[DailyEnergy]) -> int:
        sql = _upsert_sql("daily_energy", ("sys_sn", "day"), (*_DAILY_COLS, "fetched_at"))
        now = _now()
        rows = [(d.sys_sn, d.day, *(getattr(d, c) for c in _DAILY_COLS), now) for d in days]
        with self.conn:
            self.conn.executemany(sql, rows)
        return len(rows)

    def log_fetch(self, sys_sn: str, day: str, endpoint: str, rows: int) -> None:
        with self.conn:
            self.conn.execute(
                _upsert_sql("fetch_log", ("sys_sn", "day", "endpoint"), ("n_rows", "fetched_at")),
                (sys_sn, day, endpoint, rows, _now()),
            )

    # --- reads ----------------------------------------------------------------------------

    def count(self, table: str) -> int:
        if table not in {"systems", "power_samples", "daily_energy", "fetch_log"}:
            raise ValueError(f"unknown table {table!r}")
        return int(self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    def systems(self) -> list[str]:
        return [r[0] for r in self.conn.execute("SELECT sys_sn FROM systems ORDER BY sys_sn")]

    def power_range(
        self, sys_sn: str, first_day: str | None = None, last_day: str | None = None
    ) -> list[PowerSample]:
        lo = f"{first_day} 00:00:00" if first_day else ""
        hi = f"{last_day} 23:59:59" if last_day else "9999"
        cur = self.conn.execute(
            f"SELECT sys_sn, ts, {', '.join(_POWER_COLS)} FROM power_samples "
            "WHERE sys_sn = ? AND ts >= ? AND ts <= ? ORDER BY ts",
            (sys_sn, lo, hi),
        )
        return [PowerSample(*row) for row in cur]

    def daily_range(
        self, sys_sn: str, first_day: str | None = None, last_day: str | None = None
    ) -> list[DailyEnergy]:
        cur = self.conn.execute(
            f"SELECT sys_sn, day, {', '.join(_DAILY_COLS)} FROM daily_energy "
            "WHERE sys_sn = ? AND day >= ? AND day <= ? ORDER BY day",
            (sys_sn, first_day or "", last_day or "9999"),
        )
        return [DailyEnergy(*row) for row in cur]

    def integrate_day(self, sys_sn: str, day: str, max_gap_min: float = 15.0) -> dict[str, float]:
        """Integrate the stored power series of one day into kWh (left Riemann sum).

        Each sample's power is held until the next sample (or for the median step after the
        last one); gaps longer than max_gap_min count only max_gap_min.
        """
        samples = self.power_range(sys_sn, day, day)
        totals = dict.fromkeys(_POWER_COLS[:-1], 0.0)
        if not samples:
            return totals
        times = [datetime.fromisoformat(s.ts) for s in samples]
        steps = [(b - a).total_seconds() / 60.0 for a, b in pairwise(times)]
        typical = sorted(steps)[len(steps) // 2] if steps else 5.0
        steps.append(typical)
        for sample, step in zip(samples, steps, strict=True):
            hours = min(step, max_gap_min) / 60.0
            for col in totals:
                value = getattr(sample, col)
                if value is not None:
                    totals[col] += value * hours / 1000.0
        return totals
