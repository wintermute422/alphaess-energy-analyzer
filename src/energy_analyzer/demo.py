"""A synthetic demo database for `energy-analyzer serve --demo` (no account needed)."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from energy_analyzer import synthetic
from energy_analyzer.events import validate
from energy_analyzer.fake_api import FakeClock, SyntheticTransport
from energy_analyzer.fetch import AlphaESSClient, Fetcher, resolve_system
from energy_analyzer.storage import Storage

DEMO_DAYS = 120
DEMO_APP_ID, DEMO_APP_SECRET = "demo-app", "demo-secret"
DEMO_ANOMALIES = synthetic.AnomalySpec(
    high_baseline=(70,), night_spike=(84,), high_consumption=(101,), away=tuple(range(88, 96))
)


def demo_start(today: date | None = None) -> date:
    return (today or date.today()) - timedelta(days=DEMO_DAYS)


def build_demo(db_path: Path, today: date | None = None) -> SyntheticTransport:
    """Fill db_path with 120 synthetic days ending yesterday plus example events; return a
    transport that also answers live requests for the same synthetic system."""
    start = demo_start(today)
    data = synthetic.generate(start, DEMO_DAYS, anomalies=DEMO_ANOMALIES)
    clock = FakeClock()
    transport = SyntheticTransport(data, DEMO_APP_ID, DEMO_APP_SECRET, clock)
    client = AlphaESSClient(
        DEMO_APP_ID,
        DEMO_APP_SECRET,
        base_url="https://demo.invalid/api",
        transport=transport,
        clock=clock.clock,
        sleep=clock.sleep,
    )

    def day(offset: int) -> str:
        return (start + timedelta(days=offset)).isoformat()

    with Storage(db_path) as db:
        sn, info = resolve_system(client, None)
        db.upsert_system(sn, info)
        Fetcher(client, db, sn).backfill(start, start + timedelta(days=DEMO_DAYS - 1))
        for event in (
            validate("away", "Holiday", day(88), day(95), note="Demo event"),
            validate("guests", "Family visit", day(101), day(101)),
            validate("note", "New fridge delivered", day(40), None),
            validate("away", "Weekend trip (planned)", day(DEMO_DAYS + 10), day(DEMO_DAYS + 12)),
        ):
            db.save_event(event)
    return SyntheticTransport(data, DEMO_APP_ID, DEMO_APP_SECRET)
