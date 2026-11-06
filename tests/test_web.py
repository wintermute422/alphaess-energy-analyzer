"""Dashboard server: API, auth, same-origin checks and static file whitelist."""

from __future__ import annotations

import base64
import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from energy_analyzer import cli, synthetic
from energy_analyzer.config import ConfigError, Settings
from energy_analyzer.demo import DEMO_APP_ID, DEMO_APP_SECRET, build_demo
from energy_analyzer.fake_api import SyntheticTransport
from energy_analyzer.web import App, is_loopback, make_server

TODAY = date(2026, 3, 1)


@pytest.fixture(scope="module")
def demo_db(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Any]:
    path = tmp_path_factory.mktemp("web") / "demo.db"
    transport = build_demo(path, today=TODAY)
    return path, transport


def _start(app: App) -> tuple[str, Any]:
    server = make_server(app, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}", server


@pytest.fixture
def base(demo_db: tuple[Path, Any]) -> Iterator[str]:
    path, transport = demo_db
    settings = replace(Settings(), db_path=path, app_id=DEMO_APP_ID, app_secret=DEMO_APP_SECRET)
    url, server = _start(App(settings, transport=transport, demo=True))
    yield url
    server.shutdown()
    server.server_close()


def call(
    url: str, method: str = "GET", body: Any = None, headers: dict[str, str] | None = None
) -> tuple[int, dict[str, str], Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            status, hdrs = resp.status, dict(resp.headers)
    except urllib.error.HTTPError as exc:
        raw, status, hdrs = exc.read(), exc.code, dict(exc.headers)
    ctype = hdrs.get("Content-Type", "")
    return status, hdrs, json.loads(raw) if raw and "json" in ctype else raw


def write_headers(base: str) -> dict[str, str]:
    return {"Content-Type": "application/json", "Origin": base}


def test_meta_and_static(base: str) -> None:
    status, hdrs, meta = call(f"{base}/api/meta")
    assert status == 200 and meta["demo"] is True
    assert meta["system"] == "DEM*********0001"  # masked
    assert meta["first_day"] == "2025-11-01" and meta["last_day"] == "2026-02-28"
    assert {k["kind"] for k in meta["event_kinds"]} >= {"away", "heating_on", "outage"}
    assert "default-src 'self'" in hdrs["Content-Security-Policy"]
    assert hdrs["X-Frame-Options"] == "DENY"
    status, hdrs, _ = call(f"{base}/")
    assert status == 200 and hdrs["Content-Type"].startswith("text/html")


def test_summary_day_heatmap(base: str) -> None:
    _, _, s = call(f"{base}/api/summary")
    assert s["to"] == "2026-02-28" and s["kpis"]["days"] == 30
    _, _, s = call(f"{base}/api/summary?from=2025-11-01&to=2026-02-28")
    kinds = {(a["day"], a["kind"]) for a in s["anomalies"]}
    assert ("2026-01-10", "baseline") in kinds  # demo offset 70
    expected = [a for a in s["anomalies"] if a["expected_by"]]
    assert expected and {a["expected_by"] for a in expected} <= {"Holiday", "Family visit"}
    assert len(s["months"]) == 4
    _, _, d = call(f"{base}/api/day?date=2026-01-10")
    assert len(d["samples"]) == 288 and d["prev"] == "2026-01-09" and d["next"] == "2026-01-11"
    assert d["totals"]["pv_kwh"] is not None and d["anomalies"]
    _, _, d = call(f"{base}/api/day")
    assert d["date"] == "2026-02-28" and d["next"] is None
    _, _, h = call(f"{base}/api/heatmap")
    assert len(h["days"]) == 60 and len(h["values"][0]) == 24 and h["max"] > 0


def test_bad_input(base: str) -> None:
    assert call(f"{base}/api/day?date=yesterday")[0] == 400
    assert call(f"{base}/api/summary?from=2026-02-01&to=2026-01-01")[0] == 400
    assert call(f"{base}/api/nope")[0] == 404
    assert call(f"{base}/../pyproject.toml")[0] == 404
    assert call(f"{base}/web.py")[0] == 404


def test_live(base: str) -> None:
    _, _, live = call(f"{base}/api/live")
    assert live["available"] is True and live["soc"] == 72.5


def test_event_crud_and_effect_on_analysis(base: str) -> None:
    body = {"kind": "outage", "title": "Inverter firmware", "start_day": "2026-01-10"}
    status, _, created = call(f"{base}/api/events", "POST", body, write_headers(base))
    assert status == 201 and created["event"]["effect"] == "ignore"
    eid = created["event"]["id"]
    _, _, s = call(f"{base}/api/summary?from=2026-01-10&to=2026-01-10")
    assert s["anomalies"] == []  # the outage day is no longer judged
    body["end_day"] = "2026-01-11"
    status, _, updated = call(f"{base}/api/events/{eid}", "PUT", body, write_headers(base))
    assert status == 200 and updated["event"]["end_day"] == "2026-01-11"
    assert call(f"{base}/api/events/{eid}", "DELETE", None, {"Origin": base})[0] == 204
    assert call(f"{base}/api/events/{eid}", "DELETE", None, {"Origin": base})[0] == 404
    _, _, s = call(f"{base}/api/summary?from=2026-01-10&to=2026-01-10")
    assert s["anomalies"]


def test_event_validation_and_csrf(base: str) -> None:
    bad = {"kind": "away", "start_day": "2026-02-10", "end_day": "2026-02-01"}
    assert call(f"{base}/api/events", "POST", bad, write_headers(base))[0] == 400
    ok = {"kind": "guests", "start_day": "2026-02-10"}
    assert call(f"{base}/api/events", "POST", ok, {"Content-Type": "application/json"})[0] == 403
    evil = {"Content-Type": "application/json", "Origin": "http://evil.example"}
    assert call(f"{base}/api/events", "POST", ok, evil)[0] == 403
    assert call(f"{base}/api/events", "POST", ok, {"Origin": base})[0] == 415


def test_report_has_own_csp(base: str) -> None:
    status, hdrs, page = call(f"{base}/report.html")
    assert status == 200 and b'id="anomalies"' in page
    assert "script" not in hdrs["Content-Security-Policy"]


def test_basic_auth(demo_db: tuple[Path, Any]) -> None:
    settings = replace(Settings(), db_path=demo_db[0], web_user="me", web_password="s3cret-pw")
    url, server = _start(App(settings))
    try:
        assert call(f"{url}/api/meta")[0] == 401
        assert "WWW-Authenticate" in call(f"{url}/")[1]

        def auth(user: str, pw: str) -> dict[str, str]:
            return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}

        assert call(f"{url}/api/meta", headers=auth("me", "wrong"))[0] == 401
        assert call(f"{url}/api/meta", headers=auth("you", "s3cret-pw"))[0] == 401
        assert call(f"{url}/api/meta", headers={"Authorization": "Basic !!"})[0] == 401
        status, _, meta = call(f"{url}/api/meta", headers=auth("me", "s3cret-pw"))
        assert status == 200 and meta["live_available"] is False
        _, _, live = call(f"{url}/api/live", headers=auth("me", "s3cret-pw"))
        assert live["available"] is False
    finally:
        server.shutdown()
        server.server_close()


def test_refuses_lan_without_password(tmp_path: Path) -> None:
    app = App(replace(Settings(), db_path=tmp_path / "x.db"))
    with pytest.raises(ConfigError, match="WEB_PASSWORD"):
        make_server(app, "0.0.0.0", 0)
    assert is_loopback("127.0.0.1") and is_loopback("::1") and is_loopback("localhost")
    assert not is_loopback("0.0.0.0") and not is_loopback("") and not is_loopback("192.0.2.10")


def test_empty_database(tmp_path: Path) -> None:
    url, server = _start(App(replace(Settings(), db_path=tmp_path / "empty.db")))
    try:
        _, _, meta = call(f"{url}/api/meta")
        assert meta["first_day"] is None and meta["system"] is None
        _, _, s = call(f"{url}/api/summary")
        assert s["kpis"] is None and s["days"] == []
        assert call(f"{url}/api/day")[0] == 404
        assert call(f"{url}/api/heatmap")[2]["days"] == []
    finally:
        server.shutdown()
        server.server_close()


def test_refresh_fetches_today_without_ess_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for key in (
        "ALPHA_APP_ID",
        "ALPHA_APP_SECRET",
        "ALPHA_SYSTEM_SN",
        "ENERGY_DB",
        "ENERGY_CONFIG",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("energy_analyzer.fetch.time.sleep", lambda s: None)
    (tmp_path / ".env").write_text("ALPHA_APP_ID=a\nALPHA_APP_SECRET=b\n")
    data = synthetic.generate(date.today(), 1)
    transport = SyntheticTransport(data, "a", "b")
    assert cli.main(["refresh"], transport=transport) == 0  # first run resolves the system
    first_calls = [c for _, c in transport.calls]
    assert cli.main(["refresh"], transport=transport) == 0
    second_calls = [c for _, c in transport.calls][len(first_calls) :]
    assert "getEssList" in first_calls and "getEssList" not in second_calls
    assert len(second_calls) == 2
    assert "288 power samples" in capsys.readouterr().out
