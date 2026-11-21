"""The clock helpers every use case shares: UTC "now" and the Madrid calendar "today".

Kept apart from ``schemas`` (which stamps results with them) so that use cases and tests can
import the time logic without pulling in the output models. ``legislation`` and ``articles`` take
``now`` and ``today`` arguments and pass them through these two functions, which is how tests fix
the clock.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

__all__ = ["madrid_today", "utc_now"]

# Calendar days of Spanish legislation are Madrid days.
_MADRID = ZoneInfo("Europe/Madrid")


def utc_now(now: datetime | None = None) -> datetime:
    """Return ``now``, or the current UTC time when it is ``None`` (the injectable clock)."""
    return now if now is not None else datetime.now(UTC)


def madrid_today(moment: datetime, today: date | None = None) -> date:
    """Return ``today`` if given, else the Madrid calendar date at ``moment``."""
    # The BOE works in Madrid time, so "today" is the Madrid date, not the UTC one. Madrid is one
    # or two hours ahead, so for a short window around midnight UTC the dates differ, and a law
    # taking effect "tomorrow" in UTC terms may already be in force.
    return today if today is not None else moment.astimezone(_MADRID).date()
