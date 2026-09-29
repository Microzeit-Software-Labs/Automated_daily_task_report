"""SyncRepository against a real database: the reconciliation cursor and
parked conflicts.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from interlock.adapters.persistence.sync_repository import SyncRepository
from interlock.adapters.persistence.task_repository import PostgresTaskRepository
from interlock.domain.common.errors import NotFoundError, SyncConflictAlreadyResolvedError
from interlock.domain.tasks.entities import Task
from tests.conftest import BRIEF_EVENING, make_task

T0 = BRIEF_EVENING
HASH_A = "a" * 64
HASH_B = "b" * 64


def make_saved_task(session: Session, **overrides: object) -> Task:
    return PostgresTaskRepository(session).add(make_task(**overrides))  # type: ignore[arg-type]


class TestUpsertState:
    def test_first_call_inserts(self, db_session: Session) -> None:
        task = make_saved_task(db_session)
        repo = SyncRepository(db_session)

        state = repo.upsert_state(task.id, version=1, content_hash=HASH_A, at=T0, row_index=2)

        assert state.task_id == task.id
        assert state.last_synced_version == 1
        assert state.last_synced_content_hash == HASH_A
        assert state.last_known_row_index == 2

    def test_second_call_updates_in_place(self, db_session: Session) -> None:
        task = make_saved_task(db_session)
        repo = SyncRepository(db_session)
        repo.upsert_state(task.id, version=1, content_hash=HASH_A, at=T0, row_index=2)

        updated = repo.upsert_state(
            task.id, version=2, content_hash=HASH_B, at=T0 + dt.timedelta(minutes=1), row_index=3
        )

        assert updated.last_synced_version == 2
        assert updated.last_synced_content_hash == HASH_B
        assert updated.last_known_row_index == 3
        assert repo.get_state(task.id) == updated

    def test_omitting_row_index_on_update_preserves_the_stored_one(
        self, db_session: Session
    ) -> None:
        task = make_saved_task(db_session)
        repo = SyncRepository(db_session)
        repo.upsert_state(task.id, version=1, content_hash=HASH_A, at=T0, row_index=5)

        updated = repo.upsert_state(task.id, version=2, content_hash=HASH_B, at=T0)

        assert updated.last_known_row_index == 5

    def test_get_state_is_none_before_any_sync(self, db_session: Session) -> None:
        task = make_saved_task(db_session)
        assert SyncRepository(db_session).get_state(task.id) is None


class TestMostRecentSyncAt:
    def test_none_when_nothing_has_synced(self, db_session: Session) -> None:
        assert SyncRepository(db_session).most_recent_sync_at() is None

    def test_returns_the_latest_across_tasks(self, db_session: Session) -> None:
        repo = SyncRepository(db_session)
        earlier = make_saved_task(db_session)
        later = make_saved_task(db_session)
        repo.upsert_state(earlier.id, version=1, content_hash=HASH_A, at=T0)
        repo.upsert_state(later.id, version=1, content_hash=HASH_B, at=T0 + dt.timedelta(hours=1))

        assert repo.most_recent_sync_at() == T0 + dt.timedelta(hours=1)


class TestConflicts:
    def test_record_and_get(self, db_session: Session) -> None:
        task = make_saved_task(db_session)
        repo = SyncRepository(db_session)

        conflict = repo.record_conflict(
            task.id,
            detected_at=T0,
            db_value={"title": "DB version"},
            external_value={"title": "Sheet version"},
        )

        assert repo.get_conflict(conflict.id) == conflict
        assert conflict.db_value == {"title": "DB version"}
        assert conflict.external_value == {"title": "Sheet version"}
        assert conflict.is_open
        assert repo.has_open_conflict(task.id) is True

    def test_get_conflict_is_none_for_an_unknown_id(self, db_session: Session) -> None:
        assert SyncRepository(db_session).get_conflict("not-a-real-id") is None

    def test_has_open_conflict_is_false_with_none_recorded(self, db_session: Session) -> None:
        task = make_saved_task(db_session)
        assert SyncRepository(db_session).has_open_conflict(task.id) is False

    def test_a_vanished_row_records_a_null_external_value(self, db_session: Session) -> None:
        task = make_saved_task(db_session)
        repo = SyncRepository(db_session)

        conflict = repo.record_conflict(
            task.id, detected_at=T0, db_value={"title": "DB version"}, external_value=None
        )

        assert conflict.external_value is None

    def test_a_second_open_conflict_for_the_same_task_is_rejected(
        self, db_session: Session
    ) -> None:
        task = make_saved_task(db_session)
        repo = SyncRepository(db_session)
        repo.record_conflict(task.id, detected_at=T0, db_value={}, external_value={})

        with pytest.raises(IntegrityError, match="uq_sync_conflicts_open"):
            repo.record_conflict(task.id, detected_at=T0, db_value={}, external_value={})

    def test_a_second_conflict_is_allowed_once_the_first_is_resolved(
        self, db_session: Session
    ) -> None:
        task = make_saved_task(db_session)
        repo = SyncRepository(db_session)
        first = repo.record_conflict(task.id, detected_at=T0, db_value={}, external_value={})
        repo.resolve_conflict(first.id, resolution="kept_db", resolved_by="kaif", resolved_at=T0)

        second = repo.record_conflict(task.id, detected_at=T0, db_value={}, external_value={})
        assert second.id != first.id

    def test_list_conflicts_defaults_to_open_only(self, db_session: Session) -> None:
        task = make_saved_task(db_session)
        repo = SyncRepository(db_session)
        open_conflict = repo.record_conflict(
            task.id, detected_at=T0, db_value={}, external_value={}
        )

        assert [c.id for c in repo.list_conflicts()] == [open_conflict.id]

    def test_list_conflicts_can_include_resolved(self, db_session: Session) -> None:
        task = make_saved_task(db_session)
        repo = SyncRepository(db_session)
        resolved = repo.record_conflict(task.id, detected_at=T0, db_value={}, external_value={})
        repo.resolve_conflict(
            resolved.id, resolution="kept_db", resolved_by="kaif", resolved_at=T0
        )

        ids = [c.id for c in repo.list_conflicts(open_only=False)]
        assert resolved.id in ids
        assert [c.id for c in repo.list_conflicts(open_only=True)] == []

    def test_list_conflicts_orders_by_detected_at(self, db_session: Session) -> None:
        repo = SyncRepository(db_session)
        later_task = make_saved_task(db_session)
        later = repo.record_conflict(
            later_task.id, detected_at=T0 + dt.timedelta(minutes=5), db_value={}, external_value={}
        )
        repo.resolve_conflict(later.id, resolution="kept_db", resolved_by="kaif", resolved_at=T0)
        # A second task: the unique-open-conflict index allows only one OPEN
        # conflict per task, so ordering needs two rows visible together.
        earlier_task = make_saved_task(db_session)
        earlier = repo.record_conflict(
            earlier_task.id, detected_at=T0, db_value={}, external_value={}
        )

        ids = [c.id for c in repo.list_conflicts(open_only=False)]
        assert ids.index(earlier.id) < ids.index(later.id)


class TestResolveConflict:
    def test_kept_db_closes_it(self, db_session: Session) -> None:
        task = make_saved_task(db_session)
        repo = SyncRepository(db_session)
        conflict = repo.record_conflict(task.id, detected_at=T0, db_value={}, external_value={})

        resolved = repo.resolve_conflict(
            conflict.id,
            resolution="kept_db",
            resolved_by="kaif",
            resolved_at=T0 + dt.timedelta(minutes=1),
        )

        assert resolved.is_open is False
        assert resolved.resolution == "kept_db"
        assert resolved.resolved_by == "kaif"
        assert repo.has_open_conflict(task.id) is False

    def test_resolving_twice_raises(self, db_session: Session) -> None:
        task = make_saved_task(db_session)
        repo = SyncRepository(db_session)
        conflict = repo.record_conflict(task.id, detected_at=T0, db_value={}, external_value={})
        repo.resolve_conflict(conflict.id, resolution="kept_db", resolved_by="kaif", resolved_at=T0)

        with pytest.raises(SyncConflictAlreadyResolvedError):
            repo.resolve_conflict(
                conflict.id, resolution="kept_sheet", resolved_by="kaif", resolved_at=T0
            )

    def test_resolving_an_unknown_conflict_raises_not_found(self, db_session: Session) -> None:
        with pytest.raises(NotFoundError):
            SyncRepository(db_session).resolve_conflict(
                "not-a-real-id", resolution="kept_db", resolved_by="kaif", resolved_at=T0
            )


class TestConstraints:
    def test_resolution_must_be_a_known_value(self, db_session: Session) -> None:
        # Must fit VARCHAR(20) so the CHECK constraint is what actually
        # fires, rather than a column-width error masking it -- see the
        # equivalent note in test_schema_constraints.py.
        task = make_saved_task(db_session)
        repo = SyncRepository(db_session)
        conflict = repo.record_conflict(task.id, detected_at=T0, db_value={}, external_value={})

        with pytest.raises(IntegrityError, match="ck_sync_conflicts_resolution"):
            repo.resolve_conflict(
                conflict.id, resolution="bogus", resolved_by="kaif", resolved_at=T0
            )
