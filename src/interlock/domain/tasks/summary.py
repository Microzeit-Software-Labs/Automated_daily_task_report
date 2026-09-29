"""Task summary counts.

Drives the dashboard tiles, the review panel header, and the counts block in the
WhatsApp report. Computed once from a task list so every surface shows the same
numbers -- a summary computed twice by two different queries is a summary that
will eventually disagree with itself.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Iterable, Sequence
from zoneinfo import ZoneInfo

from interlock.domain.common.clock import local_date
from interlock.domain.tasks.derivations import is_due_today, is_overdue
from interlock.domain.tasks.entities import Task, TaskStatus


@dataclasses.dataclass(frozen=True, slots=True)
class TaskSummary:
    completed: int = 0
    pending: int = 0
    in_progress: int = 0
    blocked: int = 0
    deferred: int = 0
    overdue: int = 0
    due_today: int = 0
    newly_added: int = 0
    modified_today: int = 0
    completed_today: int = 0
    """COMPLETED with ``completed_at`` on ``today`` -- unlike ``completed``,
    which counts every completed task ever and is not a daily figure."""
    total: int = 0

    @property
    def remaining(self) -> int:
        """Everything not finished. What the report calls "Remaining"."""
        return self.total - self.completed

    def as_dict(self) -> dict[str, int]:
        result = {f.name: getattr(self, f.name) for f in dataclasses.fields(self)}
        result["remaining"] = self.remaining
        return result


def summarize(
    tasks: Iterable[Task],
    *,
    today: dt.date,
    tz: ZoneInfo,
) -> TaskSummary:
    """Count tasks into the buckets the product displays.

    Deleted tasks are excluded. Status buckets are mutually exclusive; overdue
    and due_today cut across them, so the numbers deliberately do not sum to
    ``total``.
    """
    counts = {
        TaskStatus.COMPLETED: 0,
        TaskStatus.PENDING: 0,
        TaskStatus.IN_PROGRESS: 0,
        TaskStatus.BLOCKED: 0,
        TaskStatus.DEFERRED: 0,
    }
    overdue = due_today = newly_added = modified_today = completed_today = total = 0

    for task in tasks:
        if task.is_deleted:
            continue
        total += 1
        counts[task.status] += 1
        if is_completed_on(task, today, tz):
            completed_today += 1

        if is_overdue(task.due_date, task.status, today):
            overdue += 1
        if is_due_today(task.due_date, task.status, today):
            due_today += 1

        created_on = local_date(task.created_at, tz)
        if created_on == today:
            newly_added += 1
        elif local_date(task.updated_at, tz) == today:
            # "Modified today" excludes tasks created today -- those are already
            # counted as new, and counting them twice overstates activity.
            modified_today += 1

    return TaskSummary(
        completed=counts[TaskStatus.COMPLETED],
        pending=counts[TaskStatus.PENDING],
        in_progress=counts[TaskStatus.IN_PROGRESS],
        blocked=counts[TaskStatus.BLOCKED],
        deferred=counts[TaskStatus.DEFERRED],
        overdue=overdue,
        due_today=due_today,
        newly_added=newly_added,
        modified_today=modified_today,
        completed_today=completed_today,
        total=total,
    )


def is_completed_on(task: Task, day: dt.date, tz: ZoneInfo) -> bool:
    return (
        task.status is TaskStatus.COMPLETED
        and task.completed_at is not None
        and local_date(task.completed_at, tz) == day
    )


@dataclasses.dataclass(frozen=True, slots=True)
class ChangeSummary:
    """"Changes since last share" -- shown before the user approves."""

    added: int = 0
    completed: int = 0
    modified: int = 0
    overdue: int = 0
    still_pending: int = 0

    def as_dict(self) -> dict[str, int]:
        return {f.name: getattr(self, f.name) for f in dataclasses.fields(self)}


def summarize_changes(
    tasks: Sequence[Task],
    *,
    since: dt.datetime | None,
    today: dt.date,
) -> ChangeSummary:
    """What changed since ``since`` (normally the last successful share).

    When ``since`` is ``None`` nothing has ever been shared, so every live task
    counts as added.
    """
    added = completed = modified = overdue = still_pending = 0

    for task in tasks:
        if task.is_deleted:
            continue

        if since is None or task.created_at > since:
            added += 1
        elif task.updated_at > since:
            modified += 1

        if task.status is TaskStatus.COMPLETED and (
            since is None or (task.completed_at is not None and task.completed_at > since)
        ):
            completed += 1

        if is_overdue(task.due_date, task.status, today):
            overdue += 1
        if task.status is TaskStatus.PENDING:
            still_pending += 1

    return ChangeSummary(
        added=added,
        completed=completed,
        modified=modified,
        overdue=overdue,
        still_pending=still_pending,
    )
