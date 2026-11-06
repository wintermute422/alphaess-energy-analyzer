"""An in-process stand-in for the Open API, serving synthetic data (selftest and tests)."""

from __future__ import annotations

import json
import urllib.parse
from dataclasses import dataclass, field

from energy_analyzer.fetch import sign
from energy_analyzer.synthetic import SyntheticDataset, envelope, ess_list, last_power


@dataclass
class FakeClock:
    """Monotonic clock + sleep that advance virtual time instead of waiting."""

    now: float = 1_000.0
    sleeps: list[float] = field(default_factory=list)

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@dataclass
class SyntheticTransport:
    dataset: SyntheticDataset
    app_id: str
    app_secret: str
    clock: FakeClock | None = None
    calls: list[tuple[float, str]] = field(default_factory=list)

    def __call__(self, url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        parsed = urllib.parse.urlparse(url)
        endpoint = parsed.path.rsplit("/", 1)[-1]
        params = dict(urllib.parse.parse_qsl(parsed.query))
        self.calls.append((self.clock.now if self.clock else 0.0, endpoint))
        expected = sign(self.app_id, self.app_secret, headers.get("timeStamp", ""))
        if headers.get("appId") != self.app_id or headers.get("sign") != expected:
            return 200, _dump(envelope(None, 6007, "Sign verification error"))
        sn = params.get("sysSn")
        if endpoint == "getEssList":
            return 200, _dump(envelope(ess_list(self.dataset.sys_sn)))
        if sn is not None and sn != self.dataset.sys_sn:
            return 200, _dump(envelope(None, 6005, "This appId is not bound to the SN"))
        if endpoint == "getLastPowerData":
            return 200, _dump(envelope(last_power(self.dataset.sys_sn)))
        day = self.dataset.by_date().get(params.get("queryDate", ""))
        if endpoint == "getOneDayPowerBySn":
            return 200, _dump(envelope(day.power_rows if day else []))
        if endpoint == "getOneDateEnergyBySn":
            return 200, _dump(envelope(day.energy if day else None))
        return 404, b"not found"


def _dump(payload: object) -> bytes:
    return json.dumps(payload).encode("utf-8")
