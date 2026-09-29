"""The Task entity.

Frozen on purpose. Every edit produces a new instance via :func:`apply_edit`,
which makes the before/after pair needed for history and audit fall out
naturally instead of having to be reconstructed after the fact.

Note what is *absent*: there is no ``OVERDUE`` status. Overdue is a derived
property of a due date and a status, computed in :mod:`.derivations`. Storing it
would let it drift from reality the moment a clock ticks past midnight.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from enum import StrEnum
from typing import Any

from interlock.domain.common.clock import ensure_aware
from interlock.domain.common.errors import ValidationFailedError


class TaskStatus(StrEnum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    DEFERRED = "DEFERRED"

    @property
    def is_terminal(self) -> bool:
        return self is TaskStatus.COMPLETED


class Priority(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    URGENT = "URGENT"

    @property
    def rank(self) -> int:
        """Sort weight. Higher is more urgent."""
        return _PRIORITY_RANK[self]


_PRIORITY_RANK: dict[Priority, int] = {
    Priority.LOW: 0,
    Priority.MEDIUM: 1,
    Priority.HIGH: 2,
    Priority.URGENT: 3,
}


class TaskSourceKind(StrEnum):
    """Where a task last came from. Used for audit, not for routing."""

    APP = "APP"
    GOOGLE_SHEETS = "GOOGLE_SHEETS"
    EXCEL = "EXCEL"
    IMPORT = "IMPORT"
    SHEET_IMPORT = "SHEET_IMPORT"
    """Mirrored read-only from a hand-kept sheet (services/sheet_import_service.py).
    The sheet owns these tasks: TaskService refuses to edit them."""


MAX_TITLE_LENGTH = 300
MAX_TAGS = 20

# Fields a caller is allowed to change. Anything else -- id, display_id,
# created_at, version -- is owned by the system.
EDITABLE_FIELDS: frozenset[str] = frozenset(
    {
        "title",
        "description",
        "project_id",
        "owner_id",
        "priority",
        "status",
        "due_date",
        "remarks",
        "tags",
    }
)


@dataclasses.dataclass(frozen=True, slots=True)
class Task:
    id: str
    display_id: str
    title: str
    status: TaskStatus
    priority: Priority
    created_at: dt.datetime
    updated_at: dt.datetime
    version: int

    description: str = ""
    project_id: str | None = None
    owner_id: str | None = None
    due_date: dt.date | None = None
    completed_at: dt.datetime | None = None
    remarks: str = ""
    tags: tuple[str, ...] = ()
    source: TaskSourceKind = TaskSourceKind.APP
    external_row_ref: str | None = None
    last_shared_at: dt.datetime | None = None
    deleted_at: dt.datetime | None = None

    def __post_init__(self) -> None:
        ensure_aware(self.created_at, field="task.created_at")
        ensure_aware(self.updated_at, field="task.updated_at")
        if self.completed_at is not None:
            ensure_aware(self.completed_at, field="task.completed_at")
        if not self.title.strip():
            raise ValidationFailedError("Task title cannot be empty.", field="title")
        if len(self.title) > MAX_TITLE_LENGTH:
            raise ValidationFailedError(
                f"Task title cannot exceed {MAX_TITLE_LENGTH} characters.",
                field="title",
                length=len(self.title),
            )
        if len(self.tags) > MAX_TAGS:
            raise ValidationFailedError(
                f"A task cannot carry more than {MAX_TAGS} tags.",
                field="tags",
                count=len(self.tags),
            )
        if self.version < 1:
            raise ValidationFailedError("Task version must be positive.", field="version")

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    def snapshot_fields(self) -> dict[str, Any]:
        """A plain dict of the entity, for history and audit payloads."""
        return {
            field.name: _jsonable(getattr(self, field.name))
            for field in dataclasses.fields(self)
        }


def apply_edit(
    task: Task,
    changes: dict[str, Any],
    *,
    now: dt.datetime,
) -> Task:
    """Return a new Task with ``changes`` applied and the version bumped.

    Rejects unknown or system-owned fields rather than ignoring them -- a typo in
    a field name should fail loudly, not silently drop the edit.

    ``completed_at`` is managed here rather than by the caller so that it can
    never disagree with ``status``: it is stamped when a task becomes COMPLETED
    and cleared if the task is reopened.
    """
    ensure_aware(now, field="now")

    unknown = set(changes) - EDITABLE_FIELDS
    if unknown:
        raise ValidationFailedError(
            f"Fields cannot be edited: {', '.join(sorted(unknown))}.",
            fields=sorted(unknown),
            editable=sorted(EDITABLE_FIELDS),
        )

    if not changes:
        return task

    updates: dict[str, Any] = dict(changes)

    if "tags" in updates and updates["tags"] is not None:
        updates["tags"] = tuple(updates["tags"])

    new_status = updates.get("status", task.status)
    if new_status is not task.status:
        if new_status is TaskStatus.COMPLETED:
            updates["completed_at"] = now
        elif task.status is TaskStatus.COMPLETED:
            # Reopened. Clear the completion stamp so derived state stays honest.
            updates["completed_at"] = None

    updates["updated_at"] = now
    updates["version"] = task.version + 1

    return dataclasses.replace(task, **updates)


def diff(before: Task, after: Task) -> dict[str, dict[str, Any]]:
    """Field-level changes between two versions of a task.

    Feeds the history row and the audit entry, and drives the "what changed
    today" summary shown before sharing.
    """
    changed: dict[str, dict[str, Any]] = {}
    for field in dataclasses.fields(Task):
        if field.name in {"updated_at", "version"}:
            continue
        old = getattr(before, field.name)
        new = getattr(after, field.name)
        if old != new:
            changed[field.name] = {"before": _jsonable(old), "after": _jsonable(new)}
    return changed


def _jsonable(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, tuple):
        return list(value)
    return value
