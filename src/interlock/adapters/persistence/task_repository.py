"""PostgreSQL-backed TaskRepository.

Implements :class:`interlock.domain.ports.repositories.TaskRepository`. Bound
to a ``Session`` rather than owning its own engine or transaction, so its
writes participate in whatever transaction the caller already opened -- that
is what lets a task edit and its audit entry commit or roll back together.

``update()`` is the one method worth reading carefully. It performs the
version check and the write as a single conditional ``UPDATE ... WHERE id = :id
AND version = :expected`` rather than fetching the row, comparing versions in
Python, and then writing. The two-step version has a race window: between the
read and the write, a concurrent transaction could commit a conflicting
change, and the Python-level comparison -- having already read a
now-stale version -- would never see it. Folding the check into the ``WHERE``
clause makes it atomic: PostgreSQL either finds a row that still matches
``expected_version`` and updates it, or it does not, and there is no window in
between where the wrong answer could be given.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

from sqlalchemy import CursorResult, select, update
from sqlalchemy.orm import Session

from interlock.adapters.persistence.models import TaskRow
from interlock.adapters.persistence.sequences import PostgresSequences
from interlock.domain.common.errors import NotFoundError, TaskVersionConflictError
from interlock.domain.ports.repositories import TaskFilter
from interlock.domain.tasks.entities import Priority, Task, TaskSourceKind, TaskStatus


class PostgresTaskRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, task_id: str) -> Task | None:
        row = self._session.get(TaskRow, task_id)
        return None if row is None else _to_entity(row)

    def get_by_display_id(self, display_id: str) -> Task | None:
        row = self._session.execute(
            select(TaskRow).where(TaskRow.display_id == display_id)
        ).scalar_one_or_none()
        return None if row is None else _to_entity(row)

    def list(self, criteria: TaskFilter | None = None) -> Sequence[Task]:
        criteria = criteria or TaskFilter()
        stmt = select(TaskRow)

        if not criteria.include_deleted:
            stmt = stmt.where(TaskRow.deleted_at.is_(None))
        if criteria.statuses:
            stmt = stmt.where(TaskRow.status.in_([s.value for s in criteria.statuses]))
        if criteria.priorities:
            stmt = stmt.where(TaskRow.priority.in_([p.value for p in criteria.priorities]))
        if criteria.owner_id:
            stmt = stmt.where(TaskRow.owner_id == criteria.owner_id)
        if criteria.project_id:
            stmt = stmt.where(TaskRow.project_id == criteria.project_id)
        if criteria.due_before:
            stmt = stmt.where(TaskRow.due_date < criteria.due_before)
        if criteria.due_after:
            stmt = stmt.where(TaskRow.due_date > criteria.due_after)
        if criteria.tags:
            stmt = stmt.where(TaskRow.tags.overlap(list(criteria.tags)))
        if criteria.search:
            like = f"%{criteria.search}%"
            stmt = stmt.where(
                TaskRow.display_id.ilike(like)
                | TaskRow.title.ilike(like)
                | TaskRow.description.ilike(like)
            )

        stmt = stmt.order_by(TaskRow.created_at)
        rows = self._session.execute(stmt).scalars().all()
        return [_to_entity(row) for row in rows]

    def add(self, task: Task) -> Task:
        self._session.add(_to_row(task))
        self._session.flush()
        return task

    def update(self, task: Task, *, expected_version: int) -> Task:
        # Cast to CursorResult: an UPDATE genuinely returns one at runtime,
        # with a real .rowcount -- Session.execute()'s stub return type
        # (Result[Any]) is generic enough to cover a SELECT too, where
        # rowcount would not be meaningful, so it omits the attribute.
        result = cast(
            "CursorResult[Any]",
            self._session.execute(
                update(TaskRow)
                .where(TaskRow.id == task.id, TaskRow.version == expected_version)
                .values(**_row_values(task))
            ),
        )

        if result.rowcount == 0:
            # Either the row does not exist, or it moved past the version the
            # caller thought they were editing. Distinguish which, so the
            # error is accurate rather than a generic "update failed".
            current_version = self._session.execute(
                select(TaskRow.version).where(TaskRow.id == task.id)
            ).scalar_one_or_none()
            if current_version is None:
                raise NotFoundError(f"Task {task.id} does not exist.", task_id=task.id)
            raise TaskVersionConflictError(
                f"Task {task.display_id} changed since you opened this review.",
                expected_version=expected_version,
                actual_version=current_version,
                task_id=task.id,
                display_id=task.display_id,
            )

        self._session.flush()
        return task

    def next_display_sequence(self) -> int:
        return PostgresSequences(self._session).next("task")


def _to_entity(row: TaskRow) -> Task:
    return Task(
        id=row.id,
        display_id=row.display_id,
        title=row.title,
        description=row.description,
        project_id=row.project_id,
        owner_id=row.owner_id,
        priority=Priority(row.priority),
        status=TaskStatus(row.status),
        created_at=row.created_at,
        updated_at=row.updated_at,
        due_date=row.due_date,
        completed_at=row.completed_at,
        remarks=row.remarks,
        tags=tuple(row.tags),
        source=TaskSourceKind(row.source),
        external_row_ref=row.external_row_ref,
        last_shared_at=row.last_shared_at,
        deleted_at=row.deleted_at,
        version=row.version,
    )


def _to_row(task: Task) -> TaskRow:
    return TaskRow(id=task.id, **_row_values(task))


def _row_values(task: Task) -> dict[str, object]:
    """Every column except ``id`` -- shared between insert and the
    conditional update so the two can never drift apart field by field."""
    return {
        "display_id": task.display_id,
        "title": task.title,
        "description": task.description,
        "project_id": task.project_id,
        "owner_id": task.owner_id,
        "priority": task.priority.value,
        "status": task.status.value,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "due_date": task.due_date,
        "completed_at": task.completed_at,
        "remarks": task.remarks,
        "tags": list(task.tags),
        "source": task.source.value,
        "external_row_ref": task.external_row_ref,
        "last_shared_at": task.last_shared_at,
        "deleted_at": task.deleted_at,
        "version": task.version,
    }
