"""Derived task state.

These are the rules the brief calls "task intelligence". All of them are pure
functions of (task, today) so they can never disagree with stored data -- there
is nothing stored to disagree with.

The single most important rule here: **a task is never automatically completed
because its due date passed.** Overdue is a signal, not a state change.
"""

from __future__ import annotations

import dataclasses
import datetime as dt

from interlock.domain.tasks.entities import Task, TaskStatus

DEFAULT_DUE_SOON_DAYS = 2


def is_overdue(
    due_date: dt.date | None,
    status: TaskStatus,
    today: dt.date,
) -> bool:
    """Past its due date and not finished.

    A COMPLETED task is never overdue, however late it was completed. A task
    with no due date can never be overdue.
    """
    if due_date is None:
        return False
    if status is TaskStatus.COMPLETED:
        return False
    return due_date < today


def is_due_today(due_date: dt.date | None, status: TaskStatus, today: dt.date) -> bool:
    if due_date is None or status is TaskStatus.COMPLETED:
        return False
    return due_date == today


def is_due_soon(
    due_date: dt.date | None,
    status: TaskStatus,
    today: dt.date,
    *,
    horizon_days: int = DEFAULT_DUE_SOON_DAYS,
) -> bool:
    """Approaching its due date but not yet there.

    Excludes today (that is ``is_due_today``) and excludes anything already
    overdue, so the three buckets never overlap.
    """
    if due_date is None or status is TaskStatus.COMPLETED:
        return False
    delta = (due_date - today).days
    return 0 < delta <= horizon_days


@dataclasses.dataclass(frozen=True, slots=True)
class TaskFlags:
    """Everything derived about one task at one moment."""

    overdue: bool
    due_today: bool
    due_soon: bool
    days_until_due: int | None

    @property
    def needs_attention(self) -> bool:
        return self.overdue or self.due_today


def derive_flags(
    task: Task,
    today: dt.date,
    *,
    horizon_days: int = DEFAULT_DUE_SOON_DAYS,
) -> TaskFlags:
    return TaskFlags(
        overdue=is_overdue(task.due_date, task.status, today),
        due_today=is_due_today(task.due_date, task.status, today),
        due_soon=is_due_soon(task.due_date, task.status, today, horizon_days=horizon_days),
        days_until_due=None if task.due_date is None else (task.due_date - today).days,
    )


def sort_key_for_report(task: Task, today: dt.date) -> tuple[int, int, str]:
    """Ordering for the shared report: most urgent first.

    Overdue outranks priority, because "this was due yesterday" is more
    actionable than "this is marked High". Ties break on display id so the
    ordering is stable between renders of the same data.
    """
    overdue_rank = 0 if is_overdue(task.due_date, task.status, today) else 1
    return (overdue_rank, -task.priority.rank, task.display_id)
