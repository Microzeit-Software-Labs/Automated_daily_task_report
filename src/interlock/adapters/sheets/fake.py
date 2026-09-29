"""An in-memory spreadsheet, for tests and for running the worker with no
Google credentials configured at all.

Plays the same role for the sync tick that
:class:`~interlock.adapters.whatsapp.mock.MockWhatsAppProvider` plays for
sends: the safe default (``SHEETS_PROVIDER=mock``) the API is wired with until
real credentials exist, and a controllable double tests drive directly --
seeding rows to simulate a sheet that already has data, mutating a row's
fields to simulate a human editing it between ticks, deleting a row to
simulate one vanishing.

The worker deliberately does *not* tick against this under ``mock``: it
forgets every row on exit while the Postgres reconciliation cursor does not
(see ``workers/loop.py``).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from interlock.domain.ports.sheets import SheetRow, SheetRowWrite


class FakeSpreadsheetProvider:
    def __init__(self) -> None:
        self._rows: dict[int, SheetRow] = {}
        self._next_row_index = 2  # row 1 is the header

    def read_all_rows(self) -> Sequence[SheetRow]:
        return [self._rows[index] for index in sorted(self._rows)]

    def write_rows(self, writes: Sequence[SheetRowWrite]) -> None:
        for write in writes:
            row_index = write.row_index
            if row_index is None:
                row_index = self._next_row_index
                self._next_row_index += 1
            self._rows[row_index] = SheetRow(
                row_index=row_index,
                sys_task_id=write.sys_task_id,
                sys_version=write.sys_version,
                sys_content_hash=write.sys_content_hash,
                fields=dict(write.fields),
            )

    # -- test/dev helpers, not part of the port -----------------------------

    def seed_row(
        self,
        *,
        sys_task_id: str | None,
        sys_version: int | None,
        sys_content_hash: str | None,
        fields: dict[str, Any],
    ) -> int:
        """Add a row as if it were already in the sheet before this tick.
        Returns the row index, so a test can target it with mutate_row/
        delete_row later."""
        row_index = self._next_row_index
        self._next_row_index += 1
        self._rows[row_index] = SheetRow(
            row_index=row_index,
            sys_task_id=sys_task_id,
            sys_version=sys_version,
            sys_content_hash=sys_content_hash,
            fields=dict(fields),
        )
        return row_index

    def mutate_row(self, row_index: int, **field_updates: Any) -> None:
        """Simulate a human editing cells in ``row_index`` -- the row's
        ``_sys`` stamp is left untouched, exactly like a real protected
        column a person cannot edit."""
        current = self._rows[row_index]
        self._rows[row_index] = SheetRow(
            row_index=row_index,
            sys_task_id=current.sys_task_id,
            sys_version=current.sys_version,
            sys_content_hash=current.sys_content_hash,
            fields={**current.fields, **field_updates},
        )

    def delete_row(self, row_index: int) -> None:
        """Simulate a row vanishing -- an accidental range delete, never
        treated as intentional by the reconciler."""
        del self._rows[row_index]
