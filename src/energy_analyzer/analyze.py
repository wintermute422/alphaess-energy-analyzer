"""Daily statistics, monthly summaries and anomaly detection on stored data.

Pure functions over the plain data types in models.py; `analyze()` is the thin storage wrapper.

- Standby baseline: median load of the night-window samples with PV == 0.
- Anomalies: robust z-score (median / MAD) of the daily baseline and the daily consumption
  against the previous `rolling_days` calendar days, plus night spikes that are high against
  the baseline AND unusual against previous nights' peaks.
- Events (events.py) exclude days from references, start new regimes and explain anomalies.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import replace
from datetime import date, timedelta

from energy_analyzer.config import Settings
from energy_analyzer.events import EventIndex
from energy_analyzer.models import (
    AnalysisResult,
    Anomaly,
    DailyEnergy,
    DayStats,
    Event,
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


def _reference(
    day: str, values: dict[str, float], settings: Settings, index: EventIndex
) -> list[float]:
    """Values of the previous rolling_days days that may serve as "normal" for day: not inside
    a holiday/guests/outage period and not before the latest regime change (e.g. heating on)."""
    today = date.fromisoformat(day)
    since = index.regime_start(day)
    ref = []
    for i in range(1, settings.rolling_days + 1):
        d = (today - timedelta(days=i)).isoformat()
        if since is not None and d < since:
            break
        v = values.get(d)
        if v is not None and not index.excluded_from_reference(d):
            ref.append(v)
    return ref


def _rolling_anomalies(
    stats: list[DayStats],
    kind: str,
    values: dict[str, float],
    settings: Settings,
    index: EventIndex,
) -> list[Anomaly]:
    out: list[Anomaly] = []
    for ds in stats:
        x = values.get(ds.day)
        if x is None or index.ignored(ds.day):
            continue
        ref = _reference(ds.day, values, settings, index)
        if len(ref) < max(1, settings.min_history_days):
            continue
        med = float(statistics.median(ref))
        res = _robust_z(x, ref)
        unit, fmt = ("W", ".0f") if kind == "baseline" else ("kWh", ".1f")
        label = "Standby load" if kind == "baseline" else "Consumption"
        flagged = False
        if res is not None:
            z = res[0]
            # Effect-size floor: with a very stable history the MAD is tiny and a statistically
            # unusual day can still be practically normal (e.g. +10 % consumption).
            big = not med or abs(x - med) / abs(med) >= settings.min_rel_deviation
            if abs(z) > settings.z_threshold and big:
                flagged = True
                why = index.expected(ds.day, 1 if x > med else -1)
                msg = f"{label} {x:{fmt}} {unit} vs typical {med:{fmt}} {unit}"
                out.append(Anomaly(ds.day, kind, x, med, z, msg, why.title if why else None))
        away = index.expected(ds.day, -1)
        not_lower = med > 0 and x >= med * (1.0 - settings.min_rel_deviation)
        if kind == "consumption" and away is not None and not flagged and not_lower:
            z = res[0] if res is not None else 0.0
            msg = (
                f"Consumption {x:.1f} kWh during {away.title!r}: expected lower than the "
                f"typical {med:.1f} kWh"
            )
            out.append(Anomaly(ds.day, "not_lower", x, med, z, msg))
    return out


def _night_peaks(night: dict[str, list[PowerSample]]) -> dict[str, float]:
    peaks: dict[str, float] = {}
    for day, win in night.items():
        loads = [p.load_w for p in win if p.load_w is not None]
        if loads:
            peaks[day] = float(max(loads))
    return peaks


def _night_spike(
    day: str,
    night: list[PowerSample],
    baseline: float,
    peaks: dict[str, float],
    s: Settings,
    index: EventIndex,
) -> Anomaly | None:
    """A night load far above the standby baseline that is ALSO unusual compared with the
    night peaks of the reference days. A heat pump or storage heater that starts every night
    is part of the household's normal pattern and is not flagged once enough history exists;
    without history the baseline rule alone decides."""
    if baseline <= 0 or index.ignored(day):
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
    ref = _reference(day, peaks, s, index)
    msg = f"Night load {load:.0f} W at {top.ts[11:16]} vs standby {baseline:.0f} W"
    if len(ref) >= max(1, s.min_history_days):
        typical = float(statistics.median(ref))
        if load - typical < s.spike_min_w:
            return None
        res = _robust_z(load, ref)
        if res is not None and res[0] <= s.z_threshold:
            return None
        msg += f" (typical night peak {typical:.0f} W)"
    why = index.expected(day, 1)
    return Anomaly(
        day, "night_spike", load, baseline, load / baseline, msg, why.title if why else None
    )


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
    # Theil-Sen estimator (median of pairwise slopes): a single anomalous day must not tilt
    # the trend the way it does with least squares.
    slopes = [
        (y2 - y1) / (x2 - x1)
        for i, (x1, y1) in enumerate(points)
        for x2, y2 in points[i + 1 :]
        if x2 != x1
    ]
    if not slopes:
        return None
    return statistics.median(slopes) * 30.0


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
    sys_sn: str,
    samples: list[PowerSample],
    daily: list[DailyEnergy],
    settings: Settings,
    events: list[Event] | None = None,
) -> AnalysisResult:
    """Compute per-day stats, anomalies, monthly totals and the baseline trend."""
    start, end = _parse_hhmm(settings.night_start), _parse_hhmm(settings.night_end)
    index = EventIndex(events)
    night = _night_samples(samples, start, end)
    peaks = _night_peaks(night)
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
            spike = _night_spike(day, win, base, peaks, settings, index)
            if spike is not None:
                anomalies.append(spike)

    baselines = {d.day: d.baseline_w for d in stats if d.baseline_w is not None}
    loads = {d.day: d.load_kwh for d in stats if d.load_kwh is not None}
    anomalies += _rolling_anomalies(stats, "baseline", baselines, settings, index)
    anomalies += _rolling_anomalies(stats, "consumption", loads, settings, index)
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
        events=sorted(events or [], key=lambda e: (e.start_day, e.id or 0)),
    )


def _overlaps(e: Event, lo: str, hi: str) -> bool:
    if e.effect == "regime_change":
        return e.start_day <= hi
    return e.start_day <= hi and (e.end_day or e.start_day) >= lo


def trim(result: AnalysisResult, first_day: str | None, last_day: str | None) -> AnalysisResult:
    """Restrict a result to a day range (months and trend are recomputed for the range)."""
    lo, hi = first_day or "", last_day or "9999"
    days = [d for d in result.days if lo <= d.day <= hi]
    return replace(
        result,
        first_day=days[0].day if days else None,
        last_day=days[-1].day if days else None,
        days=days,
        anomalies=[a for a in result.anomalies if lo <= a.day <= hi],
        months=_months(days),
        baseline_trend_w_per_30d=_trend_per_30d(days),
        events=[e for e in result.events if _overlaps(e, lo, hi)],
    )


def analyze(
    storage: Storage,
    sys_sn: str,
    settings: Settings,
    first_day: str | None = None,
    last_day: str | None = None,
) -> AnalysisResult:
    """Load the stored series of one system (optionally a day range) and analyse it.

    Days before first_day are loaded as history, so the rolling references at the start of the
    range are the same as in a full analysis; the result is then trimmed to the range."""
    load_from = first_day
    if first_day:
        history = timedelta(days=settings.rolling_days)
        load_from = (date.fromisoformat(first_day) - history).isoformat()
    samples = storage.power_range(sys_sn, load_from, last_day)
    daily = storage.daily_range(sys_sn, load_from, last_day)
    result = analyze_data(sys_sn, samples, daily, settings, storage.events())
    return trim(result, first_day, last_day) if first_day or last_day else result
