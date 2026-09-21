"""TaskService orchestration against real repository and audit implementations.

The repository's concurrency guarantee and the audit chain's integrity are
already proven at their own layers (test_task_repository.py,
test_audit_sink.py); these tests focus on what the service itself adds:
minting ids, deciding when to audit, and isolating bulk-update failures.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import text
from sqlalchemy.orm import Session

from interlock.adapters.persistence.audit_sink import PostgresAuditSink
from interlock.adapters.persistence.task_repository import PostgresTaskRepository
from interlock.domain.common.actor import Actor, ActorKind
from interlock.domain.common.clock import FrozenClock
from interlock.domain.tasks.entities import Priority, TaskStatus
from interlock.services.task_service import BulkUpdateItem, TaskService

NOW = dt.datetime(2026, 9, 15, 17, 4, tzinfo=dt.UTC)
KAIF = Actor(kind=ActorKind.USER, id="u-1", label="kaif")


def make_service(db_session: Session, *, at: dt.datetime = NOW) -> TaskService:
    return TaskService(
        repo=PostgresTaskRepository(db_session),
        audit=PostgresAuditSink(db_session),
        clock=FrozenClock(at),
    )


class TestCreate:
    def test_create_mints_a_sequential_display_id(self, db_session: Session) -> None:
        service = make_service(db_session)
        a = service.create(title="First task", actor=KAIF)
        b = service.create(title="Second task", actor=KAIF)
        assert a.display_id != b.display_id
        assert a.display_id.startswith("TSK-")

    def test_create_writes_an_audit_entry(self, db_session: Session) -> None:
        service = make_service(db_session)
        task = service.create(title="Deploy production server", actor=KAIF)

        count = db_session.execute(
            text(
                "SELECT count(*) FROM audit_logs WHERE entity_id = :id AND action = 'TASK_CREATED'"
            ),
            {"id": task.id},
        ).scalar_one()
        assert count == 1

    def test_created_task_is_immediately_readable(self, db_session: Session) -> None:
        service = make_service(db_session)
        task = service.create(title="Deploy production server", actor=KAIF)
        assert service.get(task.id) is not None
        assert service.get_by_display_id(task.display_id) is not None


class TestUpdate:
    def test_update_bumps_version_and_audits_only_the_diff(self, db_session: Session) -> None:
        service = make_service(db_session)
        task = service.create(
            title="Deploy production server", actor=KAIF, status=TaskStatus.PENDING
        )

        updated = service.update(
            task.id,
            {"status": TaskStatus.COMPLETED},
            expected_version=1,
            actor=KAIF,
        )
        assert updated.version == 2
        assert updated.status is TaskStatus.COMPLETED

        row = db_session.execute(
            text(
                "SELECT before, after FROM audit_logs "
                "WHERE entity_id = :id AND action = 'TASK_UPDATED'"
            ),
            {"id": task.id},
        ).one()
        # Completing a task also stamps completed_at, so the diff legitimately
        # covers both fields -- not just the one the caller explicitly set.
        assert row.before == {"status": "PENDING", "completed_at": None}
        assert row.after["status"] == "COMPLETED"
        assert row.after["completed_at"] is not None

    def test_no_op_edit_does_not_write_history_or_audit(self, db_session: Session) -> None:
        service = make_service(db_session)
        task = service.create(title="Deploy production server", actor=KAIF)

        result = service.update(task.id, {}, expected_version=1, actor=KAIF)
        assert result.version == 1  # unchanged

        count = db_session.execute(
            text(
                "SELECT count(*) FROM audit_logs WHERE entity_id = :id AND action = 'TASK_UPDATED'"
            ),
            {"id": task.id},
        ).scalar_one()
        assert count == 0

    def test_mark_completed_stamps_completion_time(self, db_session: Session) -> None:
        """Scenario 2's quick action, via the service layer."""
        service = make_service(db_session)
        task = service.create(title="Deploy customer server", actor=KAIF, status=TaskStatus.PENDING)

        updated = service.update(
            task.id, {"status": TaskStatus.COMPLETED}, expected_version=1, actor=KAIF
        )
        assert updated.completed_at == NOW


class TestBulkUpdate:
    def test_one_stale_item_does_not_affect_the_others(self, db_session: Session) -> None:
        service = make_service(db_session)
        a = service.create(title="Task A", actor=KAIF)
        b = service.create(title="Task B", actor=KAIF)

        # Make b's stored version 2 by editing it first, so the bulk request
        # below submits a stale expected_version=1 for it.
        service.update(b.id, {"remarks": "already edited"}, expected_version=1, actor=KAIF)

        results = service.bulk_update(
            [
                BulkUpdateItem(
                    task_id=a.id, changes={"status": TaskStatus.COMPLETED}, expected_version=1
                ),
                BulkUpdateItem(
                    task_id=b.id, changes={"status": TaskStatus.COMPLETED}, expected_version=1
                ),
            ],
            actor=KAIF,
        )

        by_id = {r.task_id: r for r in results}
        assert by_id[a.id].ok
        assert not by_id[b.id].ok
        assert by_id[b.id].error_code == "TASK_VERSION_CONFLICT"

        # a's success must stand regardless of b's failure.
        assert service.get(a.id).status is TaskStatus.COMPLETED  # type: ignore[union-attr]

    def test_nonexistent_task_in_a_batch_is_isolated(self, db_session: Session) -> None:
        service = make_service(db_session)
        a = service.create(title="Real task", actor=KAIF)

        results = service.bulk_update(
            [
                BulkUpdateItem(
                    task_id=a.id, changes={"priority": Priority.HIGH}, expected_version=1
                ),
                BulkUpdateItem(
                    task_id="does-not-exist",
                    changes={"priority": Priority.HIGH},
                    expected_version=1,
                ),
            ],
            actor=KAIF,
        )
        by_id = {r.task_id: r for r in results}
        assert by_id[a.id].ok
        assert not by_id["does-not-exist"].ok
        assert by_id["does-not-exist"].error_code == "NOT_FOUND"
