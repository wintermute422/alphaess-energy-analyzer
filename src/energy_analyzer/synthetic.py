"""Deterministic synthetic AlphaESS data, shaped like Open API responses.

Used by the test fixtures in sample_data/ and by `energy-analyzer selftest`. Nothing here is real
data: the serial number, the household and the weather are all made up.

The model is a simple household with PV, a 10 kWh battery and the grid: 5-minute power samples,
a battery that charges from PV surplus and covers deficits, and daily energy totals that are the
exact integral of the (integer-rounded) power samples, so energy balances hold by construction.
"""

from __future__ import annotations

import argparse
import json
import math
import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

DEMO_SN = "DEMO000000000001"
SLOT_MINUTES = 5
SLOTS_PER_DAY = 24 * 60 // SLOT_MINUTES

BATTERY_KWH = 10.0
BATTERY_MAX_W = 5000.0
SOC_MIN = 10.0
SOC_MAX = 100.0


@dataclass(frozen=True)
class AnomalySpec:
    """Day offsets (0-based, relative to the start date) that get injected anomalies."""

    high_baseline: tuple[int, ...] = ()  # standby load ~2.6x for the whole day
    night_spike: tuple[int, ...] = ()  # +2.5 kW between 03:00 and 03:30
    high_consumption: tuple[int, ...] = ()  # daytime load x2.2 (e.g. a heater left on)


DEFAULT_ANOMALIES = AnomalySpec(high_baseline=(40,), night_spike=(47,), high_consumption=(53,))


@dataclass
class SyntheticDay:
    day: date
    power_rows: list[dict[str, Any]]
    energy: dict[str, Any]
    baseline_w: float  # the true standby level used to generate the night


@dataclass
class SyntheticDataset:
    sys_sn: str
    days: list[SyntheticDay] = field(default_factory=list)

    def by_date(self) -> dict[str, SyntheticDay]:
        return {d.day.isoformat(): d for d in self.days}


def envelope(data: Any, code: int = 200, msg: str = "Success") -> dict[str, Any]:
    """Wrap data the way the Open API does."""
    return {"code": code, "msg": msg, "expMsg": None, "data": data}


def ess_list(sys_sn: str = DEMO_SN) -> list[dict[str, Any]]:
    return [
        {
            "sysSn": sys_sn,
            "popv": 9.6,
            "minv": "DEMO-INV-10K",
            "poinv": 10.0,
            "cobat": 10.0,
            "mbat": "DEMO-BAT",
            "usCapacity": 95.0,
            "emsStatus": "Normal",
        }
    ]


def last_power(sys_sn: str = DEMO_SN) -> dict[str, Any]:
    return {"sysSn": sys_sn, "ppv": 2310, "pload": 640, "soc": 72.5, "pgrid": 0, "pbat": -1670}


def _load_w(rng: random.Random, hour: float, base: float, heavy: bool) -> float:
    load = base + abs(rng.gauss(0, 8))
    if 6.5 <= hour < 8.5:
        load += 600
    if 11.0 <= hour < 13.0:
        load += 400
    if 17.0 <= hour < 22.0:
        load += 900
    if 6.0 <= hour < 23.0:
        if rng.random() < 0.02:
            load += 2000  # kettle, oven, ...
        if heavy:
            load *= 2.2
    return load


def _pv_w(rng: random.Random, hour: float, season: float, cloud: float) -> float:
    if not 6.0 < hour < 20.0:
        return 0.0
    shape = float(math.sin(math.pi * (hour - 6.0) / 14.0) ** 1.5)
    jitter = min(1.0, max(0.0, cloud + rng.gauss(0, 0.08)))
    return 7000.0 * season * jitter * shape


def generate(
    start: date,
    days: int,
    *,
    seed: int = 42,
    sys_sn: str = DEMO_SN,
    anomalies: AnomalySpec = DEFAULT_ANOMALIES,
) -> SyntheticDataset:
    rng = random.Random(seed)
    soc = 50.0
    out = SyntheticDataset(sys_sn=sys_sn)
    slot_h = SLOT_MINUTES / 60.0
    for offset in range(days):
        day = start + timedelta(days=offset)
        doy = day.timetuple().tm_yday
        season = min(1.0, max(0.25, 0.6 + 0.4 * math.cos(2 * math.pi * (doy - 172) / 365.0)))
        cloud = rng.uniform(0.3, 1.0)
        base = 150.0 + 10.0 * math.sin(offset / 30.0) + rng.gauss(0, 5)
        if offset in anomalies.high_baseline:
            base *= 2.6
        heavy = offset in anomalies.high_consumption
        spike = offset in anomalies.night_spike

        rows: list[dict[str, Any]] = []
        totals = dict.fromkeys(("pv", "imp", "exp", "chg", "dis"), 0.0)
        for slot in range(SLOTS_PER_DAY):
            hour = slot * slot_h
            load = _load_w(rng, hour, base, heavy)
            if spike and 3.0 <= hour < 3.5:
                load += 2500
            pv = round(_pv_w(rng, hour, season, cloud))
            load = round(load)
            surplus = pv - load
            charge = discharge = imp = exp = 0
            if surplus > 0:
                room_w = (SOC_MAX - soc) / 100.0 * BATTERY_KWH * 1000.0 / slot_h
                charge = round(min(surplus, BATTERY_MAX_W, room_w))
                exp = surplus - charge
            else:
                avail_w = max(0.0, soc - SOC_MIN) / 100.0 * BATTERY_KWH * 1000.0 / slot_h
                discharge = round(min(-surplus, BATTERY_MAX_W, avail_w))
                imp = -surplus - discharge
            soc += (charge - discharge) * slot_h / 1000.0 / BATTERY_KWH * 100.0
            soc = min(SOC_MAX, max(0.0, soc))
            hh, mm = divmod(slot * SLOT_MINUTES, 60)
            rows.append(
                {
                    "sysSn": sys_sn,
                    "uploadTime": f"{day.isoformat()} {hh:02d}:{mm:02d}:00",
                    "ppv": pv,
                    "load": load,
                    "cbat": round(soc, 1),
                    "feedIn": exp,
                    "gridCharge": imp,
                    "pchargingPile": 0,
                }
            )
            totals["pv"] += pv
            totals["imp"] += imp
            totals["exp"] += exp
            totals["chg"] += charge
            totals["dis"] += discharge

        kwh = {k: round(v * slot_h / 1000.0, 3) for k, v in totals.items()}
        energy = {
            "sysSn": sys_sn,
            "theDate": day.isoformat(),
            "epv": kwh["pv"],
            "eInput": kwh["imp"],
            "eOutput": kwh["exp"],
            "eCharge": kwh["chg"],
            "eDischarge": kwh["dis"],
            "eGridCharge": 0.0,
            "eChargingPile": 0.0,
        }
        out.days.append(SyntheticDay(day=day, power_rows=rows, energy=energy, baseline_w=base))
    return out


def write_sample_data(out_dir: Path, start: date, days: int, seed: int = 42) -> list[Path]:
    """Write recorded-style API responses (one file per endpoint and day)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    data = generate(start, days, seed=seed, anomalies=AnomalySpec())
    written: list[Path] = []

    def dump(name: str, payload: dict[str, Any]) -> None:
        path = out_dir / name
        path.write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8", newline="\n")
        written.append(path)

    dump("getEssList.json", envelope(ess_list(data.sys_sn)))
    dump("getLastPowerData.json", envelope(last_power(data.sys_sn)))
    for d in data.days:
        dump(f"getOneDayPowerBySn_{d.day.isoformat()}.json", envelope(d.power_rows))
        dump(f"getOneDateEnergyBySn_{d.day.isoformat()}.json", envelope(d.energy))
    dump("error_sign.json", envelope(None, code=6007, msg="Sign verification error"))
    dump("error_too_fast.json", envelope(None, code=6053, msg="The request was too fast"))
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write synthetic AlphaESS sample responses.")
    parser.add_argument("--out", type=Path, default=Path("sample_data"))
    parser.add_argument("--start", type=date.fromisoformat, default=date(2025, 6, 1))
    parser.add_argument("--days", type=int, default=3)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args(argv)
    for path in write_sample_data(args.out, args.start, args.days, args.seed):
        print(path.as_posix())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
