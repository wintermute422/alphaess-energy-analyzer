"""End-to-end selftest on synthetic data: fake API -> fetch -> SQLite -> analysis -> report.

No network, no credentials, no local config. Exits non-zero if any step fails.
"""

from __future__ import annotations

import re
import tempfile
from collections.abc import Callable
from datetime import date, timedelta
from itertools import pairwise
from pathlib import Path

from energy_analyzer import synthetic
from energy_analyzer.analyze import analyze
from energy_analyzer.config import Settings
from energy_analyzer.fake_api import FakeClock, SyntheticTransport
from energy_analyzer.fetch import AlphaESSClient, Fetcher, resolve_system
from energy_analyzer.report import write_report
from energy_analyzer.storage import Storage

START = date(2025, 6, 1)
DAYS = 60
REPORT_SECTIONS = ("summary", "daily", "baseline", "anomalies", "monthly")


def run_selftest(keep_dir: Path | None = None, out: Callable[[str], None] = print) -> bool:
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        out(f"  [{'PASS' if ok else 'FAIL'}] {name}{f' - {detail}' if detail else ''}")
        if not ok:
            failures.append(name)

    with tempfile.TemporaryDirectory(prefix="energy-selftest-") as tmp:
        work = keep_dir or Path(tmp)
        work.mkdir(parents=True, exist_ok=True)
        db_path = work / "selftest.db"
        if db_path.exists():
            db_path.unlink()
        out(f"selftest: {DAYS} synthetic days from {START}, workdir {work}")

        data = synthetic.generate(START, DAYS)
        clock = FakeClock()
        transport = SyntheticTransport(data, "selftest-id", "selftest-secret", clock)
        client = AlphaESSClient(
            "selftest-id",
            "selftest-secret",
            base_url="https://selftest.invalid/api",
            transport=transport,
            clock=clock.clock,
            sleep=clock.sleep,
        )
        settings = Settings(db_path=db_path)
        last = START + timedelta(days=DAYS - 1)

        with Storage(db_path) as db:
            sn, info = resolve_system(client, None)
            db.upsert_system(sn, info)
            check("fake API authenticates (getEssList)", sn == synthetic.DEMO_SN)
            fetcher = Fetcher(client, db, sn)
            fetcher.backfill(START, last)
            n_power, n_daily = db.count("power_samples"), db.count("daily_energy")
            check(
                "backfill stores every sample and day",
                (n_power, n_daily) == (DAYS * synthetic.SLOTS_PER_DAY, DAYS),
                f"{n_power} samples, {n_daily} days",
            )
            times = [t for t, _ in transport.calls]
            gaps = [b - a for a, b in pairwise(times)]
            check("API calls spaced >= 10 s", min(gaps) >= 10.0, f"min gap {min(gaps):.0f} s")
            fetcher.backfill(last - timedelta(days=1), last)
            check(
                "re-fetch creates no duplicates",
                (db.count("power_samples"), db.count("daily_energy")) == (n_power, n_daily),
            )
            worst = 0.0
            for d in data.days:
                series = db.integrate_day(sn, d.day.isoformat())
                ref = d.energy["epv"]
                if ref:
                    worst = max(worst, abs(series["pv_w"] - ref) / ref)
            check("power series integrate to daily totals (<1 %)", worst < 0.01, f"{worst:.3%}")

            result = analyze(db, sn, settings)
            flagged = {(a.day, a.kind) for a in result.anomalies}
            expected = {
                ((START + timedelta(days=40)).isoformat(), "baseline"),
                ((START + timedelta(days=47)).isoformat(), "night_spike"),
                ((START + timedelta(days=53)).isoformat(), "consumption"),
            }
            check("injected anomalies detected", expected <= flagged, f"{len(flagged)} flagged")
            extra = {day for day, _ in flagged} - {day for day, _ in expected}
            check("normal days not flagged", not extra, ", ".join(sorted(extra)))
            normal = [s for s in result.days if s.baseline_w is not None][:30]
            truth = data.by_date()
            err = max(abs(s.baseline_w - truth[s.day].baseline_w) for s in normal if s.baseline_w)
            check("baseline matches generated standby load", err < 20.0, f"max error {err:.1f} W")

            report = write_report(result, work / "selftest-report.html", title="Selftest report")
            html = report.read_text("utf-8")
            missing = [s for s in REPORT_SECTIONS if f'id="{s}"' not in html]
            check("report has all sections", not missing, ", ".join(missing))
            check("report has no external URLs", not re.search(r"https?://", html))
            check("report hides the serial", synthetic.DEMO_SN not in html)

    if failures:
        out(f"selftest: FAILED ({len(failures)}): {', '.join(failures)}")
        return False
    out("selftest: OK")
    return True
