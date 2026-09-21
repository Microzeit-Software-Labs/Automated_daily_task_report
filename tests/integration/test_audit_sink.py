"""The audit sink against a real database.

Three properties are load-bearing enough to prove here rather than trust:

1. An audit entry lands in the same transaction as the change it describes --
   roll back the transaction and *both* disappear, never just one.
2. Tampering with a written entry is detected by re-verifying the chain.
3. Concurrent writers cannot fork the chain -- proven with real threads and
   real commits, since the advisory lock this depends on only means anything
   across genuinely separate transactions.
"""

from __future__ import annotations

import datetime as dt
import threading
from collections.abc import Iterator

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from interlock.adapters.persistence.audit_sink import (
    PostgresAuditSink,
    load_and_verify_chain,
)
from interlock.adapters.persistence.models import TaskRow
from interlock.domain.common.ids import new_id

NOW = dt.datetime(2026, 9, 15, 17, 4, tzinfo=dt.UTC)


class TestRecording:
    def test_record_returns_the_entry_hash(self, db_session: Session) -> None:
        sink = PostgresAuditSink(db_session)
        entry_hash = sink.record(
            action="TASK_CREATED",
            entity_type="task",
            entity_id="TSK-00104",
            actor="kaif",
            actor_kind="USER",
            at=NOW,
            after={"status": "PENDING"},
        )
        assert len(entry_hash) == 64  # sha256 hex digest

    def test_sequential_records_form_a_valid_chain(self, db_session: Session) -> None:
        sink = PostgresAuditSink(db_session)
        for i in range(5):
            sink.record(
                action="TASK_UPDATED",
                entity_type="task",
                entity_id=f"TSK-{i:05d}",
                actor="kaif",
                actor_kind="USER",
                at=NOW,
                after={"n": i},
            )

        result = load_and_verify_chain(db_session)
        assert result.intact
        assert result.entries_checked >= 5


class TestUnitOfWorkAtomicity:
    """"Written in the same transaction as the change it describes" is a
    claim the architecture makes throughout -- this proves it rather than
    assuming the surrounding code will always remember to do it right."""

    def test_audit_entry_and_domain_change_commit_together(
        self, migrated_engine: Engine
    ) -> None:
        """Deliberately does NOT use the shared db_session/db_connection
        fixtures: those assume they alone control commit/rollback so they can
        isolate each test by rolling back at teardown. This test's whole point
        is to call session.commit() for real, which would defeat that
        assumption and leak permanent rows -- exactly the bug this project
        hit once already (see the truncate step added to migrated_engine
        after this was found). It gets its own connection and cleans up
        explicitly instead.
        """
        connection = migrated_engine.connect()
        task_id = new_id()
        try:
            factory = sessionmaker(bind=connection, future=True)
            session = factory()

            session.add(
                TaskRow(
                    id=task_id,
                    display_id=f"TSK-{new_id()[:6]}",
                    title="Deploy production server",
                    description="",
                    priority="MEDIUM",
                    status="PENDING",
                    created_at=NOW,
                    updated_at=NOW,
                    remarks="",
                    tags=[],
                    source="APP",
                    version=1,
                )
            )
            PostgresAuditSink(session).record(
                action="TASK_CREATED",
                entity_type="task",
                entity_id=task_id,
                actor="kaif",
                actor_kind="USER",
                at=NOW,
                after={"status": "PENDING"},
            )
            session.commit()

            verify = factory()
            assert verify.get(TaskRow, task_id) is not None
            audit_count = verify.execute(
                text("SELECT count(*) FROM audit_logs WHERE entity_id = :id"),
                {"id": task_id},
            ).scalar_one()
            assert audit_count == 1
        finally:
            # Real commit, real cleanup: delete both rows this test created.
            connection.execute(
                text("DELETE FROM audit_logs WHERE entity_id = :id"), {"id": task_id}
            )
            connection.execute(text("DELETE FROM tasks WHERE id = :id"), {"id": task_id})
            connection.commit()
            connection.close()

    def test_a_failure_after_both_writes_rolls_back_both(
        self, db_connection: object
    ) -> None:
        """The failure mode this guarantee exists to prevent: a task change
        that is saved while its audit trail silently is not, or vice versa."""
        factory = sessionmaker(bind=db_connection, future=True)  # type: ignore[arg-type]
        session = factory()
        task_id = new_id()

        session.add(
            TaskRow(
                id=task_id,
                display_id=f"TSK-{new_id()[:6]}",
                title="Deploy production server",
                description="",
                priority="MEDIUM",
                status="PENDING",
                created_at=NOW,
                updated_at=NOW,
                remarks="",
                tags=[],
                source="APP",
                version=1,
            )
        )
        PostgresAuditSink(session).record(
            action="TASK_CREATED",
            entity_type="task",
            entity_id=task_id,
            actor="kaif",
            actor_kind="USER",
            at=NOW,
            after={"status": "PENDING"},
        )
        # Something goes wrong before commit -- a later step in the same
        # request raises. Application code rolls back the whole transaction.
        session.rollback()

        verify = factory()
        assert verify.get(TaskRow, task_id) is None
        audit_count = verify.execute(
            text("SELECT count(*) FROM audit_logs WHERE entity_id = :id"), {"id": task_id}
        ).scalar_one()
        assert audit_count == 0


class TestTamperDetection:
    def test_editing_a_written_entry_is_detected(self, db_session: Session) -> None:
        sink = PostgresAuditSink(db_session)
        sink.record(
            action="TASK_UPDATED",
            entity_type="task",
            entity_id="TSK-00104",
            actor="kaif",
            actor_kind="USER",
            at=NOW,
            before={"status": "PENDING"},
            after={"status": "COMPLETED"},
        )

        before = load_and_verify_chain(db_session)
        assert before.intact

        # The application role cannot do this -- see
        # test_schema_constraints.py::TestAuditLogIsAppendOnly. This uses the
        # owner connection specifically to simulate an attacker or an
        # operator with elevated access bypassing that restriction, to prove
        # the *hash chain* is the second line of defence behind the
        # database-level REVOKE, not merely decorative.
        db_session.execute(
            text(
                "UPDATE audit_logs SET after = '{\"status\": \"TAMPERED\"}' "
                "WHERE entity_id = 'TSK-00104'"
            )
        )

        after = load_and_verify_chain(db_session)
        assert not after.intact
        assert after.first_break is not None
        assert "altered after being written" in after.first_break.reason


@pytest.fixture
def cleanup_audit_entities() -> Iterator[list[str]]:
    """Entity ids to delete from audit_logs at teardown, via the owner role
    (the app role cannot delete audit rows by design -- see the REVOKE in the
    initial migration). Used only by the concurrency test below, which
    deliberately commits for real rather than rolling back, since the
    property under test only exists across genuinely separate transactions."""
    ids: list[str] = []
    yield ids


class TestConcurrentWriters:
    def test_concurrent_writers_do_not_fork_the_chain(
        self,
        migrated_engine: Engine,
        cleanup_audit_entities: list[str],
    ) -> None:
        """The property the advisory lock exists for. Each thread opens its
        own connection and commits for real -- a single rolled-back
        connection (the usual `db_session` fixture) cannot exercise this,
        because the lock only matters across independent transactions."""
        thread_count = 8
        # Bare ULIDs, not a human-readable prefix -- ULIDs are already
        # globally unique on their own, and entity_id is String(26): a
        # "concurrency-test-" prefix would overflow that column outright.
        entity_ids = [new_id() for _ in range(thread_count)]
        cleanup_audit_entities.extend(entity_ids)
        errors: list[BaseException] = []

        def write_one(entity_id: str) -> None:
            connection = migrated_engine.connect()
            try:
                with connection.begin():
                    session = sessionmaker(bind=connection, future=True)()
                    PostgresAuditSink(session).record(
                        action="TASK_UPDATED",
                        entity_type="task",
                        entity_id=entity_id,
                        actor="concurrency-test",
                        actor_kind="SYSTEM",
                        at=NOW,
                        after={"marker": entity_id},
                    )
            except BaseException as exc:
                errors.append(exc)
            finally:
                connection.close()

        threads = [threading.Thread(target=write_one, args=(eid,)) for eid in entity_ids]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        try:
            assert not errors, f"writer thread(s) raised: {errors}"

            with migrated_engine.connect() as verify_conn:
                verify_session = sessionmaker(bind=verify_conn, future=True)()
                result = load_and_verify_chain(verify_session)
                assert result.intact, (
                    f"chain broke at seq={result.first_break.seq if result.first_break else None}: "
                    f"{result.first_break.reason if result.first_break else ''}"
                )

                written = verify_session.execute(
                    text(
                        "SELECT count(*) FROM audit_logs WHERE entity_id = ANY(:ids)"
                    ),
                    {"ids": entity_ids},
                ).scalar_one()
                assert written == thread_count
        finally:
            # Real commits need real cleanup -- via the owner role, since the
            # app role cannot delete audit rows.
            with migrated_engine.connect() as cleanup_conn:
                cleanup_conn.execute(
                    text("DELETE FROM audit_logs WHERE entity_id = ANY(:ids)"),
                    {"ids": entity_ids},
                )
                cleanup_conn.commit()
