"""Identifiers.

Two kinds, deliberately separated:

* **Internal ids** are ULIDs -- sortable by creation time, collision-free without
  coordination, safe to generate before a database round-trip.
* **Display ids** (``TSK-00104``) are what a person reads and types. They are
  assigned from a sequence, are immutable for the life of the entity, and are
  never used as foreign keys.

Mixing the two is a common source of bugs, so they have separate types and no
implicit conversion.
"""

from __future__ import annotations

from typing import Final

from ulid import ULID

TASK_PREFIX: Final = "TSK"
SHARE_PREFIX: Final = "SHR"
REVIEW_PREFIX: Final = "REV"

_MIN_DISPLAY_DIGITS: Final = 5


def new_id() -> str:
    """A fresh internal identifier."""
    return str(ULID())


def format_display_id(prefix: str, sequence: int) -> str:
    """``("TSK", 104) -> "TSK-00104"``.

    Zero-padded to five digits for alignment in lists, but not truncated -- a
    six-digit sequence simply renders wider rather than wrapping around.
    """
    if sequence < 1:
        raise ValueError(f"display id sequence must be positive; got {sequence}")
    return f"{prefix}-{sequence:0{_MIN_DISPLAY_DIGITS}d}"


def parse_display_id(display_id: str) -> tuple[str, int]:
    """Inverse of :func:`format_display_id`. Raises ``ValueError`` if malformed."""
    prefix, _, digits = display_id.partition("-")
    if not prefix or not digits or not digits.isdigit():
        raise ValueError(f"malformed display id: {display_id!r}")
    return prefix, int(digits)
