"""SheetSyncService against a real database and FakeSpreadsheetProvider.

Same "call the service directly, no network" shape as
``test_scheduler_service.py`` and its ``MockWhatsAppProvider``. Assertions on
the *sheet* side always go through ``FakeSpreadsheetProvider.read_all_rows()``
-- never through whatever a test happened to pass to ``write_rows`` -- so a
test proves what the service actually wrote, not what it meant to.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from interlock.adapters.persistence.audit_sink import PostgresAuditSink
from interlock.adapters.persistence.models import AuditLogRow, TaskSheetSyncRow
from interlock.adapters.persistence.sync_repository import SyncRepository
from interlock.adapters.persistence.task_repository import PostgresTaskRepository
from interlock.adapters.sheets.fake import FakeSpreadsheetProvider
from interlock.domain.common.actor import ActorKind
from interlock.domain.common.errors import ValidationFailedError
from interlock.domain.sync.reconciliation import ConflictResolution
from interlock.domain.tasks.entities import Priority, Task, TaskSourceKind, apply_edit
from interlock.services.sheet_sync_service import SheetSyncService, SyncTickResult
from tests.conftest import BRIEF_EVENING, make_task

T0 = BRIEF_EVENING


def build(session: Session, sheets: FakeSpreadsheetProvider) -> SheetSyncService:
    return SheetSyncService(
        sheets=sheets,
        sync_repo=SyncRepository(session),
        task_repo=PostgresTaskRepository(session),
        audit=PostgresAuditSink(session),
    )


def make_saved_task(session: Session, **overrides: object) -> Task:
    return PostgresTaskRepository(session).add(make_task(**overrides))  # type: ignore[arg-type]


def audit_actions(session: Session, entity_id: str) -> list[tuple[str, str]]:
    rows = (
        session.execute(
            select(AuditLogRow).where(AuditLogRow.entity_id == entity_id).order_by(AuditLogRow.seq)
        )
        .scalars()
        .all()
    )
    return [(row.action, row.actor_kind) for row in rows]


@pytest.fixture
def sheets() -> FakeSpreadsheetProvider:
    return FakeSpreadsheetProvider()


class TestNewAppTask:
    def test_is_pushed_and_a_second_tick_is_a_noop(
        self, db_session: Session, sheets: FakeSpreadsheetProvider
    ) -> None:
        task = make_saved_task(db_session)
        service = build(db_session, sheets)

        result = service.tick(now=T0)

        assert result.pushed == 1
        rows = sheets.read_all_rows()
        assert len(rows) == 1
        assert rows[0].sys_task_id == task.id

        again = service.tick(now=T0 + dt.timedelta(minutes=1))
        assert again == SyncTickResult()


class TestUntrackedRow:
    def test_creates_a_task(self, db_session: Session, sheets: FakeSpreadsheetProvider) -> None:
        sheets.seed_row(
            sys_task_id=None,
            sys_version=None,
            sys_content_hash=None,
            fields={"title": "From sheet", "priority": "MEDIUM", "status": "PENDING"},
        )
        service = build(db_session, sheets)

        result = service.tick(now=T0)

        assert result.created == 1
        tasks = PostgresTaskRepository(db_session).list()
        assert len(tasks) == 1
        task = tasks[0]
        assert task.title == "From sheet"
        assert task.source is TaskSourceKind.GOOGLE_SHEETS
        assert task.display_id.startswith("TSK-")
        assert audit_actions(db_session, task.id) == [("TASK_CREATED", ActorKind.SYNC.value)]

    def test_a_blank_title_cell_falls_back_to_a_placeholder(
        self, db_session: Session, sheets: FakeSpreadsheetProvider
    ) -> None:
        sheets.seed_row(sys_task_id=None, sys_version=None, sys_content_hash=None, fields={})
        service = build(db_session, sheets)

        service.tick(now=T0)

        task = PostgresTaskRepository(db_session).list()[0]
        assert task.title == "Untitled (from Google Sheets)"


class TestSheetOnlyEdit:
    def test_pulls_into_the_task(
        self, db_session: Session, sheets: FakeSpreadsheetProvider
    ) -> None:
        task = make_saved_task(db_session, title="Original title")
        service = build(db_session, sheets)
        service.tick(now=T0)
        row_index = sheets.read_all_rows()[0].row_index

        sheets.mutate_row(row_index, title="Edited in sheet")
        result = service.tick(now=T0 + dt.timedelta(minutes=1))

        assert result.pulled == 1
        updated = PostgresTaskRepository(db_session).get(task.id)
        assert updated is not None
        assert updated.title == "Edited in sheet"
        assert updated.version == 2
        assert audit_actions(db_session, task.id)[-1] == ("TASK_UPDATED", ActorKind.SYNC.value)

        noop = service.tick(now=T0 + dt.timedelta(minutes=2))
        assert noop == SyncTickResult()

    def test_an_unparseable_cell_is_dropped_but_the_rest_of_the_row_still_applies(
        self, db_session: Session, sheets: FakeSpreadsheetProvider
    ) -> None:
        make_saved_task(db_session, title="Original", priority=Priority.MEDIUM)
        service = build(db_session, sheets)
        service.tick(now=T0)
        row_index = sheets.read_all_rows()[0].row_index

        sheets.mutate_row(row_index, title="New title", priority="BANANA")
        result = service.tick(now=T0 + dt.timedelta(minutes=1))

        assert result.pulled == 1
        updated = PostgresTaskRepository(db_session).list()[0]
        assert updated.title == "New title"
        assert updated.priority is Priority.MEDIUM

    def test_blank_due_date_clears_but_blank_title_does_not_apply(
        self, db_session: Session, sheets: FakeSpreadsheetProvider
    ) -> None:
        make_saved_task(db_session, title="Keep this title", due_date=dt.date(2026, 9, 20))
        service = build(db_session, sheets)
        service.tick(now=T0)
        row_index = sheets.read_all_rows()[0].row_index

        sheets.mutate_row(row_index, due_date="", title="")
        result = service.tick(now=T0 + dt.timedelta(minutes=1))

        assert result.pulled == 1
        updated = PostgresTaskRepository(db_session).list()[0]
        assert updated.due_date is None
        assert updated.title == "Keep this title"


    def test_a_mistyped_due_date_is_ignored_not_treated_as_a_clear(
        self, db_session: Session, sheets: FakeSpreadsheetProvider
    ) -> None:
        """A blank cell clears a nullable field; a cell that merely fails to
        parse must not -- otherwise a human typing "20/9/2026" into the
        sheet silently erases the task's due date."""
        make_saved_task(db_session, due_date=dt.date(2026, 9, 20))
        service = build(db_session, sheets)
        service.tick(now=T0)
        row_index = sheets.read_all_rows()[0].row_index

        sheets.mutate_row(row_index, due_date="20/9/2026")
        service.tick(now=T0 + dt.timedelta(minutes=1))

        updated = PostgresTaskRepository(db_session).list()[0]
        assert updated.due_date == dt.date(2026, 9, 20)
        # The sheet is re-stamped with the real value, so the bad cell heals.
        assert sheets.read_all_rows()[0].fields["due_date"] == "2026-09-20"


class TestDbOnlyEdit:
    def test_pushes_at_the_same_row_index(
        self, db_session: Session, sheets: FakeSpreadsheetProvider
    ) -> None:
        task = make_saved_task(db_session)
        service = build(db_session, sheets)
        service.tick(now=T0)
        row_index = sheets.read_all_rows()[0].row_index

        task_repo = PostgresTaskRepository(db_session)
        edited = apply_edit(task, {"title": "Edited in app"}, now=T0 + dt.timedelta(minutes=1))
        task_repo.update(edited, expected_version=task.version)

        result = service.tick(now=T0 + dt.timedelta(minutes=2))

        assert result.pushed == 1
        rows = sheets.read_all_rows()
        assert len(rows) == 1
        assert rows[0].row_index == row_index
        assert rows[0].fields["title"] == "Edited in app"


class TestBothSidesChanged:
    def test_parks_a_conflict_and_touches_neither_side(
        self, db_session: Session, sheets: FakeSpreadsheetProvider
    ) -> None:
        task = make_saved_task(db_session)
        service = build(db_session, sheets)
        service.tick(now=T0)
        row_index = sheets.read_all_rows()[0].row_index

        sheets.mutate_row(row_index, title="Edited in sheet")
        task_repo = PostgresTaskRepository(db_session)
        edited = apply_edit(task, {"title": "Edited in app"}, now=T0 + dt.timedelta(minutes=1))
        task_repo.update(edited, expected_version=task.version)

        result = service.tick(now=T0 + dt.timedelta(minutes=2))

        assert result.conflicts == 1
        assert SyncRepository(db_session).has_open_conflict(task.id) is True
        current = task_repo.get(task.id)
        assert current is not None
        assert current.title == "Edited in app"
        assert sheets.read_all_rows()[0].fields["title"] == "Edited in sheet"

    def test_a_repeat_tick_does_not_duplicate_the_conflict(
        self, db_session: Session, sheets: FakeSpreadsheetProvider
    ) -> None:
        task = make_saved_task(db_session)
        service = build(db_session, sheets)
        service.tick(now=T0)
        row_index = sheets.read_all_rows()[0].row_index
        sheets.mutate_row(row_index, title="Edited in sheet")
        task_repo = PostgresTaskRepository(db_session)
        edited = apply_edit(task, {"title": "Edited in app"}, now=T0 + dt.timedelta(minutes=1))
        task_repo.update(edited, expected_version=task.version)
        service.tick(now=T0 + dt.timedelta(minutes=2))

        again = service.tick(now=T0 + dt.timedelta(minutes=3))

        assert again.conflicts == 0
        assert len(SyncRepository(db_session).list_conflicts()) == 1


class TestVanishedRow:
    def test_parks_a_conflict_with_no_external_value(
        self, db_session: Session, sheets: FakeSpreadsheetProvider
    ) -> None:
        task = make_saved_task(db_session)
        service = build(db_session, sheets)
        service.tick(now=T0)
        row_index = sheets.read_all_rows()[0].row_index
        sheets.delete_row(row_index)

        result = service.tick(now=T0 + dt.timedelta(minutes=1))

        assert result.vanished == 1
        conflict = SyncRepository(db_session).list_conflicts()[0]
        assert conflict.task_id == task.id
        assert conflict.external_value is None


class TestSoftDeletedTask:
    def test_is_not_listed_and_its_row_is_left_alone(
        self, db_session: Session, sheets: FakeSpreadsheetProvider
    ) -> None:
        """Documents a known, deliberate gap: nothing pushes or removes a
        deleted task's sheet row (R5 forbids inferring deletion either way).
        See HANDOVER.md."""
        make_saved_task(db_session, deleted_at=T0)
        service = build(db_session, sheets)

        result = service.tick(now=T0 + dt.timedelta(minutes=1))

        assert result == SyncTickResult()
        assert sheets.read_all_rows() == []


class TestResolveConflict:
    def _park_a_conflict(
        self, db_session: Session, sheets: FakeSpreadsheetProvider, service: SheetSyncService
    ) -> tuple[Task, str]:
        task = make_saved_task(db_session, title="DB value")
        service.tick(now=T0)
        row_index = sheets.read_all_rows()[0].row_index
        sheets.mutate_row(row_index, title="Sheet value")
        task_repo = PostgresTaskRepository(db_session)
        edited = apply_edit(
            task, {"title": "DB value edited"}, now=T0 + dt.timedelta(minutes=1)
        )
        task_repo.update(edited, expected_version=task.version)
        service.tick(now=T0 + dt.timedelta(minutes=2))
        conflict = SyncRepository(db_session).list_conflicts()[0]
        return task, conflict.id

    def test_kept_db_pushes_the_db_value_and_closes_it(
        self, db_session: Session, sheets: FakeSpreadsheetProvider
    ) -> None:
        service = build(db_session, sheets)
        task, conflict_id = self._park_a_conflict(db_session, sheets, service)

        resolved = service.resolve_conflict(
            conflict_id,
            resolution=ConflictResolution.KEPT_DB,
            resolved_by="kaif",
            now=T0 + dt.timedelta(minutes=3),
        )

        assert resolved.is_open is False
        assert sheets.read_all_rows()[0].fields["title"] == "DB value edited"
        task_repo = PostgresTaskRepository(db_session)
        current = task_repo.get(task.id)
        assert current is not None
        assert current.title == "DB value edited"

        noop = service.tick(now=T0 + dt.timedelta(minutes=4))
        assert noop == SyncTickResult()

    def test_kept_sheet_pulls_the_sheet_value_and_closes_it(
        self, db_session: Session, sheets: FakeSpreadsheetProvider
    ) -> None:
        service = build(db_session, sheets)
        task, conflict_id = self._park_a_conflict(db_session, sheets, service)

        resolved = service.resolve_conflict(
            conflict_id,
            resolution=ConflictResolution.KEPT_SHEET,
            resolved_by="kaif",
            now=T0 + dt.timedelta(minutes=3),
        )

        assert resolved.is_open is False
        task_repo = PostgresTaskRepository(db_session)
        current = task_repo.get(task.id)
        assert current is not None
        assert current.title == "Sheet value"

        noop = service.tick(now=T0 + dt.timedelta(minutes=4))
        assert noop == SyncTickResult()

    def test_kept_sheet_on_a_vanished_row_raises(
        self, db_session: Session, sheets: FakeSpreadsheetProvider
    ) -> None:
        service = build(db_session, sheets)
        make_saved_task(db_session)
        service.tick(now=T0)
        row_index = sheets.read_all_rows()[0].row_index
        sheets.delete_row(row_index)
        service.tick(now=T0 + dt.timedelta(minutes=1))
        conflict = SyncRepository(db_session).list_conflicts()[0]

        with pytest.raises(ValidationFailedError):
            service.resolve_conflict(
                conflict.id,
                resolution=ConflictResolution.KEPT_SHEET,
                resolved_by="kaif",
                now=T0 + dt.timedelta(minutes=2),
            )


class TestMissingCursorFallsBackToTheSysStamp:
    """A restored database, or the first real tick after switching
    ``SHEETS_PROVIDER=mock`` to ``google``: the cursor row is gone, but a
    row already carrying a ``_sys`` stamp has genuinely been synced before.
    See ``domain/sync/reconciliation.py::resolve_baseline``."""

    def _drop_cursor(self, db_session: Session, task_id: str) -> None:
        db_session.execute(delete(TaskSheetSyncRow).where(TaskSheetSyncRow.task_id == task_id))
        db_session.flush()

    def test_nothing_changed_is_a_noop_not_a_conflict(
        self, db_session: Session, sheets: FakeSpreadsheetProvider
    ) -> None:
        task = make_saved_task(db_session)
        service = build(db_session, sheets)
        service.tick(now=T0)
        self._drop_cursor(db_session, task.id)

        result = service.tick(now=T0 + dt.timedelta(minutes=1))

        assert result == SyncTickResult()
        assert SyncRepository(db_session).has_open_conflict(task.id) is False

    def test_a_sheet_edit_since_pulls(
        self, db_session: Session, sheets: FakeSpreadsheetProvider
    ) -> None:
        task = make_saved_task(db_session)
        service = build(db_session, sheets)
        service.tick(now=T0)
        row_index = sheets.read_all_rows()[0].row_index
        self._drop_cursor(db_session, task.id)

        sheets.mutate_row(row_index, title="Edited after restore")
        result = service.tick(now=T0 + dt.timedelta(minutes=1))

        assert result.pulled == 1
        updated = PostgresTaskRepository(db_session).get(task.id)
        assert updated is not None
        assert updated.title == "Edited after restore"

    def test_a_task_edit_since_pushes(
        self, db_session: Session, sheets: FakeSpreadsheetProvider
    ) -> None:
        task = make_saved_task(db_session)
        service = build(db_session, sheets)
        service.tick(now=T0)
        self._drop_cursor(db_session, task.id)

        task_repo = PostgresTaskRepository(db_session)
        edited = apply_edit(
            task, {"title": "Edited in app after restore"}, now=T0 + dt.timedelta(minutes=1)
        )
        task_repo.update(edited, expected_version=task.version)

        result = service.tick(now=T0 + dt.timedelta(minutes=2))

        assert result.pushed == 1
        assert sheets.read_all_rows()[0].fields["title"] == "Edited in app after restore"
