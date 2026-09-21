"""PostgreSQL-backed reconciliation cursor and parked conflicts.

Owned exclusively by ``services/sheet_sync_service.py`` -- nothing in the
app-facing task-mutation path (``services/task_service.py``) ever touches
these tables, the same separation ``SchedulerRepository`` has from the
approval/task tables it never mutates.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from interlock.adapters.persistence.models import SyncConflictRow, TaskSheetSyncRow
from interlock.domain.common.errors import NotFoundError, SyncConflictAlreadyResolvedError
from interlock.domain.common.ids import new_id
from interlock.domain.sync.reconciliation import SyncConflict, SyncState


class SyncRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    # -- cursor -------------------------------------------------------------

    def get_state(self, task_id: str) -> SyncState | None:
        row = self._session.get(TaskSheetSyncRow, task_id)
        return None if row is None else _state_to_entity(row)

    def upsert_state(
        self,
        task_id: str,
        *,
        version: int,
        content_hash: str,
        at: dt.datetime,
        row_index: int | None = None,
    ) -> SyncState:
        row = self._session.get(TaskSheetSyncRow, task_id)
        if row is None:
            row = TaskSheetSyncRow(
                task_id=task_id,
                last_synced_version=version,
                last_synced_content_hash=content_hash,
                last_synced_at=at,
                last_known_row_index=row_index,
            )
            self._session.add(row)
        else:
            row.last_synced_version = version
            row.last_synced_content_hash = content_hash
            row.last_synced_at = at
            if row_index is not None:
                row.last_known_row_index = row_index
        self._session.flush()
        return _state_to_entity(row)

    def most_recent_sync_at(self) -> dt.datetime | None:
        return self._session.execute(
            select(TaskSheetSyncRow.last_synced_at).order_by(
                TaskSheetSyncRow.last_synced_at.desc()
            )
        ).scalars().first()

    # -- conflicts ------------------------------------------------------------

    def has_open_conflict(self, task_id: str) -> bool:
        return (
            self._session.execute(
                select(SyncConflictRow.id).where(
                    SyncConflictRow.task_id == task_id,
                    SyncConflictRow.resolution.is_(None),
                )
            ).scalar_one_or_none()
            is not None
        )

    def record_conflict(
        self,
        task_id: str,
        *,
        detected_at: dt.datetime,
        db_value: dict[str, Any],
        external_value: dict[str, Any] | None,
    ) -> SyncConflict:
        row = SyncConflictRow(
            id=new_id(),
            task_id=task_id,
            detected_at=detected_at,
            db_value=db_value,
            external_value=external_value,
        )
        self._session.add(row)
        self._session.flush()
        return _conflict_to_entity(row)

    def get_conflict(self, conflict_id: str) -> SyncConflict | None:
        row = self._session.get(SyncConflictRow, conflict_id)
        return None if row is None else _conflict_to_entity(row)

    def list_conflicts(self, *, open_only: bool = True) -> list[SyncConflict]:
        stmt = select(SyncConflictRow)
        if open_only:
            stmt = stmt.where(SyncConflictRow.resolution.is_(None))
        stmt = stmt.order_by(SyncConflictRow.detected_at)
        rows = self._session.execute(stmt).scalars().all()
        return [_conflict_to_entity(row) for row in rows]

    def resolve_conflict(
        self,
        conflict_id: str,
        *,
        resolution: str,
        resolved_by: str,
        resolved_at: dt.datetime,
    ) -> SyncConflict:
        row = self._session.get(SyncConflictRow, conflict_id)
        if row is None:
            raise NotFoundError(
                f"Sync conflict {conflict_id} does not exist.", conflict_id=conflict_id
            )
        if row.resolution is not None:
            raise SyncConflictAlreadyResolvedError(
                f"Sync conflict {conflict_id} was already resolved.",
                conflict_id=conflict_id,
                resolution=row.resolution,
            )
        row.resolution = resolution
        row.resolved_by = resolved_by
        row.resolved_at = resolved_at
        self._session.flush()
        return _conflict_to_entity(row)


def _state_to_entity(row: TaskSheetSyncRow) -> SyncState:
    return SyncState(
        task_id=row.task_id,
        last_synced_version=row.last_synced_version,
        last_synced_content_hash=row.last_synced_content_hash,
        last_synced_at=row.last_synced_at,
        last_known_row_index=row.last_known_row_index,
    )


def _conflict_to_entity(row: SyncConflictRow) -> SyncConflict:
    return SyncConflict(
        id=row.id,
        task_id=row.task_id,
        detected_at=row.detected_at,
        db_value=dict(row.db_value),
        external_value=dict(row.external_value) if row.external_value is not None else None,
        resolution=row.resolution,
        resolved_by=row.resolved_by,
        resolved_at=row.resolved_at,
    )
