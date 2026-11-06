"""AlphaESS Open API client (signed requests, rate limiting, retries) and the fetch jobs.

Request signing follows the public Open API scheme: headers `appId`, `timeStamp` (unix seconds)
and `sign` = sha512(appId + appSecret + timeStamp) as hex. Written from scratch; the community
library `alphaessopenapi` (MIT) was used as a reference for endpoint names and return codes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from energy_analyzer import __version__
from energy_analyzer.models import DailyEnergy, PowerSample
from energy_analyzer.storage import Storage

log = logging.getLogger(__name__)

# (status, body); raise OSError/TimeoutError for network failures.
Transport = Callable[[str, dict[str, str], float], tuple[int, bytes]]

AUTH_CODES = {6002, 6005, 6006, 6007, 6009, 6010, 6011, 6012, 6017}
NOT_FOUND_CODES = {6038}
RETRY_CODES = {6026, 6053}  # internal error, too fast
NO_DATA_CODES = {6016}


class ApiError(Exception):
    """The API answered with an error. Messages never contain credentials."""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class AuthError(ApiError):
    pass


class SystemNotFoundError(ApiError):
    pass


class TransientError(ApiError):
    pass


def sign(app_id: str, app_secret: str, timestamp: str) -> str:
    return hashlib.sha512(f"{app_id}{app_secret}{timestamp}".encode()).hexdigest()


def mask_sn(sn: str) -> str:
    """Show enough of a serial to recognise it without publishing it in logs."""
    return sn if len(sn) <= 6 else f"{sn[:3]}{'*' * (len(sn) - 7)}{sn[-4:]}"


def urllib_transport(url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.status), resp.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read() or b""


class AlphaESSClient:
    def __init__(
        self,
        app_id: str,
        app_secret: str,
        *,
        base_url: str,
        transport: Transport = urllib_transport,
        min_interval_s: float = 10.0,
        timeout_s: float = 30.0,
        max_retries: int = 4,
        backoff_s: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self.base_url = base_url.rstrip("/")
        self.transport = transport
        self.min_interval_s = min_interval_s
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.backoff_s = backoff_s
        self._clock = clock
        self._sleep = sleep
        self._wall = wall_clock
        self._last_call: float | None = None
        self.calls = 0

    def __repr__(self) -> str:  # never show credentials
        return f"AlphaESSClient(base_url={self.base_url!r})"

    def _headers(self) -> dict[str, str]:
        ts = str(int(self._wall()))
        return {
            "appId": self._app_id,
            "timeStamp": ts,
            "sign": sign(self._app_id, self._app_secret, ts),
            "Accept": "application/json",
            "User-Agent": f"alphaess-energy-analyzer/{__version__}",
        }

    def _wait_turn(self) -> None:
        if self._last_call is not None:
            wait = self._last_call + self.min_interval_s - self._clock()
            if wait > 0:
                self._sleep(wait)
        self._last_call = self._clock()

    def _get(self, endpoint: str, **params: str) -> Any:
        query = urllib.parse.urlencode(params)
        url = f"{self.base_url}/{endpoint}" + (f"?{query}" if query else "")
        last_error: ApiError | None = None
        for attempt in range(self.max_retries + 1):
            if attempt:
                delay = self.backoff_s * 2 ** (attempt - 1)
                log.info("retrying %s in %.0fs (attempt %d)", endpoint, delay, attempt + 1)
                self._sleep(delay)
            self._wait_turn()
            self.calls += 1
            try:
                status, body = self.transport(url, self._headers(), self.timeout_s)
            except (OSError, TimeoutError) as exc:
                last_error = TransientError(f"{endpoint}: network error ({type(exc).__name__})")
                continue
            if status in (401, 403):
                raise AuthError(f"{endpoint}: HTTP {status} - check ALPHA_APP_ID/ALPHA_APP_SECRET")
            if status == 429 or status >= 500:
                last_error = TransientError(f"{endpoint}: HTTP {status}")
                continue
            if status != 200:
                raise ApiError(f"{endpoint}: unexpected HTTP {status}")
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                last_error = TransientError(f"{endpoint}: response is not valid JSON")
                continue
            code = payload.get("code") if isinstance(payload, dict) else None
            msg = str(payload.get("msg") or "") if isinstance(payload, dict) else ""
            if code == 200:
                return payload.get("data")
            if code in NO_DATA_CODES:
                return None
            if code in AUTH_CODES:
                raise AuthError(
                    f"{endpoint}: authentication failed (code {code}: {msg}). Check "
                    "ALPHA_APP_ID/ALPHA_APP_SECRET and that the system is added to your "
                    "developer account on open.alphaess.com.",
                    code,
                )
            if code in NOT_FOUND_CODES:
                raise SystemNotFoundError(f"{endpoint}: system serial not found ({msg})", code)
            if code in RETRY_CODES:
                last_error = TransientError(f"{endpoint}: code {code}: {msg}", code)
                continue
            raise ApiError(f"{endpoint}: API error code {code}: {msg}", code)
        assert last_error is not None
        raise TransientError(f"{last_error} (gave up after {self.max_retries + 1} attempts)")

    # --- endpoints ------------------------------------------------------------------------

    def get_ess_list(self) -> list[dict[str, Any]]:
        data = self._get("getEssList")
        return [d for d in (data or []) if isinstance(d, dict)]

    def get_one_day_power(self, sys_sn: str, day: date) -> list[dict[str, Any]]:
        data = self._get("getOneDayPowerBySn", sysSn=sys_sn, queryDate=day.isoformat())
        return [d for d in (data or []) if isinstance(d, dict)]

    def get_one_date_energy(self, sys_sn: str, day: date) -> dict[str, Any] | None:
        data = self._get("getOneDateEnergyBySn", sysSn=sys_sn, queryDate=day.isoformat())
        return data if isinstance(data, dict) and data else None

    def get_last_power(self, sys_sn: str) -> dict[str, Any] | None:
        data = self._get("getLastPowerData", sysSn=sys_sn)
        return data if isinstance(data, dict) else None


# --- parsing ------------------------------------------------------------------------------


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_power(rows: list[dict[str, Any]], sys_sn: str) -> list[PowerSample]:
    """Map getOneDayPowerBySn rows. Battery power is not in the series; it is derived from
    the balance pv + grid_import = load + ev + grid_export + battery (positive = charging)."""
    out: dict[str, PowerSample] = {}
    for row in rows:
        ts = str(row.get("uploadTime") or "").strip()
        if len(ts) < 16:
            continue
        ts = ts[:19] if len(ts) >= 19 else f"{ts[:16]}:00"
        pv, load = _num(row.get("ppv")), _num(row.get("load"))
        imp, exp = _num(row.get("gridCharge")), _num(row.get("feedIn"))
        ev = _num(row.get("pchargingPile")) or 0.0
        charge = discharge = None
        if None not in (pv, load, imp, exp):
            net = (pv or 0) + (imp or 0) - (load or 0) - (exp or 0) - ev
            charge, discharge = max(net, 0.0), max(-net, 0.0)
        out[ts] = PowerSample(
            sys_sn, ts, pv, load, imp, exp, charge, discharge, _num(row.get("cbat"))
        )
    return [out[k] for k in sorted(out)]


def parse_energy(data: dict[str, Any], sys_sn: str, day: date) -> DailyEnergy:
    pv, imp, exp = _num(data.get("epv")), _num(data.get("eInput")), _num(data.get("eOutput"))
    chg, dis = _num(data.get("eCharge")), _num(data.get("eDischarge"))
    load = None
    if None not in (pv, imp, exp, chg, dis):
        load = round((pv or 0) + (imp or 0) + (dis or 0) - (exp or 0) - (chg or 0), 3)
    the_date = str(data.get("theDate") or day.isoformat())[:10]
    return DailyEnergy(
        sys_sn,
        the_date,
        pv,
        load,
        imp,
        exp,
        chg,
        dis,
        _num(data.get("eGridCharge")),
        _num(data.get("eChargingPile")),
    )


# --- jobs ---------------------------------------------------------------------------------


def resolve_system(client: AlphaESSClient, wanted: str | None) -> tuple[str, dict[str, Any]]:
    systems = client.get_ess_list()
    if not systems:
        raise SystemNotFoundError(
            "getEssList returned no systems. Add your system's serial to your developer "
            "account on open.alphaess.com first."
        )
    by_sn = {str(s.get("sysSn")): s for s in systems if s.get("sysSn")}
    if wanted:
        if wanted not in by_sn:
            raise SystemNotFoundError(
                f"system {mask_sn(wanted)} is not in this developer account "
                f"(found {len(by_sn)}: {', '.join(mask_sn(s) for s in sorted(by_sn))})"
            )
        return wanted, by_sn[wanted]
    if len(by_sn) > 1:
        raise SystemNotFoundError(
            f"this developer account has {len(by_sn)} systems "
            f"({', '.join(mask_sn(s) for s in sorted(by_sn))}); choose one with "
            "ALPHA_SYSTEM_SN, [system] sn in config.toml or --sn"
        )
    sn = next(iter(by_sn))
    return sn, by_sn[sn]


def iter_days(first: date, last: date) -> Iterator[date]:
    day = first
    while day <= last:
        yield day
        day += timedelta(days=1)


@dataclass
class DayResult:
    day: date
    power_rows: int
    has_energy: bool


class Fetcher:
    def __init__(self, client: AlphaESSClient, storage: Storage, sys_sn: str) -> None:
        self.client = client
        self.storage = storage
        self.sys_sn = sys_sn

    def fetch_day(self, day: date) -> DayResult:
        samples = parse_power(self.client.get_one_day_power(self.sys_sn, day), self.sys_sn)
        self.storage.upsert_power(samples)
        self.storage.log_fetch(self.sys_sn, day.isoformat(), "getOneDayPowerBySn", len(samples))
        energy = self.client.get_one_date_energy(self.sys_sn, day)
        if energy is not None:
            self.storage.upsert_daily([parse_energy(energy, self.sys_sn, day)])
        self.storage.log_fetch(
            self.sys_sn, day.isoformat(), "getOneDateEnergyBySn", int(energy is not None)
        )
        return DayResult(day, len(samples), energy is not None)

    def backfill(
        self,
        first: date,
        last: date,
        on_day: Callable[[DayResult], None] | None = None,
    ) -> list[DayResult]:
        if last < first:
            raise ValueError("--to must not be before --from")
        results = []
        for day in iter_days(first, last):
            result = self.fetch_day(day)
            results.append(result)
            if on_day:
                on_day(result)
        return results

    def daily(self, today: date | None = None) -> list[DayResult]:
        today = today or date.today()
        return self.backfill(today - timedelta(days=1), today)
