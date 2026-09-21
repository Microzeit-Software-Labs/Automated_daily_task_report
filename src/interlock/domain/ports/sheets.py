"""The spreadsheet peer port.

The Postgres ``tasks`` table is always the source of truth; the sheet is a
peer that can be edited independently, not a read replica and not an
alternate :class:`~interlock.domain.ports.repositories.TaskRepository`. A
:class:`SpreadsheetProvider` only knows how to read and write raw rows --
every reconciliation decision (push, pull, conflict) lives in
:mod:`interlock.domain.sync.reconciliation` and
:mod:`interlock.services.sheet_sync_service`, not here.

One read and one batched write per tick, deliberately: a real implementation
should not make a network call per row.

``fields`` on both :class:`SheetRow` and :class:`SheetRowWrite` must be
shaped exactly like
:func:`interlock.domain.sync.reconciliation.task_content_fields` --
enum members as their string ``.value``, dates as ISO strings, tags as a
list -- so a hash computed on either side is comparable to the other.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable


@dataclasses.dataclass(frozen=True, slots=True)
class SheetRow:
    """One parsed row read back from the sheet."""

    row_index: int
    """1-based sheet row number. Informational only -- never used as row
    identity. Row position is not stable (see the architecture doc's R5);
    identity always comes from ``sys_task_id``."""

    sys_task_id: str | None
    """Parsed from the protected ``_sys`` column. ``None`` means this row
    has no task behind it yet -- a human typed a new row by hand."""

    sys_version: int | None
    sys_content_hash: str | None
    fields: dict[str, Any]


@dataclasses.dataclass(frozen=True, slots=True)
class SheetRowWrite:
    """One row to write back, as part of a single batched call."""

    row_index: int | None
    """``None`` appends a new row; otherwise overwrites the row at this index."""

    sys_task_id: str
    sys_version: int
    sys_content_hash: str
    display_id: str
    """For human reference only -- never parsed back on read."""
    fields: dict[str, Any]


@runtime_checkable
class SpreadsheetProvider(Protocol):
    def read_all_rows(self) -> Sequence[SheetRow]:
        """Every data row currently in the sheet, header row excluded."""
        ...

    def write_rows(self, writes: Sequence[SheetRowWrite]) -> None:
        """Apply every write in ``writes`` as one batched call."""
        ...
