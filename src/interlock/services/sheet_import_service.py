"""The read-only sheet import tick: mirror a hand-kept sheet into ``tasks``.

One-way and read-only by construction -- ``fetch`` only ever reads, and this
service never writes anywhere but Postgres. The sheet is the source of truth
for the tasks it owns (``TaskSourceKind.SHEET_IMPORT``, matched by
``external_row_ref = "sr:<Sr No.>"``); ``TaskService`` refuses to edit them,
so there is no Interlock-side edit for a later tick to silently revert.

Inbound writes bypass ``TaskService`` and go through ``TaskRepository`` +
``apply_edit`` directly, audited as ``sync:sheet_import`` -- the same peer-
write reasoning ``sheet_sync_service.py`` documents.

Rows marked as someone else's ("Subhan's Task") are left out of reports by
hiding the task through the existing soft delete (``deleted_at``), which
every summary, report and default task listing already skips; they reappear
if the row's status changes back.

A row that vanishes from the sheet hides its task too (tagged ``removed:sheet``),
and the task returns if the row does. This mirror is one-way and the sheet is the
only place tasks are edited, so a row deleted or renumbered there must stop being
reported; leaving it behind showed a task as "In Progress" long after the user
had deleted it. (The Phase 0 rule "a vanished row is never a deletion" is for the
two-way sync, where the sheet is only a peer.) Nothing is hidden when the sheet
yielded no tasks at all, and a task whose row is merely *skipped* (a duplicate or
blank ``Sr No.``) is left alone, because that row is still in the sheet.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import re
from collections.abc import Callable, Iterable
from typing import Any
from zoneinfo import ZoneInfo

from interlock.domain.common.actor import ActorKind, sync_actor
from interlock.domain.common.clock import combine_local, local_date
from interlock.domain.common.ids import TASK_PREFIX, format_display_id, new_id
from interlock.domain.ports.repositories import AuditSink, TaskFilter, TaskRepository
from interlock.domain.sync.sheet_import import ImportedRow, SkippedRow, parse_sheet
from interlock.domain.sync.sheet_source import REMOVED_TAG, RETIRED_TAG, scope_of_ref, scoped_ref
from interlock.domain.tasks.entities import (
    Priority,
    Task,
    TaskSourceKind,
    TaskStatus,
    apply_edit,
    diff,
)

_ACTOR = sync_actor("sheet_import")
_DELEGATED_TAG = "delegated:"
_SKIPPED_SR_NO = re.compile(r"Sr No\. (.+)")
"""How ``SkippedRow.detail`` names the row's number (see ``parse_sheet``)."""
_MAX_TAG_LENGTH = 50


@dataclasses.dataclass(frozen=True, slots=True)
class ImportResult:
    created: int = 0
    updated: int = 0
    hidden: int = 0
    unhidden: int = 0
    retired: int = 0
    """Tasks that came from a different sheet, hidden now that this one is in use."""
    removed: int = 0
    """Tasks hidden because their row is no longer in the sheet."""
    unchanged: int = 0
    rows_read: int = 0
    """Tasks the sheet holds (rows with a usable ``Sr No.`` and title)."""
    skipped: tuple[SkippedRow, ...] = ()
    unrecognised_status: tuple[str, ...] = ()
    """``Sr No.`` values whose status text was not understood."""

    @property
    def changed_anything(self) -> bool:
        return bool(
            self.created
            or self.updated
            or self.hidden
            or self.unhidden
            or self.retired
            or self.removed
        )


class SheetImportService:
    def __init__(
        self,
        *,
        fetch: Callable[[], str],
        task_repo: TaskRepository,
        audit: AuditSink,
        tz: ZoneInfo,
        scope: str = "",
    ) -> None:
        """``scope`` names the sheet being read (``SheetLink.scope``): tasks are
        matched only within it, and tasks from any other sheet are retired. Empty
        means unscoped (refs are plain ``sr:<n>``), which retires nothing."""
        self._fetch = fetch
        self._tasks = task_repo
        self._audit = audit
        self._tz = tz
        self._scope = scope

    def tick(self, *, now: dt.datetime) -> ImportResult:
        # Fetch and parse fully before touching anything: a network failure,
        # a sheet gone private, or a missing column raises here, and the
        # database is untouched.
        parsed = parse_sheet(self._fetch(), today=local_date(now, self._tz))

        existing = {
            task.external_row_ref: task
            for task in self._tasks.list(TaskFilter(include_deleted=True))
            if task.source is TaskSourceKind.SHEET_IMPORT and task.external_row_ref
        }

        counts = {"created": 0, "updated": 0, "hidden": 0, "unhidden": 0, "unchanged": 0}
        unrecognised: list[str] = []

        seen_refs: set[str] = set()
        for row in parsed.rows:
            if row.status is None:
                unrecognised.append(row.sr_no)
            ref = scoped_ref(self._scope, row.external_ref)
            seen_refs.add(ref)
            task = existing.get(ref)
            if task is None:
                self._create(row, ref=ref, now=now)
                counts["created"] += 1
                continue
            outcome = self._update(task, row, now=now)
            counts[outcome] += 1

        return ImportResult(
            **counts,
            retired=self._retire_other_sheets(existing.values(), now=now),
            removed=self._hide_removed_rows(
                existing.values(), seen_refs=seen_refs, skipped=parsed.skipped, now=now
            ),
            rows_read=len(parsed.rows),
            skipped=parsed.skipped,
            unrecognised_status=tuple(unrecognised),
        )

    # -- helpers ------------------------------------------------------------

    def _logged_at(self, row: ImportedRow, *, now: dt.datetime) -> dt.datetime:
        """The row's own Date, as the start of that day -- so a first import
        of months of history does not report every task as "added today".
        Never in the future: a mistyped Date must not produce a task created
        after it was imported."""
        if row.logged_on is None:
            return now
        return min(combine_local(row.logged_on, dt.time(0, 0), self._tz), now)

    def _retire_other_sheets(self, tasks: Iterable[Task], *, now: dt.datetime) -> int:
        """Hide the tasks that came from a different sheet than the one in use,
        so a switch never leaves the old sheet's tasks in the reports. Hidden,
        not deleted, and tagged ``retired:sheet``: reading the old sheet again
        restores them (their refs match again). A task already hidden for another
        reason (delegated) is left exactly as it is."""
        if not self._scope:
            return 0
        retired = 0
        for task in tasks:
            if task.is_deleted or scope_of_ref(task.external_row_ref) == self._scope:
                continue
            self._hide(task, tag=RETIRED_TAG, now=now)
            retired += 1
        return retired

    def _hide_removed_rows(
        self,
        tasks: Iterable[Task],
        *,
        seen_refs: set[str],
        skipped: tuple[SkippedRow, ...],
        now: dt.datetime,
    ) -> int:
        """Hide this sheet's visible tasks whose row is no longer in it. Hidden,
        not deleted, and tagged ``removed:sheet``: the row coming back restores
        the same task. Nothing happens when the sheet yielded no tasks (that is
        a broken read, not a sheet emptied on purpose), and a task whose row is
        only *skipped* this time (duplicate or blank ``Sr No.``) stays."""
        if not seen_refs:
            return 0
        skipped_refs = {
            scoped_ref(self._scope, f"sr:{match.group(1)}")
            for skip in skipped
            if (match := _SKIPPED_SR_NO.fullmatch(skip.detail)) is not None
        }
        removed = 0
        for task in tasks:
            ref = task.external_row_ref
            if (
                task.is_deleted
                or scope_of_ref(ref) != self._scope
                or ref in seen_refs
                or ref in skipped_refs
            ):
                continue
            self._hide(task, tag=REMOVED_TAG, now=now)
            removed += 1
        return removed

    def _hide(self, task: Task, *, tag: str, now: dt.datetime) -> None:
        updated = dataclasses.replace(
            task,
            deleted_at=now,
            updated_at=now,
            version=task.version + 1,
            tags=(*task.tags, tag),
        )
        saved = self._tasks.update(updated, expected_version=task.version)
        changed = diff(task, saved)
        self._audit.record(
            action="TASK_UPDATED",
            entity_type="task",
            entity_id=saved.id,
            actor=str(_ACTOR),
            actor_kind=ActorKind.SYNC.value,
            at=now,
            before={field: change["before"] for field, change in changed.items()},
            after={field: change["after"] for field, change in changed.items()},
        )

    def _create(self, row: ImportedRow, *, ref: str, now: dt.datetime) -> None:
        logged_at = self._logged_at(row, now=now)
        status = row.status or TaskStatus.PENDING
        task = Task(
            id=new_id(),
            display_id=format_display_id(TASK_PREFIX, self._tasks.next_display_sequence()),
            title=row.title,
            status=status,
            priority=Priority.MEDIUM,
            created_at=logged_at,
            updated_at=logged_at,
            version=1,
            # Already closed when first seen: the sheet has no completion
            # date, and "logged on" is the best proxy -- it keeps months of
            # closed history out of "completed today".
            completed_at=logged_at if status is TaskStatus.COMPLETED else None,
            due_date=row.due_date,
            remarks=row.remarks,
            tags=_tags_for(row, current=()),
            source=TaskSourceKind.SHEET_IMPORT,
            external_row_ref=ref,
            deleted_at=now if row.delegated_to else None,
        )
        self._tasks.add(task)
        self._audit.record(
            action="TASK_CREATED",
            entity_type="task",
            entity_id=task.id,
            actor=str(_ACTOR),
            actor_kind=ActorKind.SYNC.value,
            at=now,
            after=task.snapshot_fields(),
        )

    def _update(self, task: Task, row: ImportedRow, *, now: dt.datetime) -> str:
        desired: dict[str, Any] = {
            "title": row.title,
            "remarks": row.remarks,
            "due_date": row.due_date,
            "tags": _tags_for(row, current=task.tags),
        }
        if row.status is not None:
            desired["status"] = row.status
        changes = {name: value for name, value in desired.items() if getattr(task, name) != value}

        hide = row.delegated_to is not None
        visibility_changes = hide != task.is_deleted

        if not changes and not visibility_changes:
            return "unchanged"

        # apply_edit owns completed_at (stamped on -> COMPLETED, cleared on
        # reopen) and the version bump; deleted_at is not an editable field,
        # so visibility is applied separately, bumping the version itself
        # only when apply_edit did not already.
        updated = apply_edit(task, changes, now=now) if changes else task
        if visibility_changes:
            updated = dataclasses.replace(
                updated,
                deleted_at=now if hide else None,
                updated_at=now,
                version=updated.version if changes else updated.version + 1,
            )

        saved = self._tasks.update(updated, expected_version=task.version)
        changed = diff(task, saved)
        self._audit.record(
            action="TASK_UPDATED",
            entity_type="task",
            entity_id=saved.id,
            actor=str(_ACTOR),
            actor_kind=ActorKind.SYNC.value,
            at=now,
            before={field: change["before"] for field, change in changed.items()},
            after={field: change["after"] for field, change in changed.items()},
        )

        if visibility_changes:
            return "hidden" if hide else "unhidden"
        return "updated"


def _tags_for(row: ImportedRow, *, current: tuple[str, ...]) -> tuple[str, ...]:
    """Keep any tag the sheet does not own; replace only the delegation tag, and
    drop the retired marker (a row being read again means its sheet is back)."""
    kept = tuple(
        tag
        for tag in current
        if not tag.startswith(_DELEGATED_TAG) and tag not in (RETIRED_TAG, REMOVED_TAG)
    )
    if row.delegated_to is None:
        return kept
    return (*kept, f"{_DELEGATED_TAG}{row.delegated_to.casefold()}"[:_MAX_TAG_LENGTH])
