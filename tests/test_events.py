"""Events (holiday, heating season, outage, ...) and history-aware night spikes."""

from __future__ import annotations

import copy
import random
from dataclasses import replace
from datetime import date, timedelta
from typing import Any

import pytest

from energy_analyzer import synthetic
from energy_analyzer.analyze import analyze, analyze_data
from energy_analyzer.config import Settings
from energy_analyzer.events import KINDS, EventError, EventIndex, validate
from energy_analyzer.fetch import parse_energy, parse_power
from energy_analyzer.models import Anomaly, DailyEnergy, PowerSample
from energy_analyzer.storage import Storage

START = date(2025, 6, 1)
NO_ANOMALIES = synthetic.AnomalySpec()


def day(offset: int) -> str:
    return (START + timedelta(days=offset)).isoformat()


def build(
    days: int = 50,
    *,
    hook: Any = None,
    load_factor: dict[int, float] | None = None,
) -> tuple[list[PowerSample], list[DailyEnergy]]:
    """Synthetic data without injected anomalies; hook(offset, rows) may change the rows,
    load_factor scales the daily consumption of single days."""
    data = synthetic.generate(START, days, anomalies=NO_ANOMALIES)
    samples: list[PowerSample] = []
    daily: list[DailyEnergy] = []
    for offset, d in enumerate(data.days):
        rows = copy.deepcopy(d.power_rows)
        if hook:
            hook(offset, rows)
        samples += parse_power(rows, data.sys_sn)
        e = parse_energy(d.energy, data.sys_sn, d.day)
        f = (load_factor or {}).get(offset)
        if f is not None and e.load_kwh is not None:
            e = replace(e, load_kwh=e.load_kwh * f)
        daily.append(e)
    return samples, daily


def flags(anomalies: list[Anomaly], kind: str, expected: bool | None = None) -> set[str]:
    return {
        a.day
        for a in anomalies
        if a.kind == kind and (expected is None or (a.expected_by is not None) == expected)
    }


def heat_pump(big_night: int | None = None) -> Any:
    rng = random.Random(7)

    def hook(offset: int, rows: list[dict[str, Any]]) -> None:
        start = rng.randrange(36, 54)  # 03:00 .. 04:30 in 5-minute slots
        power = 8000 if offset == big_night else 4000
        for slot in range(start, start + 6):
            rows[slot]["load"] += power

    return hook


# --- history-aware night spikes ----------------------------------------------------------------


def test_nightly_heat_pump_is_not_flagged_once_history_exists() -> None:
    samples, daily = build(hook=heat_pump())
    result = analyze_data(synthetic.DEMO_SN, samples, daily, Settings())
    spikes = flags(result.anomalies, "night_spike")
    # only the first week (no history yet) may be flagged by the baseline rule
    assert spikes and max(spikes) < day(Settings().min_history_days)


def test_unusually_big_night_peak_is_still_flagged() -> None:
    samples, daily = build(hook=heat_pump(big_night=30))
    result = analyze_data(synthetic.DEMO_SN, samples, daily, Settings())
    late = {d for d in flags(result.anomalies, "night_spike") if d >= day(7)}
    assert late == {day(30)}
    msg = next(a.message for a in result.anomalies if a.day == day(30))
    assert "typical night peak" in msg


def test_spike_without_recurring_pattern_still_detected() -> None:
    def hook(offset: int, rows: list[dict[str, Any]]) -> None:
        if offset == 30:
            for slot in range(40, 44):
                rows[slot]["load"] += 2500

    samples, daily = build(hook=hook)
    result = analyze_data(synthetic.DEMO_SN, samples, daily, Settings())
    assert flags(result.anomalies, "night_spike") == {day(30)}


# --- holiday (expect_lower) --------------------------------------------------------------------


def holiday(start: int, end: int) -> list[Any]:
    return [validate("away", "Summer holiday", day(start), day(end), event_id=1)]


def test_holiday_low_consumption_is_expected_and_does_not_pollute_reference() -> None:
    low = {o: 0.4 for o in range(25, 35)}
    samples, daily = build(load_factor=low)
    plain = analyze_data(synthetic.DEMO_SN, samples, daily, Settings())
    marked = analyze_data(synthetic.DEMO_SN, samples, daily, Settings(), holiday(25, 34))
    # without the event the holiday days are anomalies
    assert flags(plain.anomalies, "consumption") >= {day(25), day(26)}
    # with it they are explained, and nothing unexpected remains
    assert flags(marked.anomalies, "consumption", expected=False) == set()
    assert flags(marked.anomalies, "consumption", expected=True) >= {day(25), day(26)}
    assert all(a.expected_by == "Summer holiday" for a in marked.anomalies)


def test_holiday_with_normal_consumption_is_flagged_not_lower() -> None:
    samples, daily = build()
    result = analyze_data(synthetic.DEMO_SN, samples, daily, Settings(), holiday(30, 32))
    assert flags(result.anomalies, "not_lower") == {day(30), day(31), day(32)}
    assert "Summer holiday" in result.anomalies[0].message


# --- heating season (regime_change) --------------------------------------------------------------


def test_heating_season_start_resets_the_reference() -> None:
    more = {o: 1.6 for o in range(30, 50)}
    samples, daily = build(load_factor=more)
    plain = analyze_data(synthetic.DEMO_SN, samples, daily, Settings())
    event = [validate("heating_on", "", day(30), None, event_id=2)]
    marked = analyze_data(synthetic.DEMO_SN, samples, daily, Settings(), event)
    assert len(flags(plain.anomalies, "consumption")) >= 3
    assert flags(marked.anomalies, "consumption") == set()
    assert event[0].end_day is None and event[0].title == KINDS["heating_on"].label


# --- outage (ignore) ----------------------------------------------------------------------------


def test_outage_days_are_ignored_and_excluded() -> None:
    zero = {o: 0.0 for o in (20, 21)}
    samples, daily = build(load_factor=zero)
    plain = analyze_data(synthetic.DEMO_SN, samples, daily, Settings())
    event = [validate("outage", "Inverter update", day(20), day(21), event_id=3)]
    marked = analyze_data(synthetic.DEMO_SN, samples, daily, Settings(), event)
    assert {day(20), day(21)} <= flags(plain.anomalies, "consumption")
    assert flags(marked.anomalies, "consumption") == set()


# --- validation, index, storage ---------------------------------------------------------------


def test_validate_rejects_bad_input() -> None:
    for args in (
        ("nope", "x", "2025-01-01", None),
        ("away", "x", "2025-13-01", None),
        ("away", "x", "2025-01-05", "2025-01-01"),
        ("away", "x" * 121, "2025-01-01", None),
    ):
        with pytest.raises(EventError):
            validate(*args)
    with pytest.raises(EventError):
        validate("away", "x", "2025-01-01", None, effect="explode")


def test_event_index() -> None:
    idx = EventIndex(
        [
            validate("away", "A", "2025-01-10", "2025-01-12"),
            validate("heating_on", "", "2025-01-05", None),
            validate("heating_off", "", "2025-01-20", None),
            validate("outage", "", "2025-01-15", None),
        ]
    )
    assert idx.excluded_from_reference("2025-01-11") and not idx.ignored("2025-01-11")
    assert idx.ignored("2025-01-15") and not idx.ignored("2025-01-16")
    assert idx.regime_start("2025-01-04") is None
    assert idx.regime_start("2025-01-19") == "2025-01-05"
    assert idx.regime_start("2025-01-25") == "2025-01-20"
    assert idx.expected("2025-01-11", -1) is not None and idx.expected("2025-01-11", 1) is None


def test_storage_event_crud_and_analyze_uses_events(tmp_path: Any) -> None:
    with Storage(tmp_path / "e.db") as db:
        assert db.schema_version() == 2
        saved = db.save_event(validate("guests", "Family visit", "2025-06-10", "2025-06-12"))
        assert saved.id is not None and db.count("events") == 1
        updated = db.save_event(replace(saved, title="Family"))
        assert updated.id == saved.id and db.event(saved.id).title == "Family"  # type: ignore[union-attr]
        assert db.delete_event(saved.id) and not db.delete_event(saved.id)
        with pytest.raises(KeyError):
            db.save_event(replace(saved, id=999))


def test_range_analysis_matches_full_analysis(tmp_path: Any) -> None:
    data = synthetic.generate(START, 60)
    with Storage(tmp_path / "e.db") as db:
        for d in data.days:
            db.upsert_power(parse_power(d.power_rows, data.sys_sn))
            db.upsert_daily([parse_energy(d.energy, data.sys_sn, d.day)])
        db.save_event(validate("note", "Painted the house", day(45), None))
        full = analyze(db, data.sys_sn, Settings())
        part = analyze(db, data.sys_sn, Settings(), day(40), day(59))
    assert [a for a in full.anomalies if a.day >= day(40)] == part.anomalies
    assert part.first_day == day(40) and len(part.days) == 20
    assert [e.title for e in part.events] == ["Painted the house"]
