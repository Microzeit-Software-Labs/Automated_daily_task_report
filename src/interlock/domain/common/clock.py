"""Time.

Every datetime that crosses a boundary in this system is timezone-aware. Naive
datetimes are rejected at construction rather than silently interpreted, because
a naive datetime in a scheduling system is a bug that surfaces months later as
"the report went out at the wrong time".

Time is injected rather than read from the global clock so that scheduling,
staleness and overdue logic are testable without sleeping or touching the system
clock.
"""

from __future__ import annotations

import datetime as dt
from typing import Protocol, runtime_checkable
from zoneinfo import ZoneInfo

UTC = dt.UTC


def ensure_aware(moment: dt.datetime, *, field: str = "datetime") -> dt.datetime:
    """Return ``moment`` unchanged, or raise if it is naive.

    Used at every boundary where a datetime enters the domain.
    """
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ValueError(
            f"{field} must be timezone-aware; got a naive datetime ({moment!r}). "
            "Naive datetimes are never valid in this system."
        )
    return moment


def local_date(moment: dt.datetime, tz: ZoneInfo) -> dt.date:
    """The calendar date ``moment`` falls on, in ``tz``.

    This is the only correct way to answer "what day is it" for a user in
    Asia/Kolkata when storage is UTC.
    """
    return ensure_aware(moment).astimezone(tz).date()


def local_time(moment: dt.datetime, tz: ZoneInfo) -> dt.time:
    return ensure_aware(moment).astimezone(tz).timetz()


def combine_local(day: dt.date, at: dt.time, tz: ZoneInfo) -> dt.datetime:
    """Build the aware UTC instant for a wall-clock time on a given local day.

    Used to turn "09:00 on 16 September, Asia/Kolkata" into a real instant.
    """
    return dt.datetime.combine(day, at, tzinfo=tz).astimezone(UTC)


@runtime_checkable
class Clock(Protocol):
    def now(self) -> dt.datetime:
        """The current instant, always timezone-aware, always UTC."""
        ...


class SystemClock:
    """The real clock. The only implementation used in production."""

    def now(self) -> dt.datetime:
        return dt.datetime.now(UTC)


class FrozenClock:
    """A controllable clock for tests.

    Lets a test say "it is now 17:09 on the 15th" or advance four hours
    instantly, so scheduling behaviour is verified deterministically instead of
    by waiting.
    """

    def __init__(self, at: dt.datetime) -> None:
        self._at = ensure_aware(at, field="FrozenClock start").astimezone(UTC)

    def now(self) -> dt.datetime:
        return self._at

    def advance(self, delta: dt.timedelta) -> FrozenClock:
        self._at += delta
        return self

    def set_to(self, at: dt.datetime) -> FrozenClock:
        self._at = ensure_aware(at, field="FrozenClock target").astimezone(UTC)
        return self
