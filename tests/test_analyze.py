from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pytest

from energy_analyzer import synthetic
from energy_analyzer.analyze import analyze, analyze_data
from energy_analyzer.config import Settings
from energy_analyzer.fetch import parse_energy, parse_power
from energy_analyzer.models import AnalysisResult, DailyEnergy, PowerSample
from energy_analyzer.storage import Storage

SN = synthetic.DEMO_SN
START = date(2025, 6, 1)
ANOMALY_OFFSETS = {40: "baseline", 47: "night_spike", 53: "consumption"}


def sample(ts: str, load: float | None, pv: float | None = 0.0) -> PowerSample:
    return PowerSample(SN, ts, pv, load, None, None, None, None, None)


def energy(
    day: str,
    pv: float | None = 10.0,
    load: float | None = 8.0,
    imp: float | None = 2.0,
    exp: float | None = 4.0,
) -> DailyEnergy:
    return DailyEnergy(SN, day, pv, load, imp, exp, None, None, None, None)


def flat_day(day: str, load: float = 150.0) -> list[PowerSample]:
    return [sample(f"{day} {h:02d}:{m:02d}:00", load) for h in range(24) for m in (0, 30)]


@pytest.fixture(scope="module")
def dataset() -> synthetic.SyntheticDataset:
    return synthetic.generate(START, 60)


@pytest.fixture(scope="module")
def synthetic_input(
    dataset: synthetic.SyntheticDataset,
) -> tuple[list[PowerSample], list[DailyEnergy]]:
    samples: list[PowerSample] = []
    daily: list[DailyEnergy] = []
    for d in dataset.days:
        samples += parse_power(d.power_rows, dataset.sys_sn)
        daily.append(parse_energy(d.energy, dataset.sys_sn, d.day))
    return samples, daily


@pytest.fixture(scope="module")
def synthetic_result(
    synthetic_input: tuple[list[PowerSample], list[DailyEnergy]],
) -> AnalysisResult:
    samples, daily = synthetic_input
    return analyze_data(SN, samples, daily, Settings())


def day_of(offset: int) -> str:
    return (START + timedelta(days=offset)).isoformat()


# --- 1. baseline on a hand-built day ----------------------------------------------------------


def test_baseline_is_median_of_night_samples_with_zero_pv() -> None:
    day = "2025-01-10"
    samples = [
        sample(f"{day} 01:55:00", 5000.0),  # before the window
        sample(f"{day} 02:00:00", 100.0),
        sample(f"{day} 02:30:00", 300.0),
        sample(f"{day} 03:00:00", 120.0),
        sample(f"{day} 03:30:00", 9000.0, pv=50.0),  # PV > 0: ignored
        sample(f"{day} 04:00:00", 140.0),
        sample(f"{day} 04:30:00", None),  # missing load: ignored
        sample(f"{day} 04:45:00", 7000.0, pv=None),  # missing PV: ignored
        sample(f"{day} 05:00:00", 8000.0),  # end is exclusive
        sample(f"{day} 12:00:00", 6000.0, pv=3000.0),
    ]
    result = analyze_data(SN, samples, [], Settings())
    (stats,) = result.days
    assert stats.baseline_w == pytest.approx(130.0)  # median of 100, 120, 140, 300
    assert stats.night_max_w == pytest.approx(300.0)
    assert stats.pv_kwh is None
    assert stats.self_consumption is None


def test_baseline_needs_three_samples() -> None:
    day = "2025-01-10"
    samples = [sample(f"{day} 02:00:00", 100.0), sample(f"{day} 03:00:00", 110.0)]
    (stats,) = analyze_data(SN, samples, [], Settings()).days
    assert stats.baseline_w is None
    assert stats.night_max_w == pytest.approx(110.0)


def test_night_window_wrapping_midnight_uses_calendar_date() -> None:
    settings = replace(Settings(), night_start="23:00", night_end="01:00")
    samples = [
        sample("2025-01-10 00:00:00", 100.0),
        sample("2025-01-10 00:30:00", 110.0),
        sample("2025-01-10 00:45:00", 120.0),
        sample("2025-01-10 12:00:00", 999.0),
        sample("2025-01-10 23:00:00", 400.0),
        sample("2025-01-10 23:30:00", 410.0),
        sample("2025-01-10 23:45:00", 420.0),
    ]
    (stats,) = analyze_data(SN, samples, [], settings).days
    assert stats.baseline_w == pytest.approx(260.0)  # median of 100,110,120,400,410,420


# --- 2. baseline accuracy on synthetic data ---------------------------------------------------


def test_synthetic_baseline_within_ten_percent(
    dataset: synthetic.SyntheticDataset, synthetic_result: AnalysisResult
) -> None:
    by_day = {s.day: s for s in synthetic_result.days}
    assert len(by_day) == 60
    for offset, d in enumerate(dataset.days):
        if offset in ANOMALY_OFFSETS:
            continue
        computed = by_day[d.day.isoformat()].baseline_w
        assert computed is not None
        assert computed == pytest.approx(d.baseline_w, rel=0.10), d.day


# --- 3./4. anomaly detection on synthetic data ------------------------------------------------


def test_injected_anomalies_detected(synthetic_result: AnalysisResult) -> None:
    kinds: dict[str, set[str]] = {}
    for a in synthetic_result.anomalies:
        kinds.setdefault(a.day, set()).add(a.kind)
    for offset, kind in ANOMALY_OFFSETS.items():
        assert kind in kinds.get(day_of(offset), set()), (offset, kind)
    spike = next(a for a in synthetic_result.anomalies if a.kind == "night_spike")
    assert spike.value > 2000
    assert spike.score == pytest.approx(spike.value / spike.reference)
    assert "03:" in spike.message


def test_normal_days_not_flagged(synthetic_result: AnalysisResult) -> None:
    flagged = {a.day for a in synthetic_result.anomalies}
    assert flagged == {day_of(o) for o in ANOMALY_OFFSETS}


def test_anomalies_sorted_by_day_then_kind(synthetic_result: AnalysisResult) -> None:
    keys = [(a.day, a.kind) for a in synthetic_result.anomalies]
    assert keys == sorted(keys)


# --- 5. configurability -----------------------------------------------------------------------


def test_night_window_changes_baseline(
    synthetic_input: tuple[list[PowerSample], list[DailyEnergy]],
) -> None:
    samples, daily = synthetic_input
    default = analyze_data(SN, samples, daily, Settings())
    # 21:00-22:00: no PV, but the +900 W evening load of the synthetic household
    evening = replace(Settings(), night_start="21:00", night_end="22:00")
    shifted = analyze_data(SN, samples, daily, evening)
    d0, s0 = default.days[0], shifted.days[0]
    assert d0.baseline_w is not None and s0.baseline_w is not None
    assert s0.baseline_w > d0.baseline_w + 800
    assert shifted.settings["night_start"] == "21:00"


def test_huge_z_threshold_suppresses_all_history_based_flags(
    synthetic_input: tuple[list[PowerSample], list[DailyEnergy]],
) -> None:
    """Since v0.2.0 night spikes are also judged against previous nights' peaks."""
    samples, daily = synthetic_input
    result = analyze_data(SN, samples, daily, replace(Settings(), z_threshold=1e9))
    assert result.anomalies == []


def test_insufficient_history_no_flags() -> None:
    days = [(date(2025, 1, 1) + timedelta(days=i)).isoformat() for i in range(6)]
    samples = [s for i, d in enumerate(days) for s in flat_day(d, 150.0 + i)]
    samples += flat_day("2025-01-07", 900.0)  # 7th day: only 6 days of history
    daily = [energy(d, load=8.0 + 0.1 * i) for i, d in enumerate(days)]
    daily.append(energy("2025-01-07", load=80.0))
    result = analyze_data(SN, samples, daily, Settings())
    assert result.anomalies == []
    # with one fewer day required, the same data is flagged
    relaxed = analyze_data(SN, samples, daily, replace(Settings(), min_history_days=6))
    assert {(a.day, a.kind) for a in relaxed.anomalies} == {
        ("2025-01-07", "baseline"),
        ("2025-01-07", "consumption"),
    }


def test_zero_spread_history_is_skipped() -> None:
    days = [(date(2025, 1, 1) + timedelta(days=i)).isoformat() for i in range(10)]
    samples = [s for d in days for s in flat_day(d, 150.0)]
    samples += flat_day("2025-01-11", 300.0)
    result = analyze_data(SN, samples, [], Settings())
    assert result.anomalies == []


def test_night_spike_needs_min_watts() -> None:
    day = "2025-01-10"
    samples = [*flat_day(day, 100.0), sample(f"{day} 03:10:00", 450.0)]
    assert analyze_data(SN, samples, [], Settings()).anomalies == []  # 4.5x but only +350 W
    samples.append(sample(f"{day} 03:20:00", 700.0))
    (spike,) = analyze_data(SN, samples, [], Settings()).anomalies
    assert spike.kind == "night_spike"
    assert spike.value == 700.0
    assert spike.reference == 100.0
    assert spike.score == pytest.approx(7.0)
    assert "03:20" in spike.message


# --- 6. arithmetic, months, trend, empty input ------------------------------------------------


def test_self_consumption_and_autarky() -> None:
    daily = [
        energy("2025-03-01", pv=10.0, load=8.0, imp=2.0, exp=4.0),
        energy("2025-03-02", pv=0.0, load=0.0, imp=0.0, exp=0.0),
        energy("2025-03-03", pv=5.0, load=4.0, imp=6.0, exp=7.0),  # inconsistent -> clamped
    ]
    a, b, c = analyze_data(SN, [], daily, Settings()).days
    assert a.self_consumption == pytest.approx(0.6)
    assert a.self_sufficiency == pytest.approx(0.75)
    assert b.self_consumption is None and b.self_sufficiency is None
    assert c.self_consumption == 0.0 and c.self_sufficiency == 0.0


def test_monthly_table() -> None:
    daily = [
        energy("2025-03-30", pv=10.0, load=8.0, imp=2.0, exp=4.0),
        energy("2025-03-31", pv=6.0, load=None, imp=1.0, exp=2.0),
        energy("2025-04-01", pv=0.0, load=5.0, imp=5.0, exp=0.0),
    ]
    samples = flat_day("2025-03-30", 100.0) + flat_day("2025-03-31", 200.0)
    samples += flat_day("2025-04-02", 300.0)  # day with samples only
    result = analyze_data(SN, samples, daily, Settings())
    assert [d.day for d in result.days] == ["2025-03-30", "2025-03-31", "2025-04-01", "2025-04-02"]
    march, april = result.months
    assert march.month == "2025-03" and march.days == 2
    assert march.pv_kwh == pytest.approx(16.0)
    assert march.load_kwh == pytest.approx(8.0)
    assert march.grid_import_kwh == pytest.approx(3.0)
    assert march.grid_export_kwh == pytest.approx(6.0)
    assert march.self_consumption == pytest.approx(10.0 / 16.0)
    assert march.self_sufficiency == pytest.approx(1 - 3.0 / 8.0)
    assert march.baseline_median_w == pytest.approx(150.0)
    assert april.days == 2
    assert april.self_consumption is None
    assert april.self_sufficiency == pytest.approx(0.0)
    assert april.baseline_median_w == pytest.approx(300.0)
    assert result.first_day == "2025-03-30" and result.last_day == "2025-04-02"


def test_baseline_trend_sign() -> None:
    days = [(date(2025, 1, 1) + timedelta(days=i)).isoformat() for i in range(10)]
    up = [s for i, d in enumerate(days) for s in flat_day(d, 100.0 + 2.0 * i)]
    trend = analyze_data(SN, up, [], Settings()).baseline_trend_w_per_30d
    assert trend == pytest.approx(60.0)
    down = [s for i, d in enumerate(days) for s in flat_day(d, 200.0 - i)]
    down_trend = analyze_data(SN, down, [], Settings()).baseline_trend_w_per_30d
    assert down_trend is not None and down_trend < 0
    one = analyze_data(SN, flat_day(days[0]), [], Settings())
    assert one.baseline_trend_w_per_30d is None


def test_empty_input() -> None:
    result = analyze_data(SN, [], [], Settings())
    assert result.sys_sn == SN
    assert result.days == [] and result.anomalies == [] and result.months == []
    assert result.first_day is None and result.last_day is None
    assert result.baseline_trend_w_per_30d is None
    assert result.settings["rolling_days"] == "28"
    assert result.settings["z_threshold"] == "3.5"


def test_invalid_night_window_rejected() -> None:
    with pytest.raises(ValueError, match="HH:MM"):
        analyze_data(SN, [], [], replace(Settings(), night_start="2am"))


# --- 7. storage wrapper -----------------------------------------------------------------------


def test_analyze_from_storage_matches_pure_function(
    synthetic_input: tuple[list[PowerSample], list[DailyEnergy]],
) -> None:
    samples, daily = synthetic_input
    with Storage(":memory:") as storage:
        storage.upsert_power(samples)
        storage.upsert_daily(daily)
        assert analyze(storage, SN, Settings()) == analyze_data(SN, samples, daily, Settings())
        lo, hi = day_of(10), day_of(20)
        sub_s = [s for s in samples if lo <= s.day <= hi]
        sub_d = [d for d in daily if lo <= d.day <= hi]
        ranged = analyze(storage, SN, Settings(), lo, hi)
        assert ranged == analyze_data(SN, sub_s, sub_d, Settings())
        assert ranged.first_day == lo and ranged.last_day == hi
        assert analyze(storage, "OTHER", Settings()).days == []


def test_min_rel_deviation_is_the_effect_size_floor() -> None:
    """Without the floor, two random 'busy' synthetic days (offsets 25 and 31, ~+11 %) cross
    z > 3.5; the default 20 % floor keeps them unflagged while real anomalies remain."""
    from dataclasses import replace

    from energy_analyzer import synthetic
    from energy_analyzer.analyze import analyze_data
    from energy_analyzer.config import Settings
    from energy_analyzer.fetch import parse_energy, parse_power

    data = synthetic.generate(date(2025, 6, 1), 60)
    samples = [s for d in data.days for s in parse_power(d.power_rows, data.sys_sn)]
    daily = [parse_energy(d.energy, data.sys_sn, d.day) for d in data.days]
    strict = analyze_data(data.sys_sn, samples, daily, replace(Settings(), min_rel_deviation=0.0))
    default = analyze_data(data.sys_sn, samples, daily, Settings())
    busy = {"2025-06-26", "2025-07-02"}
    assert busy <= {a.day for a in strict.anomalies if a.kind == "consumption"}
    assert not busy & {a.day for a in default.anomalies}
    assert "2025-07-24" in {a.day for a in default.anomalies if a.kind == "consumption"}


def test_baseline_trend_ignores_single_outlier() -> None:
    from energy_analyzer.analyze import _trend_per_30d
    from energy_analyzer.models import DayStats

    days = [
        DayStats(f"2025-06-{d:02d}", 150.0 + (400.0 if d == 25 else 0.0), None,
                 None, None, None, None, None, None)
        for d in range(1, 31)
    ]  # fmt: skip
    trend = _trend_per_30d(days)
    assert trend is not None and abs(trend) < 1e-9
