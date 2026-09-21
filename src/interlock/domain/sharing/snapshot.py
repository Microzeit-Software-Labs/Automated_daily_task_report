"""Freezing a report.

The most important guarantee in this product after the approval gate itself:
**what you approved is what gets sent.** When a user approves a report at 17:04
and schedules it for 21:30, the bytes that leave at 21:30 are the bytes they
read at 17:04 -- not a fresh render against four-hours-newer data.

A snapshot is therefore immutable. It captures the rendered message, the exact
task rows behind it, and a content hash used to detect drift. The database
revokes UPDATE and DELETE on the snapshot table for the application role.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
from collections.abc import Sequence
from typing import Any
from zoneinfo import ZoneInfo

from interlock.domain.common.clock import ensure_aware
from interlock.domain.common.ids import new_id
from interlock.domain.tasks.entities import Task
from interlock.domain.tasks.summary import TaskSummary

# Fields that participate in the content hash. Deliberately excludes timestamps
# and version numbers: re-saving a task without changing anything a reader would
# notice should not register as drift.
_HASHED_FIELDS = (
    "id",
    "display_id",
    "title",
    "description",
    "status",
    "priority",
    "project_id",
    "owner_id",
    "due_date",
    "remarks",
    "tags",
)


def compute_content_hash(tasks: Sequence[Task]) -> str:
    """A stable fingerprint of the task data a report is built from.

    Order-independent: the same tasks in a different order hash identically, so
    a re-sort never looks like a change.
    """
    rows = sorted(
        json.dumps(
            {field: _hashable(getattr(task, field)) for field in _HASHED_FIELDS},
            sort_keys=True,
            separators=(",", ":"),
        )
        for task in tasks
        if not task.is_deleted
    )
    digest = hashlib.sha256()
    for row in rows:
        digest.update(row.encode("utf-8"))
        digest.update(b"\x1e")
    return digest.hexdigest()


def format_dataset_version(now: dt.datetime, tz: ZoneInfo, sequence: int) -> str:
    """``2026-09-15-17-04-v12``.

    Human-readable on purpose -- it appears in the audit log and in support
    conversations, where "which data went out?" needs an answer someone can read
    aloud. ``sequence`` is the count of snapshots taken that day.
    """
    ensure_aware(now, field="now")
    local = now.astimezone(tz)
    return f"{local:%Y-%m-%d-%H-%M}-v{sequence}"


@dataclasses.dataclass(frozen=True, slots=True)
class ReportSnapshot:
    """An immutable, sendable report."""

    id: str
    dataset_version: str
    content_hash: str
    rendered_body: str
    task_state: tuple[dict[str, Any], ...]
    summary: dict[str, int]
    template_id: str
    created_at: dt.datetime

    @property
    def task_count(self) -> int:
        return len(self.task_state)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "dataset_version": self.dataset_version,
            "content_hash": self.content_hash,
            "rendered_body": self.rendered_body,
            "task_state": list(self.task_state),
            "summary": dict(self.summary),
            "template_id": self.template_id,
            "created_at": self.created_at.isoformat(),
        }


def freeze(
    *,
    tasks: Sequence[Task],
    summary: TaskSummary,
    rendered_body: str,
    template_id: str,
    now: dt.datetime,
    tz: ZoneInfo,
    sequence: int,
) -> ReportSnapshot:
    """Capture a report and the data behind it, permanently.

    Called once, at approval. Never recomputed.
    """
    ensure_aware(now, field="now")
    live = [task for task in tasks if not task.is_deleted]
    return ReportSnapshot(
        id=new_id(),
        dataset_version=format_dataset_version(now, tz, sequence),
        content_hash=compute_content_hash(live),
        rendered_body=rendered_body,
        task_state=tuple(task.snapshot_fields() for task in live),
        summary=summary.as_dict(),
        template_id=template_id,
        created_at=now,
    )


def _hashable(value: Any) -> Any:
    if isinstance(value, dt.date | dt.datetime):
        return value.isoformat()
    if isinstance(value, tuple):
        return sorted(str(item) for item in value)
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return str(value)
