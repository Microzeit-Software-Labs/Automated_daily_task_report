"""Small shared conventions for column and constraint definitions."""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum

from sqlalchemy import DateTime

TIMESTAMPTZ = DateTime(timezone=True)
"""Every stored instant is timezone-aware. A naive DateTime column would let a
naive Python datetime slip into the database undetected -- exactly the bug
:func:`interlock.domain.common.clock.ensure_aware` exists to stop upstream."""

ULID_LENGTH = 26


def check_in(column: str, values: type[StrEnum] | Iterable[str]) -> str:
    """A ``column IN (...)`` CHECK expression.

    Accepts a domain ``StrEnum`` directly, so a constraint's allowed values can
    never silently drift from the enum they mirror -- also accepts a plain
    iterable for the handful of constraints with no matching domain enum.

    Built with an explicit join rather than a tuple's ``repr()``: Python renders
    a one-element tuple as ``('X',)``, and that trailing comma is not valid
    inside a SQL ``IN (...)`` list.
    """
    members = [v.value for v in values] if isinstance(values, type) else list(values)
    quoted = ", ".join(f"'{v}'" for v in members)
    return f"{column} IN ({quoted})"
