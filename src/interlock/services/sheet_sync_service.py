"""The Google Sheets reconciliation tick.

Callable directly from a test with a `FakeSpreadsheetProvider`, no network --
same "test scheduling without touching the real thing" shape as
``scheduler_service.py`` and its `MockWhatsAppProvider`.

One tick does a full comparison pass: read every sheet row and every task
once, and for each task decide NOOP / PUSH / PULL / CONFLICT by comparing
both sides against the last point they were known to agree
(``SyncRepository``'s cursor) -- see ``domain/sync/reconciliation.py`` for
the decision table itself. Deliberately not an outbox: at this project's
scale (one user, one laptop, daily cadence) a full scan every tick is
simpler than coalescing writes and just as correct. See the Phase 2 plan's
"deviation 1" for the full reasoning.

Inbound writes (PULL, and minting a task from a brand new sheet row)
bypass ``TaskService`` and go straight through ``TaskRepository`` +
``apply_edit`` -- this is a peer write, not an app-facing mutation, the same
reasoning ``workers/loop.py`` already applies when it constructs
``SchedulerService``/``ReviewService`` directly instead of routing through
the API layer.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from typing import Any

from interlock.adapters.persistence.sync_repository import SyncRepository
from interlock.domain.common.actor import ActorKind, sync_actor
from interlock.domain.common.errors import NotFoundError, ValidationFailedError
from interlock.domain.common.ids import TASK_PREFIX, format_display_id, new_id
from interlock.domain.ports.repositories import AuditSink, TaskFilter, TaskRepository
from interlock.domain.ports.sheets import SheetRow, SheetRowWrite, SpreadsheetProvider
from interlock.domain.sync.reconciliation import (
    SyncAction,
    SyncConflict,
    content_hash,
    decide_action,
    editable_fields,
    task_content_fields,
)
from interlock.domain.tasks.entities import (
    Priority,
    Task,
    TaskSourceKind,
    TaskStatus,
    apply_edit,
    diff,
)

_SYNC_SOURCE = "google_sheets"

# Fields where a cleared/blank cell is a meaningful value (clear the field).
# Everywhere else, a cell that fails to parse is dropped from the change set
# for this tick rather than nulling out a field Task requires -- the sheet
# gets the current DB value re-stamped and the row can be fixed and picked
# up again next tick, instead of the tick crashing on one bad cell.
_NULLABLE_FIELDS = frozenset({"project_id", "owner_id", "due_date"})


@dataclasses.dataclass(frozen=True, slots=True)
class SyncTickResult:
    pushed: int = 0
    pulled: int = 0
    created: int = 0
    conflicts: int = 0
    vanished: int = 0


class SheetSyncService:
    def __init__(
        self,
        *,
        sheets: SpreadsheetProvider,
        sync_repo: SyncRepository,
        task_repo: TaskRepository,
        audit: AuditSink,
    ) -> None:
        self._sheets = sheets
        self._sync = sync_repo
        self._tasks = task_repo
        self._audit = audit

    def tick(self, *, now: dt.datetime) -> SyncTickResult:
        all_rows = self._sheets.read_all_rows()
        sheet_rows = {row.sys_task_id: row for row in all_rows if row.sys_task_id}
        untracked_rows = [row for row in all_rows if not row.sys_task_id]
        tasks = self._tasks.list(TaskFilter())

        writes: list[SheetRowWrite] = []
        pushed = pulled = conflicts = vanished = 0

        for task in tasks:
            sheet_row = sheet_rows.get(task.id)
            state = self._sync.get_state(task.id)

            if sheet_row is None:
                if state is None:
                    # Never synced and no row to match -- a task created in
                    # the app before this ever ran, or since the last tick.
                    writes.append(self._push_write(task))
                    self._stamp_synced(task, at=now)
                    pushed += 1
                elif not self._sync.has_open_conflict(task.id):
                    # Was synced to a row that is no longer there. Per the
                    # architecture doc's R5, a disappearing row is never
                    # treated as a deletion -- park it instead.
                    self._sync.record_conflict(
                        task.id,
                        detected_at=now,
                        db_value=task.snapshot_fields(),
                        external_value=None,
                    )
                    vanished += 1
                continue

            sheet_hash = content_hash(editable_fields(sheet_row.fields))
            db_changed = state is None or task.version != state.last_synced_version
            sheet_changed = state is None or sheet_hash != state.last_synced_content_hash
            action = decide_action(db_changed=db_changed, sheet_changed=sheet_changed)

            if action is SyncAction.NOOP:
                continue

            if action is SyncAction.PUSH:
                writes.append(self._push_write(task, row_index=sheet_row.row_index))
                self._stamp_synced(task, at=now, row_index=sheet_row.row_index)
                pushed += 1
                continue

            if action is SyncAction.PULL:
                updated = self._pull(task, sheet_row, now=now)
                writes.append(self._push_write(updated, row_index=sheet_row.row_index))
                self._stamp_synced(updated, at=now, row_index=sheet_row.row_index)
                pulled += 1
                continue

            if action is SyncAction.CONFLICT and not self._sync.has_open_conflict(task.id):
                self._sync.record_conflict(
                    task.id,
                    detected_at=now,
                    db_value=task.snapshot_fields(),
                    external_value=sheet_row.fields,
                )
                conflicts += 1

        created = 0
        for row in untracked_rows:
            new_task = self._create_from_row(row, now=now)
            writes.append(self._push_write(new_task, row_index=row.row_index))
            self._stamp_synced(new_task, at=now, row_index=row.row_index)
            created += 1

        if writes:
            self._sheets.write_rows(writes)

        return SyncTickResult(
            pushed=pushed, pulled=pulled, created=created, conflicts=conflicts, vanished=vanished
        )

    def resolve_conflict(
        self, conflict_id: str, *, resolution: str, resolved_by: str, now: dt.datetime
    ) -> SyncConflict:
        """A human's decision on a parked collision. Either side may be
        chosen -- the conflict itself never implies which one is "right".

        Re-reads the sheet to relocate the task's row fresh rather than
        trusting anything cached, for the same reason every other write in
        this service does: row position drifts, identity does not.
        """
        conflict = self._sync.get_conflict(conflict_id)
        if conflict is None:
            raise NotFoundError(
                f"Sync conflict {conflict_id} does not exist.", conflict_id=conflict_id
            )
        task = self._tasks.get(conflict.task_id)
        if task is None:
            raise NotFoundError(
                f"Task {conflict.task_id} does not exist.", task_id=conflict.task_id
            )

        current_row = next(
            (row for row in self._sheets.read_all_rows() if row.sys_task_id == task.id), None
        )
        row_index = current_row.row_index if current_row is not None else None

        if resolution == "kept_db":
            write = self._push_write(task, row_index=row_index)
            self._sheets.write_rows([write])
            self._stamp_synced(task, at=now, row_index=write.row_index)
        elif resolution == "kept_sheet":
            if conflict.external_value is None:
                raise ValidationFailedError(
                    "Cannot keep the sheet's value: the row no longer exists.",
                    conflict_id=conflict_id,
                )
            pseudo_row = SheetRow(
                row_index=row_index or 0,
                sys_task_id=task.id,
                sys_version=task.version,
                sys_content_hash=None,
                fields=conflict.external_value,
            )
            updated = self._pull(task, pseudo_row, now=now)
            write = self._push_write(updated, row_index=row_index)
            self._sheets.write_rows([write])
            self._stamp_synced(updated, at=now, row_index=write.row_index)
        else:
            raise ValidationFailedError(
                f"Unknown resolution {resolution!r}.", resolution=resolution
            )

        return self._sync.resolve_conflict(
            conflict_id, resolution=resolution, resolved_by=resolved_by, resolved_at=now
        )

    # -- helpers --------------------------------------------------------------

    def _stamp_synced(self, task: Task, *, at: dt.datetime, row_index: int | None = None) -> None:
        self._sync.upsert_state(
            task.id,
            version=task.version,
            content_hash=content_hash(task_content_fields(task)),
            at=at,
            row_index=row_index,
        )

    def _push_write(self, task: Task, *, row_index: int | None = None) -> SheetRowWrite:
        fields = task_content_fields(task)
        return SheetRowWrite(
            row_index=row_index,
            sys_task_id=task.id,
            sys_version=task.version,
            sys_content_hash=content_hash(fields),
            display_id=task.display_id,
            fields=fields,
        )

    def _pull(self, task: Task, sheet_row: SheetRow, *, now: dt.datetime) -> Task:
        changes = _sheet_changes(task, sheet_row)
        if not changes:
            return task

        updated = apply_edit(task, changes, now=now)
        saved = self._tasks.update(updated, expected_version=task.version)

        changed = diff(task, saved)
        actor = sync_actor(_SYNC_SOURCE)
        self._audit.record(
            action="TASK_UPDATED",
            entity_type="task",
            entity_id=saved.id,
            actor=str(actor),
            actor_kind=ActorKind.SYNC.value,
            at=now,
            before={field: change["before"] for field, change in changed.items()},
            after={field: change["after"] for field, change in changed.items()},
        )
        return saved

    def _create_from_row(self, row: SheetRow, *, now: dt.datetime) -> Task:
        fields = editable_fields(row.fields)
        title = fields.get("title") or "Untitled (from Google Sheets)"
        sequence = self._tasks.next_display_sequence()
        task = Task(
            id=new_id(),
            display_id=format_display_id(TASK_PREFIX, sequence),
            title=title,
            status=_coerce("status", fields.get("status")) or TaskStatus.PENDING,
            priority=_coerce("priority", fields.get("priority")) or Priority.MEDIUM,
            created_at=now,
            updated_at=now,
            version=1,
            description=fields.get("description") or "",
            project_id=fields.get("project_id"),
            owner_id=fields.get("owner_id"),
            due_date=_coerce("due_date", fields.get("due_date")),
            remarks=fields.get("remarks") or "",
            tags=tuple(fields.get("tags") or ()),
            source=TaskSourceKind.GOOGLE_SHEETS,
        )
        self._tasks.add(task)

        actor = sync_actor(_SYNC_SOURCE)
        self._audit.record(
            action="TASK_CREATED",
            entity_type="task",
            entity_id=task.id,
            actor=str(actor),
            actor_kind=ActorKind.SYNC.value,
            at=now,
            after=task.snapshot_fields(),
        )
        return task


def _sheet_changes(task: Task, sheet_row: SheetRow) -> dict[str, Any]:
    """Only the fields the sheet actually offers a different, *parseable*
    value for -- see the module docstring's note on why an unparseable cell
    is dropped rather than nulling a required field."""
    changes: dict[str, Any] = {}
    for name, raw_value in editable_fields(sheet_row.fields).items():
        coerced = _coerce(name, raw_value)
        if coerced is None and name not in _NULLABLE_FIELDS:
            continue
        if coerced != getattr(task, name):
            changes[name] = coerced
    return changes


def _coerce(name: str, value: Any) -> Any:
    """Sheet cells arrive JSON-shaped (plain strings/lists); a few fields
    need converting to the type Task actually stores. Returns None for a
    value that does not parse -- the caller decides whether that means
    "clear the field" or "ignore this cell this tick"."""
    if value is None:
        return None
    if name == "priority":
        try:
            return Priority(value)
        except ValueError:
            return None
    if name == "status":
        try:
            return TaskStatus(value)
        except ValueError:
            return None
    if name == "due_date":
        try:
            return dt.date.fromisoformat(value) if value else None
        except (TypeError, ValueError):
            return None
    if name == "tags":
        return tuple(value) if value else ()
    return value
