"""Task use cases: the orchestration between the pure domain and its ports.

Depends only on the ``TaskRepository`` and ``AuditSink`` ports, never on a
concrete adapter -- the composition root decides whether ``TaskRepository``
means Postgres, Google Sheets, or a test double. Nothing here talks SQL.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Sequence
from typing import Any

from interlock.domain.common.actor import Actor
from interlock.domain.common.clock import Clock
from interlock.domain.common.errors import DomainError, NotFoundError, ValidationFailedError
from interlock.domain.common.ids import TASK_PREFIX, format_display_id, new_id
from interlock.domain.ports.repositories import AuditSink, TaskFilter, TaskRepository
from interlock.domain.tasks.entities import (
    Priority,
    Task,
    TaskSourceKind,
    TaskStatus,
    apply_edit,
    diff,
)


@dataclasses.dataclass(frozen=True, slots=True)
class BulkUpdateItem:
    task_id: str
    changes: dict[str, Any]
    expected_version: int


@dataclasses.dataclass(frozen=True, slots=True)
class BulkUpdateResult:
    task_id: str
    ok: bool
    task: Task | None = None
    error_code: str | None = None
    error_message: str | None = None


class TaskService:
    def __init__(self, repo: TaskRepository, audit: AuditSink, clock: Clock) -> None:
        self._repo = repo
        self._audit = audit
        self._clock = clock

    def get(self, task_id: str) -> Task | None:
        return self._repo.get(task_id)

    def get_by_display_id(self, display_id: str) -> Task | None:
        return self._repo.get_by_display_id(display_id)

    def list_tasks(self, criteria: TaskFilter | None = None) -> Sequence[Task]:
        # Named list_tasks, not list: a method named `list` on this class
        # shadows the `list` builtin for every other method's annotations in
        # the same class body (e.g. bulk_update's `-> list[BulkUpdateResult]`)
        # under `from __future__ import annotations` lazy evaluation.
        return self._repo.list(criteria)

    def create(
        self,
        *,
        title: str,
        actor: Actor,
        description: str = "",
        project_id: str | None = None,
        owner_id: str | None = None,
        priority: Priority = Priority.MEDIUM,
        status: TaskStatus = TaskStatus.PENDING,
        due_date: dt.date | None = None,
        remarks: str = "",
        tags: tuple[str, ...] = (),
        source: TaskSourceKind = TaskSourceKind.APP,
    ) -> Task:
        now = self._clock.now()
        sequence = self._repo.next_display_sequence()
        task = Task(
            id=new_id(),
            display_id=format_display_id(TASK_PREFIX, sequence),
            title=title,
            description=description,
            project_id=project_id,
            owner_id=owner_id,
            priority=priority,
            status=status,
            created_at=now,
            updated_at=now,
            due_date=due_date,
            remarks=remarks,
            tags=tags,
            source=source,
            version=1,
        )
        self._repo.add(task)
        self._audit.record(
            action="TASK_CREATED",
            entity_type="task",
            entity_id=task.id,
            actor=str(actor),
            actor_kind=actor.kind.value,
            at=now,
            after=task.snapshot_fields(),
        )
        return task

    def update(
        self,
        task_id: str,
        changes: dict[str, Any],
        *,
        expected_version: int,
        actor: Actor,
    ) -> Task:
        """Apply ``changes`` to the task, enforcing that it was still at
        ``expected_version`` -- the caller's belief about what they were
        editing, not whatever the row's freshest version happens to be.

        The version check itself is enforced atomically by the repository
        (see PostgresTaskRepository.update), not here: this method only
        orchestrates building the new entity and recording the audit trail.
        """
        current = self._repo.get(task_id)
        if current is None:
            raise NotFoundError(f"Task {task_id} does not exist.", task_id=task_id)
        if current.source is TaskSourceKind.SHEET_IMPORT:
            # The sheet owns this task: an edit here would be silently
            # overwritten by the next import tick, within a minute.
            raise ValidationFailedError(
                f"{current.display_id} comes from your Google Sheet -- edit it there. "
                "Interlock picks the change up within a minute.",
                task_id=task_id,
                source=current.source.value,
            )

        now = self._clock.now()
        updated = apply_edit(current, changes, now=now)
        if updated is current:
            return current  # no-op edit; nothing to persist or audit

        saved = self._repo.update(updated, expected_version=expected_version)

        changed = diff(current, saved)
        self._audit.record(
            action="TASK_UPDATED",
            entity_type="task",
            entity_id=saved.id,
            actor=str(actor),
            actor_kind=actor.kind.value,
            at=now,
            before={field: change["before"] for field, change in changed.items()},
            after={field: change["after"] for field, change in changed.items()},
        )
        return saved

    def bulk_update(
        self, items: Sequence[BulkUpdateItem], *, actor: Actor
    ) -> list[BulkUpdateResult]:
        """Each item is applied independently. One item's failure -- a stale
        version, a task that no longer exists -- must not affect the others;
        the API layer renders a mixed result set as 207 Multi-Status."""
        results: list[BulkUpdateResult] = []
        for item in items:
            try:
                task = self.update(
                    item.task_id,
                    item.changes,
                    expected_version=item.expected_version,
                    actor=actor,
                )
                results.append(BulkUpdateResult(task_id=item.task_id, ok=True, task=task))
            except DomainError as exc:
                results.append(
                    BulkUpdateResult(
                        task_id=item.task_id,
                        ok=False,
                        error_code=exc.code,
                        error_message=exc.message,
                    )
                )
        return results
