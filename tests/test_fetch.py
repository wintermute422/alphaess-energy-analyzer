from __future__ import annotations

import itertools
import json
import urllib.parse
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from energy_analyzer import synthetic
from energy_analyzer.fake_api import FakeClock, SyntheticTransport
from energy_analyzer.fetch import (
    AlphaESSClient,
    ApiError,
    AuthError,
    Fetcher,
    SystemNotFoundError,
    TransientError,
    mask_sn,
    parse_energy,
    parse_power,
    resolve_system,
    sign,
)
from energy_analyzer.storage import Storage

APP_ID, APP_SECRET = "demo-app", "demo-secret"
DAYS = [date(2025, 6, 1), date(2025, 6, 2), date(2025, 6, 3)]


class FixtureTransport:
    """Serves the recorded-style JSON files in sample_data/ (no network)."""

    def __init__(self, sample_dir: Path) -> None:
        self.dir = sample_dir
        self.calls: list[str] = []
        self.headers: list[dict[str, str]] = []

    def __call__(self, url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        parsed = urllib.parse.urlparse(url)
        endpoint = parsed.path.rsplit("/", 1)[-1]
        params = dict(urllib.parse.parse_qsl(parsed.query))
        self.calls.append(endpoint)
        self.headers.append(headers)
        name = f"{endpoint}.json"
        if "queryDate" in params:
            name = f"{endpoint}_{params['queryDate']}.json"
        path = self.dir / name
        if not path.exists():
            return 200, json.dumps(synthetic.envelope([])).encode()
        return 200, path.read_bytes()


class ScriptedTransport:
    """Returns a scripted sequence of responses or raises scripted exceptions."""

    def __init__(self, script: list[tuple[int, object] | Exception]) -> None:
        self.script = list(script)
        self.calls = 0

    def __call__(self, url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        status, payload = item
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return status, body


def make_client(transport, clock: FakeClock | None = None, **kw) -> AlphaESSClient:
    clock = clock or FakeClock()
    return AlphaESSClient(
        APP_ID,
        APP_SECRET,
        base_url="https://api.invalid/api",
        transport=transport,
        clock=clock.clock,
        sleep=clock.sleep,
        wall_clock=lambda: 1_700_000_000.0,
        **kw,
    )


def test_sign_known_vector() -> None:
    assert sign("demo-app", "demo-secret", "1700000000") == (
        "999e3a1fc3ef6064f3a18800a9117e1be0364caa22443a4a8ef86bc8ad5efde2"
        "748cb47d253dde941962f6de67833cd599a3393c83f19d0ed6dc66bbc6ae67ae"
    )


def test_headers_carry_signature_but_repr_hides_secret(sample_dir: Path) -> None:
    transport = FixtureTransport(sample_dir)
    client = make_client(transport)
    client.get_ess_list()
    h = transport.headers[0]
    assert h["appId"] == APP_ID and h["timeStamp"] == "1700000000"
    assert h["sign"] == sign(APP_ID, APP_SECRET, "1700000000")
    assert APP_SECRET not in repr(client) and APP_SECRET not in json.dumps(h)


def test_min_interval_between_calls(sample_dir: Path) -> None:
    clock = FakeClock()
    inner = FixtureTransport(sample_dir)
    call_times: list[float] = []

    def timed(url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        call_times.append(clock.now)
        return inner(url, headers, timeout)

    client = make_client(timed, clock)
    for _ in range(4):
        client.get_ess_list()
    assert [b - a for a, b in itertools.pairwise(call_times)] == [10.0, 10.0, 10.0]


def test_retries_transient_errors_with_backoff() -> None:
    clock = FakeClock()
    transport = ScriptedTransport(
        [
            TimeoutError(),
            (502, b"bad gateway"),
            (200, synthetic.envelope(None, 6053, "too fast")),
            (200, synthetic.envelope(synthetic.ess_list())),
        ]
    )
    client = make_client(transport, clock, backoff_s=5.0)
    assert client.get_ess_list()[0]["sysSn"] == synthetic.DEMO_SN
    assert transport.calls == 4
    # backoff 5 s (+5 s to keep the 10 s spacing), then 10 s and 20 s
    assert clock.sleeps == [5.0, 5.0, 10.0, 20.0]


def test_gives_up_after_max_retries() -> None:
    transport = ScriptedTransport([(503, b"")] * 3)
    with pytest.raises(TransientError, match="gave up after 3"):
        make_client(transport, max_retries=2).get_ess_list()


def test_auth_failure_is_friendly_and_not_retried(sample_dir: Path) -> None:
    body = (sample_dir / "error_sign.json").read_bytes()
    transport = ScriptedTransport([(200, body)])
    with pytest.raises(AuthError, match="ALPHA_APP_ID/ALPHA_APP_SECRET") as exc:
        make_client(transport).get_ess_list()
    assert exc.value.code == 6007 and transport.calls == 1
    assert APP_SECRET not in str(exc.value)


def test_http_401_is_auth_error() -> None:
    with pytest.raises(AuthError):
        make_client(ScriptedTransport([(401, b"")])).get_ess_list()


def test_unknown_api_code_raises() -> None:
    with pytest.raises(ApiError, match="6001"):
        make_client(ScriptedTransport([(200, synthetic.envelope(None, 6001, "x"))])).get_ess_list()


def test_resolve_system_single_multi_and_missing() -> None:
    one = synthetic.envelope(synthetic.ess_list())
    sn, info = resolve_system(make_client(ScriptedTransport([(200, one)])), None)
    assert sn == synthetic.DEMO_SN and info["cobat"] == 10.0

    two = synthetic.envelope(synthetic.ess_list() + synthetic.ess_list("DEMO000000000002"))
    with pytest.raises(SystemNotFoundError, match="2 systems"):
        resolve_system(make_client(ScriptedTransport([(200, two)])), None)
    sn, _ = resolve_system(make_client(ScriptedTransport([(200, two)])), "DEMO000000000002")
    assert sn == "DEMO000000000002"

    with pytest.raises(SystemNotFoundError, match="not in this developer account") as exc:
        resolve_system(make_client(ScriptedTransport([(200, one)])), "DEMO999999999999")
    assert "DEMO999999999999" not in str(exc.value)  # masked

    empty = synthetic.envelope([])
    with pytest.raises(SystemNotFoundError, match="no systems"):
        resolve_system(make_client(ScriptedTransport([(200, empty)])), None)


def test_mask_sn() -> None:
    assert mask_sn("DEMO000000000001") == "DEM*********0001"


def test_parse_power_derives_battery_from_balance() -> None:
    rows: list[dict[str, Any]] = [
        {"uploadTime": "2025-06-01 12:00:00", "ppv": 3000, "load": 500, "gridCharge": 0,
         "feedIn": 1000, "cbat": 50, "pchargingPile": 0},
        {"uploadTime": "2025-06-01 20:00", "ppv": 0, "load": 800, "gridCharge": 300,
         "feedIn": 0, "cbat": 49},
        {"uploadTime": "bad"},
    ]  # fmt: skip
    a, b = parse_power(rows, "S")
    assert (a.battery_charge_w, a.battery_discharge_w) == (1500, 0)
    assert (b.ts, b.battery_charge_w, b.battery_discharge_w) == ("2025-06-01 20:00:00", 0, 500)


def test_parse_energy_derives_load() -> None:
    e = parse_energy(
        {"epv": 20, "eInput": 3, "eOutput": 8, "eCharge": 5, "eDischarge": 4}, "S", DAYS[0]
    )
    assert e.load_kwh == 14 and e.day == "2025-06-01"


def test_fetch_twice_creates_no_duplicates(sample_dir: Path) -> None:
    """Acceptance check 3: running the same fetch twice creates no duplicate rows."""
    with Storage(":memory:") as db:
        fetcher = Fetcher(make_client(FixtureTransport(sample_dir)), db, synthetic.DEMO_SN)
        fetcher.backfill(DAYS[0], DAYS[-1])
        counts = (db.count("power_samples"), db.count("daily_energy"), db.count("fetch_log"))
        fetcher.backfill(DAYS[0], DAYS[-1])
        again = (db.count("power_samples"), db.count("daily_energy"), db.count("fetch_log"))
    assert counts == again == (3 * 288, 3, 6)


def test_daily_totals_match_energy_endpoint_within_1_percent(sample_dir: Path) -> None:
    """Acceptance check 2: DB daily totals match getOneDateEnergyBySn within 1 %, both the
    stored totals and the totals re-integrated from the stored power series."""
    with Storage(":memory:") as db:
        Fetcher(make_client(FixtureTransport(sample_dir)), db, synthetic.DEMO_SN).backfill(
            DAYS[0], DAYS[-1]
        )
        stored = {d.day: d for d in db.daily_range(synthetic.DEMO_SN)}
        for day in DAYS:
            ref = json.loads((sample_dir / f"getOneDateEnergyBySn_{day}.json").read_text())["data"]
            row = stored[day.isoformat()]
            series = db.integrate_day(synthetic.DEMO_SN, day.isoformat())
            pairs = [
                (row.pv_kwh, ref["epv"]),
                (row.grid_import_kwh, ref["eInput"]),
                (row.grid_export_kwh, ref["eOutput"]),
                (row.battery_charge_kwh, ref["eCharge"]),
                (row.battery_discharge_kwh, ref["eDischarge"]),
                (series["pv_w"], ref["epv"]),
                (series["grid_import_w"], ref["eInput"]),
                (series["grid_export_w"], ref["eOutput"]),
                (series["battery_charge_w"], ref["eCharge"]),
                (series["battery_discharge_w"], ref["eDischarge"]),
            ]
            for got, want in pairs:
                assert got == pytest.approx(want, rel=0.01, abs=0.01), day


def test_backfill_rejects_reversed_range(sample_dir: Path) -> None:
    with Storage(":memory:") as db:
        fetcher = Fetcher(make_client(FixtureTransport(sample_dir)), db, synthetic.DEMO_SN)
        with pytest.raises(ValueError):
            fetcher.backfill(DAYS[1], DAYS[0])


def test_daily_fetches_yesterday_and_today_against_fake_api() -> None:
    data = synthetic.generate(date(2025, 6, 1), 5)
    clock = FakeClock()
    transport = SyntheticTransport(data, APP_ID, APP_SECRET, clock)
    with Storage(":memory:") as db:
        results = Fetcher(make_client(transport, clock), db, data.sys_sn).daily(date(2025, 6, 5))
        assert [r.day for r in results] == [date(2025, 6, 4), date(2025, 6, 5)]
        assert db.count("daily_energy") == 2
    times = [t for t, _ in transport.calls]
    assert all(b - a >= 10.0 for a, b in itertools.pairwise(times))


def test_fake_api_rejects_bad_signature() -> None:
    data = synthetic.generate(date(2025, 6, 1), 1)
    transport = SyntheticTransport(data, APP_ID, "other-secret")
    with pytest.raises(AuthError):
        make_client(transport).get_ess_list()
