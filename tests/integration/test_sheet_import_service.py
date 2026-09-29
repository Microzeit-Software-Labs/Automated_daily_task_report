"""SheetImportService against a real database, with the sheet's CSV supplied
by a plain function -- no network. The real fetch is covered by
tests/unit/test_csv_source.py and was exercised live against the real sheet
via scripts/sheet_import_preview.py."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from interlock.adapters.persistence.audit_sink import PostgresAuditSink
from interlock.adapters.persistence.models import AuditLogRow
from interlock.adapters.persistence.task_repository import PostgresTaskRepository
from interlock.adapters.sheets.csv_source import SheetNotReadableError
from interlock.domain.common.actor import Actor, ActorKind
from interlock.domain.common.clock import FrozenClock, combine_local
from interlock.domain.common.errors import ValidationFailedError
from interlock.domain.ports.repositories import TaskFilter
from interlock.domain.tasks.entities import Task, TaskSourceKind, TaskStatus
from interlock.domain.tasks.summary import summarize
from interlock.services.sheet_import_service import ImportResult, SheetImportService
from interlock.services.task_service import TaskService
from tests.conftest import make_task

TZ = ZoneInfo("Asia/Kolkata")
T0 = dt.datetime(2026, 9, 29, 12, 0, tzinfo=dt.UTC)  # 17:30 IST
T1 = T0 + dt.timedelta(minutes=1)
HEADER = "Sr No.,Date,Task,Status ,Note,Deadline \n"
KAIF = Actor(kind=ActorKind.USER, id="u-1", label="kaif")


def tick(session: Session, csv_body: str, *, now: dt.datetime = T0) -> ImportResult:
    return SheetImportService(
        fetch=lambda: HEADER + csv_body,
        task_repo=PostgresTaskRepository(session),
        audit=PostgresAuditSink(session),
        tz=TZ,
    ).tick(now=now)


def imported(session: Session) -> dict[str, Task]:
    return {
        task.external_row_ref: task
        for task in PostgresTaskRepository(session).list(TaskFilter(include_deleted=True))
        if task.source is TaskSourceKind.SHEET_IMPORT and task.external_row_ref
    }


def audit_actions(session: Session, entity_id: str) -> list[tuple[str, str]]:
    rows = session.execute(
        select(AuditLogRow).where(AuditLogRow.entity_id == entity_id).order_by(AuditLogRow.seq)
    ).scalars()
    return [(row.action, row.actor_kind) for row in rows]


class TestFirstImport:
    def test_creates_one_task_per_keyed_row(self, db_session: Session) -> None:
        result = tick(db_session, "1,29-04-2026,ICCC Integration,Closed,done,\n"
                                  "2,09-28-2026,Health check up,Inprogress,,10-05-2026\n")

        assert result.created == 2
        tasks = imported(db_session)
        assert tasks["sr:2"].title == "Health check up"
        assert tasks["sr:2"].status is TaskStatus.IN_PROGRESS
        assert tasks["sr:2"].due_date == dt.date(2026, 10, 5)
        assert tasks["sr:1"].remarks == "done"
        assert audit_actions(db_session, tasks["sr:1"].id) == [("TASK_CREATED", "SYNC")]

    def test_history_keeps_its_own_dates_so_it_does_not_flood_todays_report(
        self, db_session: Session
    ) -> None:
        tick(db_session, "1,29-04-2026,ICCC Integration,Closed,,\n")

        task = imported(db_session)["sr:1"]
        logged = combine_local(dt.date(2026, 4, 29), dt.time(0, 0), TZ)
        assert task.created_at == logged
        assert task.updated_at == logged
        assert task.completed_at == logged

        summary = summarize([task], today=dt.date(2026, 9, 29), tz=TZ)
        assert summary.newly_added == 0
        assert summary.modified_today == 0
        assert summary.completed_today == 0

    def test_a_future_date_typo_never_produces_a_task_created_in_the_future(
        self, db_session: Session
    ) -> None:
        tick(db_session, "1,12-31-2026,Typo in the date,Inprogress,,\n")
        assert imported(db_session)["sr:1"].created_at == T0

    def test_a_row_without_a_date_uses_the_import_time(self, db_session: Session) -> None:
        tick(db_session, "1,,No date,Inprogress,,\n")
        assert imported(db_session)["sr:1"].created_at == T0


class TestLaterTicks:
    def test_an_identical_sheet_changes_nothing(self, db_session: Session) -> None:
        body = "1,29-04-2026,ICCC Integration,Closed,,\n2,09-28-2026,Health,Inprogress,,\n"
        tick(db_session, body)
        before = {ref: task.version for ref, task in imported(db_session).items()}

        result = tick(db_session, body, now=T1)

        assert result.unchanged == 2
        assert not result.changed_anything
        assert {ref: task.version for ref, task in imported(db_session).items()} == before

    def test_an_edited_row_is_applied_and_audited(self, db_session: Session) -> None:
        tick(db_session, "2,09-28-2026,Health check,Inprogress,,\n")
        result = tick(db_session, "2,09-28-2026,Health check up,Inprogress,server ok,\n", now=T1)

        assert result.updated == 1
        task = imported(db_session)["sr:2"]
        assert task.title == "Health check up"
        assert task.remarks == "server ok"
        assert task.version == 2
        assert task.updated_at == T1
        assert audit_actions(db_session, task.id)[-1] == ("TASK_UPDATED", "SYNC")

    def test_closing_a_task_in_the_sheet_stamps_completed_at_now(
        self, db_session: Session
    ) -> None:
        tick(db_session, "2,09-28-2026,Health check up,Inprogress,,\n")
        tick(db_session, "2,09-28-2026,Health check up,Closed,,\n", now=T1)

        task = imported(db_session)["sr:2"]
        assert task.status is TaskStatus.COMPLETED
        assert task.completed_at == T1
        assert summarize([task], today=dt.date(2026, 9, 29), tz=TZ).completed_today == 1

    def test_a_row_that_vanishes_leaves_its_task_untouched(self, db_session: Session) -> None:
        tick(db_session, "1,29-04-2026,Keep,Closed,,\n2,09-28-2026,Also keep,Inprogress,,\n")
        tick(db_session, "1,29-04-2026,Keep,Closed,,\n", now=T1)

        task = imported(db_session)["sr:2"]
        assert not task.is_deleted
        assert task.version == 1

    def test_tasks_created_in_the_app_are_never_touched(self, db_session: Session) -> None:
        repo = PostgresTaskRepository(db_session)
        own = repo.add(make_task(title="Made in Interlock"))

        tick(db_session, "1,29-04-2026,From the sheet,Closed,,\n")

        still = repo.get(own.id)
        assert still is not None
        assert still.version == 1
        assert still.source is TaskSourceKind.APP


class TestDelegatedRows:
    def test_are_hidden_from_reports_from_the_start(self, db_session: Session) -> None:
        tick(db_session, "24,05-25-2026,Sri Lanka Issue,Subhan's Task,,\n")

        task = imported(db_session)["sr:24"]
        assert task.is_deleted
        assert "delegated:subhan" in task.tags
        assert all(t.id != task.id for t in PostgresTaskRepository(db_session).list())

    def test_a_task_handed_over_later_is_hidden(self, db_session: Session) -> None:
        tick(db_session, "24,05-25-2026,Sri Lanka Issue,Inprogress,,\n")
        result = tick(db_session, "24,05-25-2026,Sri Lanka Issue,Subhan's Task,,\n", now=T1)

        assert result.hidden == 1
        task = imported(db_session)["sr:24"]
        assert task.is_deleted
        assert task.version == 2

    def test_reappears_when_the_status_changes_back(self, db_session: Session) -> None:
        tick(db_session, "24,05-25-2026,Sri Lanka Issue,Subhan's Task,,\n")
        result = tick(db_session, "24,05-25-2026,Sri Lanka Issue,Inprogress,,\n", now=T1)

        assert result.unhidden == 1
        task = imported(db_session)["sr:24"]
        assert not task.is_deleted
        assert not any(tag.startswith("delegated:") for tag in task.tags)


class TestNeverGuess:
    def test_skipped_rows_are_reported_not_imported(self, db_session: Session) -> None:
        result = tick(db_session, ",05-11-2026,Microplus update,Closed,,\n")
        assert result.created == 0
        assert [s.detail for s in result.skipped] == ["Microplus update"]

    def test_an_unknown_status_on_a_new_row_imports_as_pending_and_is_reported(
        self, db_session: Session
    ) -> None:
        result = tick(db_session, "7,09-28-2026,Vendor call,waiting on vendor,,\n")
        assert result.unrecognised_status == ("7",)
        assert imported(db_session)["sr:7"].status is TaskStatus.PENDING

    def test_an_unknown_status_on_an_existing_task_keeps_its_status(
        self, db_session: Session
    ) -> None:
        tick(db_session, "7,09-28-2026,Vendor call,Inprogress,,\n")
        tick(db_session, "7,09-28-2026,Vendor call,waiting on vendor,,\n", now=T1)
        assert imported(db_session)["sr:7"].status is TaskStatus.IN_PROGRESS

    def test_a_failed_fetch_changes_nothing(self, db_session: Session) -> None:
        def unreadable() -> str:
            raise SheetNotReadableError("gone private")

        service = SheetImportService(
            fetch=unreadable,
            task_repo=PostgresTaskRepository(db_session),
            audit=PostgresAuditSink(db_session),
            tz=TZ,
        )
        with pytest.raises(SheetNotReadableError):
            service.tick(now=T0)
        assert imported(db_session) == {}


class TestImportedTasksAreReadOnlyInTheApp:
    def test_editing_an_imported_task_is_refused(self, db_session: Session) -> None:
        tick(db_session, "2,09-28-2026,Health check up,Inprogress,,\n")
        task = imported(db_session)["sr:2"]
        service = TaskService(
            PostgresTaskRepository(db_session), PostgresAuditSink(db_session), FrozenClock(T1)
        )

        with pytest.raises(ValidationFailedError, match="edit it there"):
            service.update(task.id, {"title": "changed"}, expected_version=1, actor=KAIF)

    def test_app_tasks_are_still_editable(self, db_session: Session) -> None:
        repo = PostgresTaskRepository(db_session)
        own = repo.add(make_task(title="Made in Interlock"))
        service = TaskService(repo, PostgresAuditSink(db_session), FrozenClock(T1))

        saved = service.update(own.id, {"title": "Renamed"}, expected_version=1, actor=KAIF)
        assert saved.title == "Renamed"
