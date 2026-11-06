from __future__ import annotations

from datetime import date, timedelta
from html.parser import HTMLParser
from pathlib import Path

import pytest

from energy_analyzer.fetch import mask_sn
from energy_analyzer.models import AnalysisResult, Anomaly, DayStats, MonthStats
from energy_analyzer.report import DASH, render_report, write_report

SN = "DEMO000000000001"
SECTION_IDS = ("summary", "daily", "baseline", "anomalies", "monthly")
VOID = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "source",
    "track",
    "wbr",
}


def _days(n: int, start: date = date(2026, 1, 1)) -> list[DayStats]:
    out = []
    for i in range(n):
        day = (start + timedelta(days=i)).isoformat()
        out.append(
            DayStats(
                day=day,
                baseline_w=None if i in (5, 17) else 180.0 + i * 0.5,
                night_max_w=900.0,
                pv_kwh=None if i == 9 else 8.0 + (i % 7),
                load_kwh=12.0 + (i % 5),
                grid_import_kwh=5.0,
                grid_export_kwh=None if i == 12 else 2.5,
                self_consumption=0.7,
                self_sufficiency=None if i == 3 else 0.6,
            )
        )
    return out


def _result(n_days: int = 40) -> AnalysisResult:
    days = _days(n_days)
    return AnalysisResult(
        sys_sn=SN,
        first_day=days[0].day if days else None,
        last_day=days[-1].day if days else None,
        days=days,
        anomalies=[
            Anomaly("2026-01-10", "baseline", 260.0, 182.0, 4.2, "Standby <b>& jumped"),
            Anomaly("2026-01-20", "consumption", 25.3, 14.0, 3.8, "High daily consumption"),
        ],
        months=[
            MonthStats("2026-01", 31, 300.0, 420.0, 160.0, 80.0, 0.73, 0.62, 190.0),
            MonthStats("2026-02", 9, 90.0, 120.0, 40.0, 25.0, None, None, None),
        ],
        baseline_trend_w_per_30d=15.0,
        settings={"window_days": "28", "z_threshold": "3.5", "night": "00:00-05:00"},
    )


class _BalanceChecker(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.errors: list[str] = []
        self.tags: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))
        if tag not in VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))

    def handle_endtag(self, tag: str) -> None:
        if tag in VOID:
            return
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"unexpected </{tag}>, open: {self.stack[-3:]}")
            return
        self.stack.pop()


def _check(html_text: str) -> _BalanceChecker:
    checker = _BalanceChecker()
    checker.feed(html_text)
    checker.close()
    assert checker.errors == []
    assert checker.stack == []
    return checker


@pytest.fixture
def report() -> str:
    return render_report(_result(), generated_at="2026-02-10T12:00:00+01:00")


def test_sections_present_with_headings(report: str) -> None:
    checker = _check(report)
    ids = [a.get("id") for tag, a in checker.tags if tag == "section"]
    assert ids == list(SECTION_IDS)
    for sid in SECTION_IDS:
        start = report.index(f'<section id="{sid}">')
        end = report.index("</section>", start)
        assert "<h2>" in report[start:end]


def test_two_accessible_svg_charts(report: str) -> None:
    checker = _check(report)
    svgs = [a for tag, a in checker.tags if tag == "svg"]
    assert len(svgs) == 2
    for attrs in svgs:
        assert attrs.get("role") == "img"
        assert attrs.get("aria-label")
    assert report.count("<svg") == 2
    for chunk in report.split("<svg")[1:]:
        assert chunk.split(">", 1)[1].lstrip().startswith("<title>")


def test_fully_offline_no_external_resources(report: str) -> None:
    lowered = report.lower()
    for needle in ("http://", "https://", "<script", "<link", "src=", "@import", "url("):
        assert needle not in lowered, needle


def test_parses_and_tags_balanced(report: str) -> None:
    _check(report)
    assert report.lower().startswith("<!doctype html>")


def test_dynamic_text_escaped(report: str) -> None:
    assert "&lt;b&gt;&amp;" in report
    assert "<b>&" not in report


def test_raw_serial_never_shown(report: str) -> None:
    assert SN not in report
    assert mask_sn(SN) in report


def test_summary_contents(report: str) -> None:
    start = report.index('<section id="summary">')
    summary = report[start : report.index("</section>", start)]
    assert "2026-01-01" in summary and "2026-02-09" in summary
    assert ">40<" in summary  # number of days
    assert "+15.0 W" in summary  # trend per 30 days
    assert ">2<" in summary  # anomaly count


def test_footer_settings_and_note(report: str) -> None:
    footer = report[report.index("<footer>") : report.index("</footer>")]
    assert "z_threshold" in footer and "3.5" in footer
    assert "2026-02-10T12:00:00+01:00" in footer
    assert "Unofficial community tool, not affiliated with Alpha ESS." in footer
    assert "Data stays on this machine." in footer


def test_monthly_table_handles_none(report: str) -> None:
    start = report.index('<section id="monthly">')
    monthly = report[start : report.index("</section>", start)]
    assert "2026-01" in monthly and "2026-02" in monthly
    assert "73.0 %" in monthly
    assert DASH in monthly


def test_empty_result_renders() -> None:
    html_text = render_report(AnalysisResult(sys_sn=SN, first_day=None, last_day=None))
    _check(html_text)
    assert "No anomalies detected" in html_text
    assert SN not in html_text
    for sid in SECTION_IDS:
        assert f'<section id="{sid}">' in html_text


def test_single_day_renders() -> None:
    days = _days(1)
    result = AnalysisResult(SN, days[0].day, days[0].day, days=days, baseline_trend_w_per_30d=2.0)
    html_text = render_report(result, generated_at="x")
    _check(html_text)
    assert html_text.count("<svg") == 2
    assert "No anomalies detected" in html_text


def test_many_days_and_all_none_values() -> None:
    days = _days(400)
    gaps = [
        DayStats(d.day, None, None, None, None, None, None, None, None) if i % 50 < 3 else d
        for i, d in enumerate(days)
    ]
    result = AnalysisResult(SN, gaps[0].day, gaps[-1].day, days=gaps, baseline_trend_w_per_30d=-3.0)
    html_text = render_report(result, generated_at="x")
    _check(html_text)
    blank = [DayStats(d.day, None, None, None, None, None, None, None, None) for d in days[:5]]
    _check(render_report(AnalysisResult(SN, None, None, days=blank), generated_at="x"))


def test_write_report_round_trips(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "dir" / "report.html"
    result = _result()
    written = write_report(result, target, title="My <report>", generated_at="fixed")
    assert written == target
    raw = target.read_bytes()
    assert b"\r\n" not in raw
    text = raw.decode("utf-8")
    assert text.lower().startswith("<!doctype html>")
    assert text == render_report(result, title="My <report>", generated_at="fixed")
    assert "My &lt;report&gt;" in text
