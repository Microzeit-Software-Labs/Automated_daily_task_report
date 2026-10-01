"""The one-row ``sheet_source`` table: which sheet, and how the last read went."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from interlock.adapters.persistence.models import SheetSourceRow
from interlock.domain.sync.sheet_source import SheetSourceState


def _to_state(row: SheetSourceRow) -> SheetSourceState:
    return SheetSourceState(
        url=row.url,
        scope=row.scope,
        updated_at=row.updated_at,
        updated_by=row.updated_by,
        import_pending=row.import_pending,
        last_attempt_at=row.last_attempt_at,
        last_success_at=row.last_success_at,
        last_task_count=row.last_task_count,
        last_error_code=row.last_error_code,
        last_error=row.last_error,
    )


class SheetSourceRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self) -> SheetSourceState | None:
        # populate_existing: the status columns are written by UPDATE statements
        # (and by another process, the worker), so never trust a cached object.
        row = self._session.execute(
            select(SheetSourceRow)
            .where(SheetSourceRow.id == 1)
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        return None if row is None else _to_state(row)

    def create_if_absent(
        self, *, url: str | None, scope: str | None, by: str, now: dt.datetime
    ) -> bool:
        """Record the first value. A second process racing to do the same loses
        quietly (returns False) instead of failing."""
        result = self._session.execute(
            pg_insert(SheetSourceRow)
            .values(
                id=1, url=url, scope=scope, updated_at=now, updated_by=by, import_pending=False
            )
            .on_conflict_do_nothing(index_elements=["id"])
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]

    def set_link(self, *, url: str, scope: str, by: str, now: dt.datetime) -> None:
        """Point at a (possibly different) sheet and ask for an import at once.
        The previous sheet's read status is cleared: it describes another sheet."""
        values = {
            "url": url,
            "scope": scope,
            "updated_at": now,
            "updated_by": by,
            "import_pending": True,
            "last_attempt_at": None,
            "last_success_at": None,
            "last_task_count": None,
            "last_error_code": None,
            "last_error": None,
        }
        self._session.execute(
            pg_insert(SheetSourceRow)
            .values(id=1, **values)
            .on_conflict_do_update(index_elements=["id"], set_=values)
        )

    def request_import(self) -> None:
        """Read the same sheet again soon (the worker acts on its next tick)."""
        self._session.execute(
            update(SheetSourceRow).where(SheetSourceRow.id == 1).values(import_pending=True)
        )

    def record_success(self, *, now: dt.datetime, task_count: int) -> None:
        self._session.execute(
            update(SheetSourceRow)
            .where(SheetSourceRow.id == 1)
            .values(
                import_pending=False,
                last_attempt_at=now,
                last_success_at=now,
                last_task_count=task_count,
                last_error_code=None,
                last_error=None,
            )
        )

    def record_failure(self, *, now: dt.datetime, code: str, message: str) -> None:
        self._session.execute(
            update(SheetSourceRow)
            .where(SheetSourceRow.id == 1)
            .values(
                import_pending=False,
                last_attempt_at=now,
                last_error_code=code,
                last_error=message,
            )
        )

    def stamp_legacy_refs(self, scope: str) -> int:
        """Give imported tasks whose ref predates scopes (``sr:5``) the scope of
        the sheet they came from (``<scope>|sr:5``). Idempotent: a scoped ref is
        never touched. Only the ref changes, so the task's version and history
        are left alone."""
        result = self._session.execute(
            text(
                "UPDATE tasks SET external_row_ref = :scope || '|' || external_row_ref "
                "WHERE source = 'SHEET_IMPORT' AND external_row_ref LIKE 'sr:%'"
            ),
            {"scope": scope},
        )
        return int(result.rowcount)  # type: ignore[attr-defined]
