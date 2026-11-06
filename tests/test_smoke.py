from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import energy_analyzer
from energy_analyzer import synthetic


def test_version() -> None:
    assert energy_analyzer.__version__ == "0.2.0"


def test_sample_data_is_reproducible(sample_dir: Path, tmp_path: Path) -> None:
    """Committed fixtures must equal what the generator writes (no hand-edited data)."""
    written = synthetic.write_sample_data(tmp_path, date(2025, 6, 1), 3)
    assert written
    for path in written:
        committed = sample_dir / path.name
        assert committed.exists(), f"missing fixture {committed.name}"
        assert json.loads(committed.read_text("utf-8")) == json.loads(path.read_text("utf-8"))


def test_synthetic_energy_balance() -> None:
    data = synthetic.generate(date(2025, 6, 1), 5)
    for day in data.days:
        e = day.energy
        load_kwh = sum(r["load"] for r in day.power_rows) * 5 / 60 / 1000
        derived = e["epv"] + e["eInput"] + e["eDischarge"] - e["eOutput"] - e["eCharge"]
        assert abs(derived - load_kwh) < 0.01
        assert len(day.power_rows) == 288


def test_synthetic_uses_demo_serial_only() -> None:
    data = synthetic.generate(date(2025, 6, 1), 1)
    assert {r["sysSn"] for r in data.days[0].power_rows} == {synthetic.DEMO_SN}
