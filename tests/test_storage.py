from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from energy_analyzer.models import DailyEnergy, PowerSample
from energy_analyzer.storage import SCHEMA_VERSION, Storage

SN = "DEMO000000000001"


def _sample(ts: str, load: float = 200.0, pv: float = 0.0) -> PowerSample:
    return PowerSample(SN, ts, pv, load, load, 0.0, 0.0, 0.0, 50.0)


def _day(day: str, pv: float = 10.0) -> DailyEnergy:
    return DailyEnergy(SN, day, pv, 12.0, 5.0, 3.0, 4.0, 4.0, 0.0, 0.0)


def test_schema_is_versioned_and_migrations_are_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "e.db"
    with Storage(db) as s:
        assert s.schema_version() == SCHEMA_VERSION
    with Storage(db) as s:  # reopening applies nothing
        assert s.migrate() == 0
        rows = s.conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
        assert rows == SCHEMA_VERSION


def test_refuses_newer_schema(tmp_path: Path) -> None:
    db = tmp_path / "e.db"
    Storage(db).close()
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO schema_version VALUES (999, 'x')")
    conn.commit()
    conn.close()
    with pytest.raises(RuntimeError, match="newer"):
        Storage(db)


def test_upsert_power_twice_creates_no_duplicates() -> None:
    """Acceptance check 3 (storage level)."""
    samples = [_sample(f"2025-06-01 00:{m:02d}:00") for m in range(0, 60, 5)]
    with Storage(":memory:") as s:
        s.upsert_power(samples)
        s.upsert_power(samples)
        assert s.count("power_samples") == len(samples)


def test_upsert_power_updates_values() -> None:
    with Storage(":memory:") as s:
        s.upsert_power([_sample("2025-06-01 00:00:00", load=100)])
        s.upsert_power([_sample("2025-06-01 00:00:00", load=300)])
        (only,) = s.power_range(SN)
        assert only.load_w == 300


def test_upsert_daily_twice_creates_no_duplicates_and_updates() -> None:
    with Storage(":memory:") as s:
        s.upsert_daily([_day("2025-06-01"), _day("2025-06-02")])
        s.upsert_daily([_day("2025-06-01", pv=20.0)])
        assert s.count("daily_energy") == 2
        first = s.daily_range(SN, "2025-06-01", "2025-06-01")[0]
        assert first.pv_kwh == 20.0


def test_ranges_filter_by_day_and_system() -> None:
    with Storage(":memory:") as s:
        s.upsert_power([_sample("2025-06-01 23:55:00"), _sample("2025-06-02 00:00:00")])
        s.upsert_power([PowerSample("OTHER", "2025-06-01 12:00:00", 1, 1, 1, 1, 1, 1, 1)])
        assert [p.ts for p in s.power_range(SN, "2025-06-02", "2025-06-02")] == [
            "2025-06-02 00:00:00"
        ]
        assert len(s.power_range(SN)) == 2


def test_integrate_day_constant_load() -> None:
    samples = [
        _sample(f"2025-06-01 {h:02d}:{m:02d}:00") for h in range(24) for m in range(0, 60, 5)
    ]
    with Storage(":memory:") as s:
        s.upsert_power(samples)
        kwh = s.integrate_day(SN, "2025-06-01")
    assert kwh["load_w"] == pytest.approx(200 * 24 / 1000)


def test_integrate_day_caps_gaps() -> None:
    with Storage(":memory:") as s:
        s.upsert_power([_sample("2025-06-01 00:00:00"), _sample("2025-06-01 06:00:00")])
        kwh = s.integrate_day(SN, "2025-06-01", max_gap_min=15)
    # two samples, each held for at most 15 minutes
    assert kwh["load_w"] == pytest.approx(2 * 200 * 0.25 / 1000)


def test_systems_and_fetch_log() -> None:
    with Storage(":memory:") as s:
        s.upsert_system(SN, {"sysSn": SN})
        s.upsert_system(SN, {"sysSn": SN, "x": 1})
        s.log_fetch(SN, "2025-06-01", "getOneDayPowerBySn", 288)
        s.log_fetch(SN, "2025-06-01", "getOneDayPowerBySn", 288)
        assert s.systems() == [SN]
        assert s.count("fetch_log") == 1
        with pytest.raises(ValueError):
            s.count("sqlite_master")
