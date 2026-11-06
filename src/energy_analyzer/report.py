"""Render an AnalysisResult as one self-contained, offline HTML5 report.

Everything is inline: CSS in a <style> block, charts as inline SVG generated here. No scripts,
no external resources, stdlib only. Every dynamic string goes through html.escape.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Callable, Sequence
from datetime import date, datetime
from html import escape
from itertools import pairwise
from pathlib import Path

from energy_analyzer.fetch import mask_sn
from energy_analyzer.models import AnalysisResult, Anomaly, DayStats, MonthStats

DASH = chr(0x2013)  # en dash, shown for missing values
NOTE = "Unofficial community tool, not affiliated with Alpha ESS. Data stays on this machine."

# chart geometry (viewBox units)
_W = 900.0
_H = 320.0
_PAD_L = 56.0
_PAD_R = 16.0
_PAD_T = 36.0
_PAD_B = 40.0
_MAX_X_LABELS = 8

_CSS = """
:root {
  --bg: #f7f7f5; --fg: #1d1f21; --muted: #5f6368; --card: #ffffff; --border: #dadce0;
  --grid: #e8eaed; --pv: #e8a317; --load: #3a6ea5; --baseline: #2e7d5b; --trend: #c0392b;
  --warn-bg: #fff4e5;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #15171a; --fg: #e8eaed; --muted: #9aa0a6; --card: #1f2226; --border: #3c4043;
    --grid: #2c3035; --pv: #f2b84b; --load: #6fa3dc; --baseline: #5cc49a; --trend: #ef7b6c;
    --warn-bg: #3a2f1c;
  }
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0; background: var(--bg); color: var(--fg);
  font: 15px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
}
main { max-width: 980px; margin: 0 auto; padding: 16px; }
header h1 { margin: 8px 0 4px; font-size: 1.6rem; }
header p { margin: 0; color: var(--muted); }
section {
  background: var(--card); border: 1px solid var(--border); border-radius: 8px;
  padding: 12px 16px; margin: 16px 0;
}
h2 { margin: 4px 0 12px; font-size: 1.2rem; }
.kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 8px; }
.kpi { border: 1px solid var(--border); border-radius: 6px; padding: 8px 10px; }
.kpi .label { color: var(--muted); font-size: 0.85rem; }
.kpi .value { font-size: 1.15rem; font-weight: 600; font-variant-numeric: tabular-nums; }
.table-wrap { overflow-x: auto; -webkit-overflow-scrolling: touch; }
table { border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }
th, td { padding: 6px 8px; border-bottom: 1px solid var(--border); text-align: left;
  white-space: nowrap; }
td.num, th.num { text-align: right; }
td.msg { white-space: normal; min-width: 220px; }
tr.empty td { color: var(--muted); font-style: italic; }
svg.chart { display: block; width: 100%; height: auto; }
svg.chart text { fill: var(--muted); font-size: 12px; font-family: inherit; }
svg.chart .axis { stroke: var(--muted); stroke-width: 1; }
svg.chart .grid { stroke: var(--grid); stroke-width: 1; }
svg.chart .bar-pv, svg.chart .sw-pv { fill: var(--pv); }
svg.chart .bar-load, svg.chart .sw-load { fill: var(--load); }
svg.chart .line-baseline { fill: none; stroke: var(--baseline); stroke-width: 2; }
svg.chart .dot-baseline, svg.chart .sw-baseline { fill: var(--baseline); }
svg.chart .line-trend { fill: none; stroke: var(--trend); stroke-width: 2;
  stroke-dasharray: 6 4; }
svg.chart .sw-trend { fill: var(--trend); }
footer { color: var(--muted); font-size: 0.85rem; padding: 8px 0 24px; }
footer dl { display: grid; grid-template-columns: max-content 1fr; gap: 2px 12px; margin: 8px 0; }
footer dt { font-weight: 600; }
footer dd { margin: 0; overflow-wrap: anywhere; }
""".strip()


# --- formatting --------------------------------------------------------------------------------


def _e(value: object) -> str:
    return escape(str(value), quote=True)


def _num(value: float | None, digits: int = 1, unit: str = "") -> str:
    if value is None or not math.isfinite(value):
        return DASH
    text = f"{value:,.{digits}f}"
    return f"{text} {unit}" if unit else text


def _pct(share: float | None, digits: int = 1) -> str:
    if share is None or not math.isfinite(share):
        return DASH
    return f"{share * 100:.{digits}f} %"


def _signed(value: float | None, digits: int = 1, unit: str = "") -> str:
    if value is None or not math.isfinite(value):
        return DASH
    text = f"{value:+,.{digits}f}"
    return f"{text} {unit}" if unit else text


def _c(value: float) -> str:
    """Coordinate for SVG attributes."""
    return f"{value:.1f}"


def _anomaly_value(kind: str, value: float) -> str:
    if kind in ("consumption", "not_lower"):
        return _num(value, 2, "kWh")
    if kind in ("baseline", "night_spike"):
        return _num(value, 0, "W")
    return _num(value, 2)


# --- aggregates --------------------------------------------------------------------------------


def _sum(values: Sequence[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return sum(present) if present else None


def _mean(values: Sequence[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return statistics.fmean(present) if present else None


def _current_baseline(days: Sequence[DayStats], window: int = 7) -> float | None:
    recent = [d.baseline_w for d in days if d.baseline_w is not None][-window:]
    return statistics.median(recent) if recent else None


# --- chart helpers -----------------------------------------------------------------------------


def _nice_step(span: float, count: int) -> float:
    raw = span / max(count, 1)
    if raw <= 0 or not math.isfinite(raw):
        return 1.0
    mag = 10.0 ** math.floor(math.log10(raw))
    for mult in (1.0, 2.0, 2.5, 5.0, 10.0):
        if raw <= mult * mag:
            return mult * mag
    return 10.0 * mag


def _ticks(lo: float, hi: float, count: int = 5) -> list[float]:
    if not (math.isfinite(lo) and math.isfinite(hi)):
        lo, hi = 0.0, 1.0
    if hi <= lo:
        pad = abs(hi) * 0.1 or 1.0
        lo, hi = lo - pad, hi + pad
    step = _nice_step(hi - lo, count)
    start = math.floor(lo / step) * step
    end = math.ceil(hi / step) * step
    n = round((end - start) / step)
    return [start + i * step for i in range(n + 1)]


def _tick_label(value: float, step: float) -> str:
    digits = 0 if step >= 1 else min(3, math.ceil(-math.log10(step)))
    return f"{value:,.{digits}f}"


def _label_indices(n: int) -> list[int]:
    if n <= 0:
        return []
    every = max(1, math.ceil(n / _MAX_X_LABELS))
    idx = list(range(0, n, every))
    if idx[-1] != n - 1 and n - 1 - idx[-1] >= every / 2:
        idx.append(n - 1)
    return idx


def _short_day(day: str) -> str:
    return day[5:] if len(day) == 10 else day


def _svg_open(label: str) -> list[str]:
    return [
        f'<svg class="chart" viewBox="0 0 {_W:.0f} {_H:.0f}" role="img" aria-label="{_e(label)}">',
        f"<title>{_e(label)}</title>",
    ]


def _svg_empty(label: str) -> str:
    parts = _svg_open(label)
    parts.append(
        f'<text x="{_c(_W / 2)}" y="{_c(_H / 2)}" text-anchor="middle">'
        "No data for this period</text>"
    )
    parts.append("</svg>")
    return "\n".join(parts)


def _y_axis(ticks: list[float], y_of: Callable[[float], float], unit: str) -> list[str]:
    step = ticks[1] - ticks[0] if len(ticks) > 1 else 1.0
    out: list[str] = []
    for t in ticks:
        y = y_of(t)
        out.append(
            f'<line class="grid" x1="{_c(_PAD_L)}" y1="{_c(y)}" '
            f'x2="{_c(_W - _PAD_R)}" y2="{_c(y)}" />'
        )
        out.append(
            f'<text x="{_c(_PAD_L - 6)}" y="{_c(y + 4)}" text-anchor="end">'
            f"{_e(_tick_label(t, step))}</text>"
        )
    out.append(
        f'<line class="axis" x1="{_c(_PAD_L)}" y1="{_c(_PAD_T)}" '
        f'x2="{_c(_PAD_L)}" y2="{_c(_H - _PAD_B)}" />'
    )
    out.append(
        f'<line class="axis" x1="{_c(_PAD_L)}" y1="{_c(_H - _PAD_B)}" '
        f'x2="{_c(_W - _PAD_R)}" y2="{_c(_H - _PAD_B)}" />'
    )
    out.append(
        f'<text x="{_c(_PAD_L - 6)}" y="{_c(_PAD_T - 12)}" text-anchor="end">{_e(unit)}</text>'
    )
    return out


def _legend(items: list[tuple[str, str]]) -> list[str]:
    out: list[str] = []
    x = _PAD_L + 8
    for cls, text in items:
        out.append(f'<rect class="{cls}" x="{_c(x)}" y="10" width="12" height="12" />')
        out.append(f'<text x="{_c(x + 18)}" y="20">{_e(text)}</text>')
        x += 30 + 7.5 * len(text)
    return out


def _x_labels(days: Sequence[DayStats], x_of: Callable[[int], float]) -> list[str]:
    out: list[str] = []
    base = _H - _PAD_B
    for i in _label_indices(len(days)):
        x = x_of(i)
        out.append(
            f'<line class="axis" x1="{_c(x)}" y1="{_c(base)}" x2="{_c(x)}" y2="{_c(base + 4)}" />'
        )
        out.append(
            f'<text x="{_c(x)}" y="{_c(base + 18)}" text-anchor="middle">'
            f"{_e(_short_day(days[i].day))}</text>"
        )
    return out


def _day_positions(days: Sequence[DayStats]) -> list[float]:
    """Calendar-day offsets from the first day (so gaps show), or plain indices as a fallback."""
    try:
        ords = [date.fromisoformat(d.day).toordinal() for d in days]
    except ValueError:
        return [float(i) for i in range(len(days))]
    if any(b <= a for a, b in pairwise(ords)):
        return [float(i) for i in range(len(days))]
    return [float(o - ords[0]) for o in ords] if ords else []


# --- charts ------------------------------------------------------------------------------------


def daily_chart(days: Sequence[DayStats]) -> str:
    """Grouped bars: consumption (load_kwh) and PV (pv_kwh) per day."""
    label = "Daily consumption and PV generation in kWh"
    values = [v for d in days for v in (d.load_kwh, d.pv_kwh) if v is not None]
    if not days or not values:
        return _svg_empty(label)
    ticks = _ticks(min(0.0, min(values)), max(values))
    lo, hi = ticks[0], ticks[-1]
    plot_w = _W - _PAD_L - _PAD_R
    plot_h = _H - _PAD_T - _PAD_B
    n = len(days)
    group = plot_w / n
    bar = max(group * 0.4, 0.3)

    def y_of(v: float) -> float:
        return _PAD_T + plot_h * (hi - v) / (hi - lo)

    def x_of(i: int) -> float:
        return _PAD_L + group * (i + 0.5)

    parts = _svg_open(label)
    parts += _y_axis(ticks, y_of, "kWh")
    zero = y_of(0.0)
    for i, d in enumerate(days):
        center = x_of(i)
        for cls, v, x in (
            ("bar-load", d.load_kwh, center - bar),
            ("bar-pv", d.pv_kwh, center),
        ):
            if v is None or not math.isfinite(v):
                continue
            top, bottom = sorted((y_of(v), zero))
            parts.append(
                f'<rect class="{cls}" x="{_c(x)}" y="{_c(top)}" width="{bar:.2f}" '
                f'height="{_c(max(bottom - top, 0.0))}"><title>{_e(d.day)}: '
                f"{_e(_num(v, 2, 'kWh'))}</title></rect>"
            )
    parts += _x_labels(days, x_of)
    parts += _legend([("sw-load", "Consumption"), ("sw-pv", "PV")])
    parts.append("</svg>")
    return "\n".join(parts)


def baseline_chart(days: Sequence[DayStats], trend_w_per_30d: float | None) -> str:
    """Daily standby baseline (W) as a line with gaps, plus the least-squares trend line."""
    label = "Daily standby baseline in W with trend line"
    pos = _day_positions(days)
    points: list[tuple[float, float]] = [
        (x, d.baseline_w)
        for x, d in zip(pos, days, strict=True)
        if d.baseline_w is not None and math.isfinite(d.baseline_w)
    ]
    if not points:
        return _svg_empty(label)

    trend: tuple[float, float, float, float] | None = None
    xs = [p[0] for p in points]
    if trend_w_per_30d is not None and math.isfinite(trend_w_per_30d) and max(xs) > min(xs):
        slope = trend_w_per_30d / 30.0
        xm = statistics.fmean(xs)
        ym = statistics.fmean(p[1] for p in points)
        x0, x1 = min(xs), max(xs)
        trend = (x0, ym + slope * (x0 - xm), x1, ym + slope * (x1 - xm))

    ys = [p[1] for p in points]
    if trend is not None:
        ys += [trend[1], trend[3]]
    ticks = _ticks(min(0.0, min(ys)), max(ys))
    lo, hi = ticks[0], ticks[-1]
    plot_w = _W - _PAD_L - _PAD_R
    plot_h = _H - _PAD_T - _PAD_B
    span = pos[-1] if pos and pos[-1] > 0 else 1.0
    inner = 0.5 if len(days) > 1 else 0.0

    def px(x: float) -> float:
        if len(days) == 1:
            return _PAD_L + plot_w / 2
        return _PAD_L + plot_w * (x + inner) / (span + 2 * inner)

    def y_of(v: float) -> float:
        return _PAD_T + plot_h * (hi - v) / (hi - lo)

    parts = _svg_open(label)
    parts += _y_axis(ticks, y_of, "W")

    # split into runs of consecutive days that all have a value
    runs: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = []
    for x, d in zip(pos, days, strict=True):
        v = d.baseline_w
        if v is None or not math.isfinite(v):
            if current:
                runs.append(current)
            current = []
        else:
            current.append((px(x), y_of(v)))
    if current:
        runs.append(current)
    show_dots = len(days) <= 62
    for run in runs:
        if len(run) > 1:
            coords = " ".join(f"{_c(x)},{_c(y)}" for x, y in run)
            parts.append(f'<polyline class="line-baseline" points="{coords}" />')
        if show_dots or len(run) == 1:
            parts += [
                f'<circle class="dot-baseline" cx="{_c(x)}" cy="{_c(y)}" r="2.5" />' for x, y in run
            ]
    if trend is not None:
        parts.append(
            f'<line class="line-trend" x1="{_c(px(trend[0]))}" y1="{_c(y_of(trend[1]))}" '
            f'x2="{_c(px(trend[2]))}" y2="{_c(y_of(trend[3]))}" />'
        )
    parts += _x_labels(days, lambda i: px(pos[i]))
    legend = [("sw-baseline", "Baseline")]
    if trend is not None:
        legend.append(("sw-trend", f"Trend {_signed(trend_w_per_30d, 1, 'W')} / 30 days"))
    parts += _legend(legend)
    parts.append("</svg>")
    return "\n".join(parts)


# --- sections ----------------------------------------------------------------------------------


def _kpi(label: str, value: str) -> str:
    return (
        f'<div class="kpi"><div class="label">{_e(label)}</div>'
        f'<div class="value">{_e(value)}</div></div>'
    )


def _summary(result: AnalysisResult) -> str:
    days = result.days
    if result.first_day and result.last_day:
        period = f"{result.first_day} {DASH} {result.last_day}"
    else:
        period = result.first_day or result.last_day or DASH
    kpis = [
        ("System", mask_sn(result.sys_sn)),
        ("Period", period),
        ("Days", str(len(days))),
        ("PV generation", _num(_sum([d.pv_kwh for d in days]), 1, "kWh")),
        ("Consumption", _num(_sum([d.load_kwh for d in days]), 1, "kWh")),
        ("Grid import", _num(_sum([d.grid_import_kwh for d in days]), 1, "kWh")),
        ("Grid export", _num(_sum([d.grid_export_kwh for d in days]), 1, "kWh")),
        ("Avg self-consumption", _pct(_mean([d.self_consumption for d in days]))),
        ("Avg autarky", _pct(_mean([d.self_sufficiency for d in days]))),
        ("Current baseline (7-day median)", _num(_current_baseline(days), 0, "W")),
        ("Baseline trend per 30 days", _signed(result.baseline_trend_w_per_30d, 1, "W")),
        ("Anomalies (unexpected)", str(sum(a.expected_by is None for a in result.anomalies))),
    ]
    body = "\n".join(_kpi(k, v) for k, v in kpis)
    return (
        f'<section id="summary">\n<h2>Summary</h2>\n<div class="kpis">\n{body}\n</div>\n</section>'
    )


def _anomaly_row(a: Anomaly) -> str:
    return (
        f"<tr><td>{_e(a.day)}</td><td>{_e(a.kind)}</td>"
        f'<td class="num">{_e(_anomaly_value(a.kind, a.value))}</td>'
        f'<td class="num">{_e(_anomaly_value(a.kind, a.reference))}</td>'
        f'<td class="num">{_e(_num(a.score, 2))}</td>'
        f'<td class="msg">{_e(a.message)}'
        f"{_e(f' (expected: {a.expected_by})') if a.expected_by else ''}</td></tr>"
    )


def _anomalies(anomalies: Sequence[Anomaly]) -> str:
    head = (
        '<thead><tr><th>Date</th><th>Kind</th><th class="num">Value</th>'
        '<th class="num">Typical / reference</th><th class="num">Score</th>'
        "<th>Message</th></tr></thead>"
    )
    if anomalies:
        rows = "\n".join(_anomaly_row(a) for a in anomalies)
    else:
        rows = '<tr class="empty"><td colspan="6">No anomalies detected</td></tr>'
    return (
        '<section id="anomalies">\n<h2>Anomalies</h2>\n<div class="table-wrap"><table>\n'
        f"{head}\n<tbody>\n{rows}\n</tbody>\n</table></div>\n</section>"
    )


def _month_row(m: MonthStats) -> str:
    cells = [
        f"<td>{_e(m.month)}</td>",
        f'<td class="num">{_e(m.days)}</td>',
        f'<td class="num">{_e(_num(m.pv_kwh, 1))}</td>',
        f'<td class="num">{_e(_num(m.load_kwh, 1))}</td>',
        f'<td class="num">{_e(_num(m.grid_import_kwh, 1))}</td>',
        f'<td class="num">{_e(_num(m.grid_export_kwh, 1))}</td>',
        f'<td class="num">{_e(_pct(m.self_consumption))}</td>',
        f'<td class="num">{_e(_pct(m.self_sufficiency))}</td>',
        f'<td class="num">{_e(_num(m.baseline_median_w, 0))}</td>',
    ]
    return "<tr>" + "".join(cells) + "</tr>"


def _monthly(months: Sequence[MonthStats]) -> str:
    cols = [
        ("Month", False),
        ("Days", True),
        ("PV kWh", True),
        ("Consumption kWh", True),
        ("Import kWh", True),
        ("Export kWh", True),
        ("Self-consumption", True),
        ("Autarky", True),
        ("Median baseline W", True),
    ]
    head = "".join(
        f'<th class="num">{_e(c)}</th>' if num else f"<th>{_e(c)}</th>" for c, num in cols
    )
    if months:
        rows = "\n".join(_month_row(m) for m in months)
    else:
        rows = f'<tr class="empty"><td colspan="{len(cols)}">No monthly data</td></tr>'
    return (
        '<section id="monthly">\n<h2>Monthly overview</h2>\n<div class="table-wrap"><table>\n'
        f"<thead><tr>{head}</tr></thead>\n<tbody>\n{rows}\n</tbody>\n</table></div>\n</section>"
    )


def _footer(settings: dict[str, str], generated_at: str) -> str:
    if settings:
        items = "\n".join(f"<dt>{_e(k)}</dt><dd>{_e(v)}</dd>" for k, v in sorted(settings.items()))
        block = f"<dl>\n{items}\n</dl>"
    else:
        block = "<p>No analysis settings recorded.</p>"
    return (
        "<footer>\n<p><strong>Analysis settings</strong></p>\n"
        f"{block}\n<p>Generated at {_e(generated_at)}</p>\n<p>{_e(NOTE)}</p>\n</footer>"
    )


# --- public API --------------------------------------------------------------------------------


def render_report(
    result: AnalysisResult, *, title: str = "Energy report", generated_at: str | None = None
) -> str:
    """Return the complete HTML document as a string (LF line endings)."""
    if generated_at is None:
        generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    subtitle = f"System {mask_sn(result.sys_sn)}"
    if result.first_day and result.last_day:
        subtitle += f" · {result.first_day} {DASH} {result.last_day}"
    sections = [
        _summary(result),
        '<section id="daily">\n<h2>Daily consumption and PV</h2>\n'
        f"{daily_chart(result.days)}\n</section>",
        '<section id="baseline">\n<h2>Standby baseline</h2>\n'
        f"{baseline_chart(result.days, result.baseline_trend_w_per_30d)}\n</section>",
        _anomalies(result.anomalies),
        _monthly(result.months),
    ]
    doc = [
        "<!doctype html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        '<meta name="color-scheme" content="light dark">',
        f"<title>{_e(title)}</title>",
        f"<style>\n{_CSS}\n</style>",
        "</head>",
        "<body>",
        "<main>",
        f"<header>\n<h1>{_e(title)}</h1>\n<p>{_e(subtitle)}</p>\n</header>",
        *sections,
        _footer(result.settings, generated_at),
        "</main>",
        "</body>",
        "</html>",
    ]
    return "\n".join(doc) + "\n"


def write_report(
    result: AnalysisResult,
    path: Path,
    *,
    title: str = "Energy report",
    generated_at: str | None = None,
) -> Path:
    """Write the report as UTF-8 with LF line endings, creating parent directories."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    html_text = render_report(result, title=title, generated_at=generated_at)
    path.write_text(html_text, encoding="utf-8", newline="\n")
    return path
