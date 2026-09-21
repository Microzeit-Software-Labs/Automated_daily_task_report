"""PostgresTaskRepository against a real database.

The one property worth real scrutiny: concurrent updates must not silently
overwrite each other. A "fetch, compare in Python, then write" scheme has a
race window where that can happen; the atomic conditional UPDATE this
repository uses should not.
"""

from __future__ import annotations

import datetime as dt
import itertools
import threading

import pytest
from sqlalchemy.orm import Session, sessionmaker

from interlock.adapters.persistence.task_repository import PostgresTaskRepository
from interlock.domain.common.errors import NotFoundError, TaskVersionConflictError
from interlock.domain.common.ids import format_display_id, new_id
from interlock.domain.ports.repositories import TaskFilter
from interlock.domain.tasks.entities import Priority, Task, TaskStatus, apply_edit

NOW = dt.datetime(2026, 9, 15, 17, 4, tzinfo=dt.UTC)

# Plain counter, not a slice of a ULID: a ULID's alphabet (Crockford base32)
# includes letters like W/Y/Z that are not valid base-32 digits, which is
# exactly the mistake this replaced.
_display_seq = itertools.count(1)


def make_task(**overrides: object) -> Task:
    defaults: dict[str, object] = {
        "id": new_id(),
        "display_id": format_display_id("TSK", next(_display_seq)),
        "title": "Deploy production server",
        "status": TaskStatus.PENDING,
        "priority": Priority.MEDIUM,
        "created_at": NOW,
        "updated_at": NOW,
        "version": 1,
    }
    defaults.update(overrides)
    return Task(**defaults)  # type: ignore[arg-type]


class TestCrud:
    def test_add_then_get_round_trips(self, db_session: Session) -> None:
        repo = PostgresTaskRepository(db_session)
        task = make_task(title="Deploy customer server")
        repo.add(task)

        fetched = repo.get(task.id)
        assert fetched is not None
        assert fetched.title == "Deploy customer server"
        assert fetched.tags == ()

    def test_get_by_display_id(self, db_session: Session) -> None:
        repo = PostgresTaskRepository(db_session)
        task = make_task(display_id="TSK-100001")
        repo.add(task)

        fetched = repo.get_by_display_id("TSK-100001")
        assert fetched is not None
        assert fetched.id == task.id

    def test_get_missing_returns_none(self, db_session: Session) -> None:
        assert PostgresTaskRepository(db_session).get(new_id()) is None

    def test_round_trip_preserves_tags(self, db_session: Session) -> None:
        repo = PostgresTaskRepository(db_session)
        task = make_task(tags=("urgent", "customer-facing"))
        repo.add(task)

        fetched = repo.get(task.id)
        assert fetched is not None
        assert set(fetched.tags) == {"urgent", "customer-facing"}


class TestOptimisticConcurrency:
    def test_update_with_the_right_version_succeeds(self, db_session: Session) -> None:
        repo = PostgresTaskRepository(db_session)
        task = make_task(version=1)
        repo.add(task)

        edited = apply_edit(task, {"status": TaskStatus.COMPLETED}, now=NOW)
        saved = repo.update(edited, expected_version=1)

        assert saved.version == 2
        assert repo.get(task.id).status is TaskStatus.COMPLETED  # type: ignore[union-attr]

    def test_update_with_a_stale_version_is_rejected(self, db_session: Session) -> None:
        """Scenario: the review panel was opened at version 7, someone else's
        edit already landed, and the panel's own save must not silently win."""
        repo = PostgresTaskRepository(db_session)
        task = make_task(version=1)
        repo.add(task)

        # Someone else's edit lands first.
        first_edit = apply_edit(task, {"remarks": "Deployment started"}, now=NOW)
        repo.update(first_edit, expected_version=1)  # -> version 2

        # The stale caller still believes it is at version 1.
        stale_edit = apply_edit(task, {"status": TaskStatus.COMPLETED}, now=NOW)
        with pytest.raises(TaskVersionConflictError) as exc:
            repo.update(stale_edit, expected_version=1)

        assert exc.value.details["expected_version"] == 1
        assert exc.value.details["actual_version"] == 2
        # The winning edit must still be intact -- the rejected one must not
        # have partially applied.
        current = repo.get(task.id)
        assert current is not None
        assert current.remarks == "Deployment started"
        assert current.status is TaskStatus.PENDING

    def test_updating_a_nonexistent_task_raises_not_found(
        self, db_session: Session
    ) -> None:
        phantom = make_task()
        with pytest.raises(NotFoundError):
            PostgresTaskRepository(db_session).update(phantom, expected_version=1)

    def test_concurrent_updates_do_not_silently_overwrite_each_other(
        self, migrated_engine: object
    ) -> None:
        """The property the atomic conditional UPDATE exists for. Two
        genuinely separate transactions race to update the same row starting
        from the same version; exactly one must win and the other must see a
        version conflict -- never both silently "succeeding" with one
        clobbering the other.

        Uses real, separate connections and commits, like the audit chain's
        concurrency test -- a single rolled-back transaction cannot exercise
        real cross-transaction races.
        """
        task_id = new_id()
        display_id = f"TSK-{new_id()[:6]}"

        setup_conn = migrated_engine.connect()  # type: ignore[attr-defined]
        try:
            setup_session = sessionmaker(bind=setup_conn, future=True)()
            PostgresTaskRepository(setup_session).add(
                make_task(id=task_id, display_id=display_id, version=1)
            )
            setup_session.commit()
        finally:
            setup_conn.close()

        outcomes: list[str] = []
        barrier = threading.Barrier(2)

        def attempt(remark: str) -> None:
            conn = migrated_engine.connect()  # type: ignore[attr-defined]
            try:
                session = sessionmaker(bind=conn, future=True)()
                repo = PostgresTaskRepository(session)
                current = repo.get(task_id)
                assert current is not None
                edited = apply_edit(current, {"remarks": remark}, now=NOW)

                barrier.wait()  # force both threads to race the same write
                try:
                    repo.update(edited, expected_version=1)
                    session.commit()
                    outcomes.append("won")
                except TaskVersionConflictError:
                    session.rollback()
                    outcomes.append("conflict")
            finally:
                conn.close()

        threads = [
            threading.Thread(target=attempt, args=("thread A",)),
            threading.Thread(target=attempt, args=("thread B",)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        try:
            assert sorted(outcomes) == ["conflict", "won"], (
                f"expected exactly one winner and one conflict, got {outcomes}"
            )

            verify_conn = migrated_engine.connect()  # type: ignore[attr-defined]
            try:
                verify_session = sessionmaker(bind=verify_conn, future=True)()
                final = PostgresTaskRepository(verify_session).get(task_id)
                assert final is not None
                assert final.version == 2
                assert final.remarks in {"thread A", "thread B"}
            finally:
                verify_conn.close()
        finally:
            cleanup_conn = migrated_engine.connect()  # type: ignore[attr-defined]
            try:
                from sqlalchemy import text

                cleanup_conn.execute(text("DELETE FROM tasks WHERE id = :id"), {"id": task_id})
                cleanup_conn.commit()
            finally:
                cleanup_conn.close()


class TestFiltering:
    def test_filters_by_status(self, db_session: Session) -> None:
        repo = PostgresTaskRepository(db_session)
        repo.add(make_task(status=TaskStatus.PENDING))
        repo.add(make_task(status=TaskStatus.COMPLETED))

        pending = repo.list(TaskFilter(statuses=frozenset({TaskStatus.PENDING})))
        assert all(t.status is TaskStatus.PENDING for t in pending)

    def test_deleted_tasks_are_excluded_by_default(self, db_session: Session) -> None:
        repo = PostgresTaskRepository(db_session)
        task = make_task()
        repo.add(task)
        deleted = apply_edit(task, {}, now=NOW)  # no-op edit, just to get a copy
        import dataclasses

        repo.update(dataclasses.replace(deleted, deleted_at=NOW, version=2), expected_version=1)

        assert repo.get(task.id) is not None  # get() still finds it directly
        assert task.id not in {t.id for t in repo.list()}
        assert task.id in {t.id for t in repo.list(TaskFilter(include_deleted=True))}

    def test_search_matches_title(self, db_session: Session) -> None:
        repo = PostgresTaskRepository(db_session)
        repo.add(make_task(title="Deploy customer integration"))
        repo.add(make_task(title="Unrelated task"))

        results = repo.list(TaskFilter(search="customer"))
        assert len(results) == 1
        assert "customer" in results[0].title.lower()

    def test_filters_by_overlapping_tags(self, db_session: Session) -> None:
        repo = PostgresTaskRepository(db_session)
        repo.add(make_task(tags=("urgent",)))
        repo.add(make_task(tags=("low-priority",)))

        results = repo.list(TaskFilter(tags=frozenset({"urgent"})))
        assert len(results) == 1
        assert "urgent" in results[0].tags


class TestDisplaySequence:
    def test_sequence_is_monotonic(self, db_session: Session) -> None:
        repo = PostgresTaskRepository(db_session)
        first = repo.next_display_sequence()
        second = repo.next_display_sequence()
        assert second == first + 1

    def test_sequence_is_never_reused_even_after_rollback(
        self, migrated_engine: object
    ) -> None:
        """Sequences are not transactional in Postgres -- a rolled-back
        nextval() still consumed a number. That is a deliberate, acceptable
        property (gaps in a display id are harmless; a *reused* number is
        not) and this test documents that it holds, rather than assuming."""
        conn = migrated_engine.connect()  # type: ignore[attr-defined]
        try:
            trans = conn.begin()
            session = sessionmaker(bind=conn, future=True)()
            first = PostgresTaskRepository(session).next_display_sequence()
            trans.rollback()
        finally:
            conn.close()

        conn2 = migrated_engine.connect()  # type: ignore[attr-defined]
        try:
            session2 = sessionmaker(bind=conn2, future=True)()
            second = PostgresTaskRepository(session2).next_display_sequence()
            session2.commit()
        finally:
            conn2.close()

        assert second > first
