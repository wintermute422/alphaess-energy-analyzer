"""Daily statistics, monthly summaries and anomaly detection on stored data.

Pure functions over the plain data types in models.py; `analyze()` is the thin storage wrapper.

- Standby baseline: median load of the night-window samples with PV == 0.
- Anomalies: robust z-score (median / MAD) of the daily baseline and the daily consumption
  against the previous `rolling_days` calendar days, plus short night spikes above the baseline.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Iterable
from datetime import date, timedelta

from energy_analyzer.config import Settings
from energy_analyzer.models import (
    AnalysisResult,
    Anomaly,
    DailyEnergy,
    DayStats,
    MonthStats,
    PowerSample,
)
from energy_analyzer.storage import Storage

MIN_BASELINE_SAMPLES = 3
MAD_SCALE = 0.6745  # Phi^-1(0.75): makes MAD comparable to a standard deviation
MEAN_AD_SCALE = 1.253314  # sqrt(pi / 2): same for the mean absolute deviation


def _parse_hhmm(text: str) -> int:
    """'HH:MM' (or 'HH:MM:SS') -> seconds since midnight."""
    parts = text.strip().split(":")
    try:
        if len(parts) not in (2, 3):
            raise ValueError
        hh, mm = int(parts[0]), int(parts[1])
        ss = int(parts[2]) if len(parts) == 3 else 0
    except ValueError:
        raise ValueError(f"invalid time of day {text!r}, expected HH:MM") from None
    if not (0 <= hh <= 23 and 0 <= mm <= 59 and 0 <= ss <= 59):
        raise ValueError(f"invalid time of day {text!r}, expected HH:MM")
    return hh * 3600 + mm * 60 + ss


def _seconds_of_day(ts: str) -> int:
    return int(ts[11:13]) * 3600 + int(ts[14:16]) * 60 + int(ts[17:19] or 0)


def _in_window(seconds: int, start: int, end: int) -> bool:
    if start <= end:
        return start <= seconds < end
    return seconds >= start or seconds < end  # window wraps midnight


def _clamp01(value: float) -> float:
    return min(1.0, max(0.0, value))


def _ratio_self_consumption(pv: float | None, export: float | None) -> float | None:
    if pv is None or pv <= 0:
        return None
    return _clamp01((pv - (export or 0.0)) / pv)


def _ratio_self_sufficiency(load: float | None, imp: float | None) -> float | None:
    if load is None or load <= 0:
        return None
    return _clamp01(1.0 - (imp or 0.0) / load)


def _robust_z(x: float, ref: list[float]) -> tuple[float, float] | None:
    """Return (z, median) of x against ref, or None if the spread is zero."""
    med = statistics.median(ref)
    mad = statistics.median(abs(v - med) for v in ref)
    if mad > 0:
        return MAD_SCALE * (x - med) / mad, med
    mean_ad = statistics.fmean(abs(v - med) for v in ref)
    if mean_ad > 0:
        return (x - med) / (MEAN_AD_SCALE * mean_ad), med
    return None


def _night_samples(
    samples: Iterable[PowerSample], start: int, end: int
) -> dict[str, list[PowerSample]]:
    out: dict[str, list[PowerSample]] = defaultdict(list)
    for s in samples:
        if s.load_w is not None and _in_window(_seconds_of_day(s.ts), start, end):
            out[s.day].append(s)
    return out


def _baseline(night: list[PowerSample]) -> float | None:
    loads = [s.load_w for s in night if s.pv_w is not None and s.pv_w == 0 and s.load_w is not None]
    if len(loads) < MIN_BASELINE_SAMPLES:
        return None
    return float(statistics.median(loads))


def _night_max(night: list[PowerSample]) -> float | None:
    loads = [s.load_w for s in night if s.pv_w is not None and s.pv_w == 0 and s.load_w is not None]
    return max(loads) if loads else None


def _rolling_anomalies(
    stats: list[DayStats], kind: str, values: dict[str, float], settings: Settings
) -> list[Anomaly]:
    out: list[Anomaly] = []
    for ds in stats:
        x = values.get(ds.day)
        if x is None:
            continue
        today = date.fromisoformat(ds.day)
        ref = [
            v
            for i in range(1, settings.rolling_days + 1)
            if (v := values.get((today - timedelta(days=i)).isoformat())) is not None
        ]
        if len(ref) < max(1, settings.min_history_days):
            continue
        res = _robust_z(x, ref)
        if res is None:
            continue
        z, med = res
        if abs(z) <= settings.z_threshold:
            continue
        # Effect-size floor: with a very stable history the MAD is tiny and a statistically
        # unusual day can still be practically normal (e.g. +10 % consumption).
        if med and abs(x - med) / abs(med) < settings.min_rel_deviation:
            continue
        if kind == "baseline":
            msg = f"Standby load {x:.0f} W vs typical {med:.0f} W"
        else:
            msg = f"Consumption {x:.1f} kWh vs typical {med:.1f} kWh"
        out.append(Anomaly(ds.day, kind, x, med, z, msg))
    return out


def _night_spike(
    day: str, night: list[PowerSample], baseline: float, s: Settings
) -> Anomaly | None:
    if baseline <= 0:
        return None
    hits = [
        p
        for p in night
        if p.load_w is not None
        and p.load_w > s.spike_factor * baseline
        and p.load_w - baseline >= s.spike_min_w
    ]
    if not hits:
        return None
    top = max(hits, key=lambda p: (p.load_w or 0.0, p.ts))
    load = float(top.load_w or 0.0)
    msg = f"Night load {load:.0f} W at {top.ts[11:16]} vs standby {baseline:.0f} W"
    return Anomaly(day, "night_spike", load, baseline, load / baseline, msg)


def _months(stats: list[DayStats]) -> list[MonthStats]:
    groups: dict[str, list[DayStats]] = defaultdict(list)
    for ds in stats:
        groups[ds.day[:7]].append(ds)
    out: list[MonthStats] = []
    for month in sorted(groups):
        rows = groups[month]
        pv = sum(d.pv_kwh for d in rows if d.pv_kwh is not None)
        load = sum(d.load_kwh for d in rows if d.load_kwh is not None)
        imp = sum(d.grid_import_kwh for d in rows if d.grid_import_kwh is not None)
        exp = sum(d.grid_export_kwh for d in rows if d.grid_export_kwh is not None)
        baselines = [d.baseline_w for d in rows if d.baseline_w is not None]
        out.append(
            MonthStats(
                month=month,
                days=len(rows),
                pv_kwh=float(pv),
                load_kwh=float(load),
                grid_import_kwh=float(imp),
                grid_export_kwh=float(exp),
                self_consumption=_ratio_self_consumption(pv, exp),
                self_sufficiency=_ratio_self_sufficiency(load, imp),
                baseline_median_w=float(statistics.median(baselines)) if baselines else None,
            )
        )
    return out


def _trend_per_30d(stats: list[DayStats]) -> float | None:
    points = [
        (float(date.fromisoformat(d.day).toordinal()), d.baseline_w)
        for d in stats
        if d.baseline_w is not None
    ]
    if len(points) < 2:
        return None
    mx = statistics.fmean(x for x, _ in points)
    my = statistics.fmean(y for _, y in points)
    sxx = sum((x - mx) ** 2 for x, _ in points)
    if sxx == 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in points)
    return sxy / sxx * 30.0


def _settings_dict(s: Settings) -> dict[str, str]:
    return {
        "night_start": s.night_start,
        "night_end": s.night_end,
        "night_window": f"{s.night_start}-{s.night_end}",
        "rolling_days": str(s.rolling_days),
        "z_threshold": str(s.z_threshold),
        "min_rel_deviation": str(s.min_rel_deviation),
        "spike_factor": str(s.spike_factor),
        "spike_min_w": str(s.spike_min_w),
        "min_history_days": str(s.min_history_days),
    }


def analyze_data(
    sys_sn: str, samples: list[PowerSample], daily: list[DailyEnergy], settings: Settings
) -> AnalysisResult:
    """Compute per-day stats, anomalies, monthly totals and the baseline trend."""
    start, end = _parse_hhmm(settings.night_start), _parse_hhmm(settings.night_end)
    night = _night_samples(samples, start, end)
    energy = {d.day: d for d in daily}
    days = sorted({s.day for s in samples} | set(energy))

    stats: list[DayStats] = []
    anomalies: list[Anomaly] = []
    for day in days:
        win = night.get(day, [])
        base = _baseline(win)
        e = energy.get(day)
        pv = e.pv_kwh if e else None
        load = e.load_kwh if e else None
        imp = e.grid_import_kwh if e else None
        exp = e.grid_export_kwh if e else None
        stats.append(
            DayStats(
                day=day,
                baseline_w=base,
                night_max_w=_night_max(win),
                pv_kwh=pv,
                load_kwh=load,
                grid_import_kwh=imp,
                grid_export_kwh=exp,
                self_consumption=_ratio_self_consumption(pv, exp),
                self_sufficiency=_ratio_self_sufficiency(load, imp),
            )
        )
        if base is not None:
            spike = _night_spike(day, win, base, settings)
            if spike is not None:
                anomalies.append(spike)

    baselines = {d.day: d.baseline_w for d in stats if d.baseline_w is not None}
    loads = {d.day: d.load_kwh for d in stats if d.load_kwh is not None}
    anomalies += _rolling_anomalies(stats, "baseline", baselines, settings)
    anomalies += _rolling_anomalies(stats, "consumption", loads, settings)
    anomalies.sort(key=lambda a: (a.day, a.kind))

    return AnalysisResult(
        sys_sn=sys_sn,
        first_day=days[0] if days else None,
        last_day=days[-1] if days else None,
        days=stats,
        anomalies=anomalies,
        months=_months(stats),
        baseline_trend_w_per_30d=_trend_per_30d(stats),
        settings=_settings_dict(settings),
    )


def analyze(
    storage: Storage,
    sys_sn: str,
    settings: Settings,
    first_day: str | None = None,
    last_day: str | None = None,
) -> AnalysisResult:
    """Load the stored series of one system (optionally a day range) and analyse it."""
    samples = storage.power_range(sys_sn, first_day, last_day)
    daily = storage.daily_range(sys_sn, first_day, last_day)
    return analyze_data(sys_sn, samples, daily, settings)
