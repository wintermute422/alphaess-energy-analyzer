"""Plain data types shared by fetch, storage, analysis and report."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PowerSample:
    """One point of the intraday power series (W), timestamp in the system's local time."""

    sys_sn: str
    ts: str  # "YYYY-MM-DD HH:MM:SS"
    pv_w: float | None
    load_w: float | None
    grid_import_w: float | None
    grid_export_w: float | None
    battery_charge_w: float | None
    battery_discharge_w: float | None
    soc: float | None

    @property
    def day(self) -> str:
        return self.ts[:10]


@dataclass(frozen=True)
class DailyEnergy:
    """Daily energy totals (kWh). load_kwh is derived by energy balance."""

    sys_sn: str
    day: str  # "YYYY-MM-DD"
    pv_kwh: float | None
    load_kwh: float | None
    grid_import_kwh: float | None
    grid_export_kwh: float | None
    battery_charge_kwh: float | None
    battery_discharge_kwh: float | None
    battery_grid_charge_kwh: float | None
    ev_kwh: float | None


# --- analysis results (consumed by report.py) -------------------------------------------------


@dataclass(frozen=True)
class DayStats:
    day: str
    baseline_w: float | None  # median night load with PV == 0
    night_max_w: float | None
    pv_kwh: float | None
    load_kwh: float | None
    grid_import_kwh: float | None
    grid_export_kwh: float | None
    self_consumption: float | None  # share of PV used on site, 0..1
    self_sufficiency: float | None  # share of load not covered by the grid (autarky), 0..1


@dataclass(frozen=True)
class Event:
    """Something the user told us about: a holiday, a heating season change, an outage, ...

    Range events cover start_day..end_day (end_day None = that single day). Events with the
    "regime_change" effect mark a point in time instead: from start_day on, days are only
    compared with days after the change.
    """

    id: int | None
    kind: str
    title: str
    start_day: str
    end_day: str | None
    effect: str
    note: str = ""

    def covers(self, day: str) -> bool:
        return self.start_day <= day <= (self.end_day or self.start_day)


@dataclass(frozen=True)
class Anomaly:
    day: str
    kind: str  # "baseline" | "consumption" | "night_spike" | "not_lower"
    value: float
    reference: float  # rolling median (or baseline for spikes)
    score: float  # robust z-score, or spike ratio
    message: str
    expected_by: str | None = None  # title of the event that explains it, if any


@dataclass(frozen=True)
class MonthStats:
    month: str  # "YYYY-MM"
    days: int
    pv_kwh: float
    load_kwh: float
    grid_import_kwh: float
    grid_export_kwh: float
    self_consumption: float | None
    self_sufficiency: float | None
    baseline_median_w: float | None


@dataclass(frozen=True)
class AnalysisResult:
    sys_sn: str
    first_day: str | None
    last_day: str | None
    days: list[DayStats] = field(default_factory=list)
    anomalies: list[Anomaly] = field(default_factory=list)
    months: list[MonthStats] = field(default_factory=list)
    baseline_trend_w_per_30d: float | None = None  # least-squares slope of daily baseline
    settings: dict[str, str] = field(default_factory=dict)  # thresholds used, for the report
    events: list[Event] = field(default_factory=list)  # events overlapping the period
