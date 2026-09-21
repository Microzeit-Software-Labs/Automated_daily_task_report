"""Persistence ports.

The domain describes what it needs to load and save; adapters decide how. In
Phase 1 the only implementation is PostgreSQL. Phase 2 adds Google Sheets and
Excel behind the same :class:`TaskRepository` interface, which is why the
business logic never mentions a spreadsheet.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from interlock.domain.tasks.entities import Priority, Task, TaskStatus


@dataclasses.dataclass(frozen=True, slots=True)
class TaskFilter:
    """Query criteria for listing tasks. All fields are AND-ed."""

    statuses: frozenset[TaskStatus] | None = None
    priorities: frozenset[Priority] | None = None
    owner_id: str | None = None
    project_id: str | None = None
    tags: frozenset[str] | None = None
    due_before: dt.date | None = None
    due_after: dt.date | None = None
    search: str | None = None
    """Matches display id, title, or description."""
    include_deleted: bool = False


@runtime_checkable
class TaskRepository(Protocol):
    def get(self, task_id: str) -> Task | None: ...

    def get_by_display_id(self, display_id: str) -> Task | None: ...

    def list(self, criteria: TaskFilter | None = None) -> Sequence[Task]: ...

    def add(self, task: Task) -> Task: ...

    def update(self, task: Task, *, expected_version: int) -> Task:
        """Persist ``task``, failing if the stored version moved.

        Raises :class:`~interlock.domain.common.errors.TaskVersionConflictError` when
        another writer -- a person, or an inbound spreadsheet sync -- changed the
        row since the caller read it.
        """
        ...

    def next_display_sequence(self) -> int: ...


@runtime_checkable
class AuditSink(Protocol):
    def record(
        self,
        *,
        action: str,
        entity_type: str,
        entity_id: str,
        actor: str,
        actor_kind: str,
        at: dt.datetime,
        before: dict[str, object] | None = None,
        after: dict[str, object] | None = None,
        correlation_id: str | None = None,
    ) -> str:
        """Append one entry and return its hash. Never updates, never deletes."""
        ...


@runtime_checkable
class NotificationSink(Protocol):
    def push(
        self,
        *,
        user_id: str,
        title: str,
        body: str,
        action_url: str | None = None,
        at: dt.datetime | None = None,
    ) -> None: ...
