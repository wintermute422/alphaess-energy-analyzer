"""Known events the user tells the analyzer about, and how they change the analysis.

Effects:
- expect_lower   the period should use less energy (holiday). Days are left out of other days'
                 reference; anomalies in the expected direction are marked as expected, and a
                 day that is NOT lower is flagged ("not_lower").
- expect_higher  the period should use more (guests, EV, home office): same idea, upwards.
- regime_change  from this day on the household behaves differently (heating season, new
                 appliance): days are only compared with days after the change.
- ignore         data of the period is not representative (outage, maintenance): no flags,
                 and left out of every reference.
- info           just a note on the charts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from energy_analyzer.models import Event

EFFECTS = ("expect_lower", "expect_higher", "regime_change", "ignore", "info")


@dataclass(frozen=True)
class EventKind:
    kind: str
    label: str
    effect: str
    hint: str


EVENT_KINDS: tuple[EventKind, ...] = (
    EventKind("away", "Holiday / away", "expect_lower",
              "Nobody home: expect lower use, warn if it is not lower"),
    EventKind("guests", "Guests", "expect_higher", "More people in the house"),
    EventKind("home_office", "Home office", "expect_higher", "More hours at home than usual"),
    EventKind("ev", "EV / extra charging", "expect_higher", "Car or other big charger in use"),
    EventKind("heating_on", "Heating season starts", "regime_change",
              "Heat pump / heating switched to winter mode"),
    EventKind("heating_off", "Heating season ends", "regime_change",
              "Heating switched off for summer"),
    EventKind("cooling", "Cooling season", "regime_change", "Air conditioning in use from now on"),
    EventKind("new_appliance", "New appliance", "regime_change",
              "A device was added or removed (freezer, pool pump, ...)"),
    EventKind("outage", "Outage / maintenance", "ignore",
              "Inverter offline, grid outage, service: do not judge these days"),
    EventKind("note", "Note", "info", "Anything worth remembering"),
)  # fmt: skip
KINDS = {k.kind: k for k in EVENT_KINDS}


class EventError(ValueError):
    """Invalid event data (message is safe to show the user)."""


def validate(
    kind: str,
    title: str,
    start_day: str,
    end_day: str | None,
    effect: str | None = None,
    note: str = "",
    event_id: int | None = None,
) -> Event:
    if kind not in KINDS:
        raise EventError(f"unknown kind {kind!r}")
    effect = effect or KINDS[kind].effect
    if effect not in EFFECTS:
        raise EventError(f"unknown effect {effect!r}")
    title = (title or KINDS[kind].label).strip()
    if len(title) > 120 or len(note) > 2000:
        raise EventError("title (120) or note (2000 characters) too long")
    try:
        date.fromisoformat(start_day)
        if end_day:
            date.fromisoformat(end_day)
    except (TypeError, ValueError) as exc:
        raise EventError("dates must be YYYY-MM-DD") from exc
    if end_day and end_day < start_day:
        raise EventError("end date is before start date")
    if effect == "regime_change":
        end_day = None  # a point in time
    return Event(event_id, kind, title, start_day, end_day or None, effect, note.strip())


class EventIndex:
    """Answers the questions the analysis asks about a day."""

    def __init__(self, events: list[Event] | None = None) -> None:
        events = events or []
        self.ranges = [e for e in events if e.effect != "regime_change"]
        self.changes = sorted(
            (e.start_day for e in events if e.effect == "regime_change"), reverse=True
        )

    def covering(self, day: str) -> list[Event]:
        return [e for e in self.ranges if e.covers(day)]

    def ignored(self, day: str) -> bool:
        return any(e.effect == "ignore" for e in self.covering(day))

    def excluded_from_reference(self, day: str) -> bool:
        return any(
            e.effect in ("ignore", "expect_lower", "expect_higher") for e in self.covering(day)
        )

    def regime_start(self, day: str) -> str | None:
        """The latest regime change on or before day."""
        return next((d for d in self.changes if d <= day), None)

    def expected(self, day: str, direction: int) -> Event | None:
        """The event that explains a deviation upwards (+1) or downwards (-1) on day."""
        want = "expect_higher" if direction > 0 else "expect_lower"
        return next((e for e in self.covering(day) if e.effect == want), None)
