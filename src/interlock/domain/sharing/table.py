"""The report as a table: what the image shows, decided here, drawn elsewhere.

The WhatsApp report is sent as a picture of a plain, sheet-style table plus a
one-line caption. This module decides the *content* -- which tasks, which
columns, what each cell says -- as pure data, so it is unit-testable and the
same rules as the text report apply (``select_report_tasks``). Turning a
``ReportTable`` into PNG bytes is I/O (fonts) and lives behind
``domain/ports/rendering.py``.

Deliberately monochrome: no colour coding. Overdue is stated in words in the
Deadline cell, not signalled by colour.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Sequence
from zoneinfo import ZoneInfo

from interlock.domain.common.clock import ensure_aware
from interlock.domain.sharing.render import STATUS_LABELS, select_report_tasks
from interlock.domain.sync.sheet_source import sr_no_of_ref
from interlock.domain.tasks.derivations import is_overdue
from interlock.domain.tasks.entities import Task
from interlock.domain.tasks.summary import TaskSummary

TABLE_TEMPLATE_ID = "table.v1"
MAX_ROWS = 40

TABLE_TITLES: dict[str, str] = {
    "MORNING": "Morning Task Update",
    "EVENING": "End of Day Task Update",
    "MANUAL": "Task Update",
}


@dataclasses.dataclass(frozen=True, slots=True)
class ReportTable:
    title: str
    subtitle: str
    columns: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    footer: str
    wide_columns: frozenset[int] = frozenset()
    """Indexes of free-text columns that get the leftover width and wrap."""


def row_number(task: Task) -> str:
    """The sheet's own ``Sr No.`` for an imported task, else the display id."""
    return sr_no_of_ref(task.external_row_ref) or task.display_id


def _deadline_cell(task: Task, today: dt.date) -> str:
    if task.due_date is None:
        return ""
    text = f"{task.due_date:%d %b %Y}"
    return f"{text} (overdue)" if is_overdue(task.due_date, task.status, today) else text


def _counts(summary: TaskSummary) -> str:
    parts = [
        f"Completed today {summary.completed_today}",
        f"In progress {summary.in_progress}",
        f"Pending {summary.pending}",
        f"Overdue {summary.overdue}",
    ]
    if summary.blocked:
        parts.append(f"Blocked {summary.blocked}")
    return " · ".join(parts)


def build_report_table(
    tasks: Sequence[Task],
    summary: TaskSummary,
    *,
    kind: str,
    now: dt.datetime,
    tz: ZoneInfo,
    max_rows: int = MAX_ROWS,
) -> ReportTable:
    ensure_aware(now, field="now")
    local = now.astimezone(tz)
    today = local.date()
    listed = select_report_tasks(tasks, today=today, tz=tz)
    shown = listed[:max_rows]

    has_deadline = any(t.due_date is not None for t in shown)
    has_note = any(t.remarks.strip() for t in shown)

    columns = ["No.", "Task", "Status"]
    wide = {1}
    if has_deadline:
        columns.append("Deadline")
    if has_note:
        columns.append("Note")
        wide.add(len(columns) - 1)

    rows = []
    for task in shown:
        row = [row_number(task), task.title, STATUS_LABELS[task.status]]
        if has_deadline:
            row.append(_deadline_cell(task, today))
        if has_note:
            row.append(task.remarks.strip())
        rows.append(tuple(row))

    footer = _counts(summary)
    if len(listed) > len(shown):
        footer += f" · {len(listed) - len(shown)} more not shown"

    return ReportTable(
        title=TABLE_TITLES.get(kind, "Task Update"),
        subtitle=f"{local:%A, %d %B %Y} · {local:%H:%M}",
        columns=tuple(columns),
        rows=tuple(rows),
        footer=footer,
        wide_columns=frozenset(wide),
    )


def render_caption(summary: TaskSummary, *, kind: str, now: dt.datetime, tz: ZoneInfo) -> str:
    """The text sent with the image. Plain and short: no emoji."""
    ensure_aware(now, field="now")
    local = now.astimezone(tz)
    title = TABLE_TITLES.get(kind, "Task Update")
    return f"*{title}* — {local:%d %b %Y}, {local:%H:%M}\n{_counts(summary)}"
