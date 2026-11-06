from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import pytest

from energy_analyzer import cli, synthetic
from energy_analyzer.fake_api import SyntheticTransport

APP_ID, APP_SECRET = "cli-test-id", "cli-test-secret"


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated folder with credentials in .env and nothing from the real environment."""
    for key in (
        "ALPHA_APP_ID",
        "ALPHA_APP_SECRET",
        "ALPHA_SYSTEM_SN",
        "ENERGY_DB",
        "ENERGY_CONFIG",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(f"ALPHA_APP_ID={APP_ID}\nALPHA_APP_SECRET={APP_SECRET}\n")
    return tmp_path


@pytest.fixture
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """Do not really sleep between API calls in CLI tests."""
    monkeypatch.setattr("energy_analyzer.fetch.time.sleep", lambda s: None)


def _transport(days: int = 10) -> SyntheticTransport:
    return SyntheticTransport(synthetic.generate(date(2025, 6, 1), days), APP_ID, APP_SECRET)


def test_init_creates_files_and_checks_credentials(
    workdir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["init"], transport=_transport()) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert (workdir / "config.toml").is_file() and (workdir / ".env.example").is_file()
    assert (workdir / "energy.db").is_file()
    assert "credentials OK: 1 system" in out
    assert synthetic.DEMO_SN not in out and APP_SECRET not in out
    # second run leaves files alone
    assert cli.main(["init", "--no-check"]) == cli.EXIT_OK
    assert "left unchanged" in capsys.readouterr().out


def test_init_without_credentials_explains_next_step(
    workdir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (workdir / ".env").unlink()
    assert cli.main(["init"]) == cli.EXIT_OK
    assert "fill in your credentials" in capsys.readouterr().out


def test_bad_credentials_exit_2(workdir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bad = SyntheticTransport(synthetic.generate(date(2025, 6, 1), 1), APP_ID, "other")
    assert cli.main(["init"], transport=bad) == cli.EXIT_CONFIG
    err = capsys.readouterr().err
    assert "authentication failed" in err and APP_SECRET not in err


def test_backfill_daily_report_flow(
    workdir: Path, fast: None, capsys: pytest.CaptureFixture[str]
) -> None:
    t = _transport(10)
    assert cli.main(["backfill", "--from", "2025-06-01", "--to", "2025-06-08"], transport=t) == 0
    assert cli.main(["daily", "--today", "2025-06-10", "--report", "r.html"], transport=t) == 0
    assert cli.main(["report", "--out", "out/report.html"]) == 0
    html = (workdir / "out" / "report.html").read_text("utf-8")
    assert 'id="anomalies"' in html and not re.search(r"https?://", html)
    assert (workdir / "r.html").is_file()
    out = capsys.readouterr().out
    assert "2025-06-10: 288 power samples" in out
    assert synthetic.DEMO_SN not in out


def test_backfill_rejects_bad_ranges(workdir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["backfill", "--from", "2025-06-05", "--to", "2025-06-01"]) == cli.EXIT_CONFIG
    with pytest.raises(SystemExit):
        cli.main(["backfill", "--from", "June", "--to", "2025-06-01"])


def test_report_without_data_is_a_clear_error(
    workdir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["report"]) == cli.EXIT_CONFIG
    assert "backfill" in capsys.readouterr().err


def test_live_masks_serial(workdir: Path, capsys: pytest.CaptureFixture[str], fast: None) -> None:
    assert cli.main(["live"], transport=_transport(1)) == 0
    out = capsys.readouterr().out
    assert "SoC" in out and synthetic.DEMO_SN not in out


def test_selftest_passes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["selftest", "--keep", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "selftest: OK" in out and "FAIL" not in out
    assert (tmp_path / "selftest-report.html").is_file()
