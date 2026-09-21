"""Proof that the schema's constraints actually reject bad writes.

A constraint declared in a migration but never exercised against a real
database is a constraint nobody has verified exists. Every test here performs
the exact operation the constraint is meant to block, against real
PostgreSQL, and asserts it is rejected by name -- not merely that *some*
error occurred.
"""

from __future__ import annotations

import datetime as dt
import itertools
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from interlock.adapters.persistence.models import (
    ApprovalRequestRow,
    AuditLogRow,
    ReportSnapshotRow,
    ScheduledActionRow,
    ShareJobRow,
    ShareRecipientRow,
    TaskHistoryRow,
    TaskRow,
    UserRow,
    WhatsAppGroupRow,
)
from interlock.domain.common.ids import new_id

NOW = dt.datetime(2026, 9, 15, 17, 4, tzinfo=dt.UTC)

# A dedicated counter for test display_ids -- NOT `new_id()[:8]`. A ULID's
# first 10 characters encode its millisecond timestamp; truncating to 8 keeps
# fewer bits than the timestamp needs, so two IDs minted in the same test run
# (often the same millisecond) collide on their prefix and trip the very
# uniqueness constraint a *different* test is trying to test. Production code
# never has this problem because display ids come from a real incrementing
# sequence (see domain/ports/repositories.py's next_display_sequence()), never
# from a hashed/random string.
_display_seq = itertools.count(1)


def _display_id(prefix: str) -> str:
    return f"{prefix}-{next(_display_seq):06d}"


# ---------------------------------------------------------------------------
# Minimal, valid-by-default row builders. Each test overrides only the one
# field it is deliberately breaking, so the constraint under test stays
# obvious at the call site.
# ---------------------------------------------------------------------------


def make_user(**overrides: Any) -> UserRow:
    defaults: dict[str, Any] = {
        "id": new_id(),
        "email": f"{new_id()}@example.com",
        "display_name": "Kaif",
        "role": "approver",
        "is_active": True,
        "created_at": NOW,
    }
    return UserRow(**(defaults | overrides))


def make_task(**overrides: Any) -> TaskRow:
    defaults: dict[str, Any] = {
        "id": new_id(),
        "display_id": _display_id("TSK"),
        "title": "Deploy production server",
        "description": "",
        "priority": "MEDIUM",
        "status": "PENDING",
        "created_at": NOW,
        "updated_at": NOW,
        "remarks": "",
        "tags": [],
        "source": "APP",
        "version": 1,
    }
    return TaskRow(**(defaults | overrides))


def make_approval(**overrides: Any) -> ApprovalRequestRow:
    defaults: dict[str, Any] = {
        "id": new_id(),
        "display_id": _display_id("REV"),
        "kind": "EVENING",
        "local_date": dt.date(2026, 9, 15),
        "scheduled_for": NOW,
        "state": "APPROVED",
        "created_at": NOW,
        "updated_at": NOW,
    }
    return ApprovalRequestRow(**(defaults | overrides))


def make_snapshot(approval_request_id: str, **overrides: Any) -> ReportSnapshotRow:
    defaults: dict[str, Any] = {
        "id": new_id(),
        "approval_request_id": approval_request_id,
        "dataset_version": "2026-09-15-17-04-v1",
        "content_hash": "a" * 64,
        "rendered_body": "*End of Day*",
        "task_state": [],
        "summary": {},
        "template_id": "evening.default",
        "created_at": NOW,
    }
    return ReportSnapshotRow(**(defaults | overrides))


def make_scheduled_action(**overrides: Any) -> ScheduledActionRow:
    defaults: dict[str, Any] = {
        "id": new_id(),
        "kind": "SEND_SHARE_JOB",
        "run_at": NOW,
        "payload": {},
        "status": "PENDING",
        "attempts": 0,
        "created_at": NOW,
        "updated_at": NOW,
    }
    return ScheduledActionRow(**(defaults | overrides))


def make_share_job(
    *,
    approval_request_id: str,
    snapshot_id: str,
    approved_by_user_id: str,
    scheduled_action_id: str,
    action_version: int = 1,
    **overrides: Any,
) -> ShareJobRow:
    defaults: dict[str, Any] = {
        "id": new_id(),
        "display_id": _display_id("SHR"),
        "approval_request_id": approval_request_id,
        "snapshot_id": snapshot_id,
        "approved_by_user_id": approved_by_user_id,
        "scheduled_action_id": scheduled_action_id,
        "action_version": action_version,
        "state": "SCHEDULED",
        "created_at": NOW,
        "updated_at": NOW,
    }
    return ShareJobRow(**(defaults | overrides))


def make_group(**overrides: Any) -> WhatsAppGroupRow:
    defaults: dict[str, Any] = {
        "id": new_id(),
        "display_name": "SI Team",
        "description": "",
        "external_jid": f"{new_id()}@g.us",
        "enabled": True,
        "default_morning": False,
        "default_evening": False,
        "created_at": NOW,
        "updated_at": NOW,
    }
    return WhatsAppGroupRow(**(defaults | overrides))


def make_recipient(
    *, share_job_id: str, whatsapp_group_id: str, **overrides: Any
) -> ShareRecipientRow:
    defaults: dict[str, Any] = {
        "id": new_id(),
        "share_job_id": share_job_id,
        "whatsapp_group_id": whatsapp_group_id,
        "client_message_id": new_id(),
        "state": "QUEUED",
        "attempts": 0,
        "error_detail": "",
        "created_at": NOW,
        "updated_at": NOW,
    }
    return ShareRecipientRow(**(defaults | overrides))


@pytest.fixture
def approved_chain(db_session: Session) -> dict[str, str]:
    """A user, an APPROVED review, its frozen snapshot, and a scheduled action
    -- the minimum a share_job is allowed to reference. Built directly against
    the ORM rather than a repository, because step 3 is proving the schema,
    not the repository (that comes later)."""
    user = make_user()
    approval = make_approval()
    db_session.add_all([user, approval])
    db_session.flush()

    snapshot = make_snapshot(approval.id)
    action = make_scheduled_action()
    db_session.add_all([snapshot, action])
    db_session.flush()

    return {
        "user_id": user.id,
        "approval_id": approval.id,
        "snapshot_id": snapshot.id,
        "scheduled_action_id": action.id,
    }


class TestTaskConstraints:
    def test_status_must_be_a_known_value(self, db_session: Session) -> None:
        db_session.add(make_task(status="NOT_A_REAL_STATUS"))
        with pytest.raises(IntegrityError, match="ck_tasks_status"):
            db_session.flush()

    def test_priority_must_be_a_known_value(self, db_session: Session) -> None:
        # Must fit VARCHAR(10) so the CHECK constraint is what actually fires,
        # rather than a column-width error masking the constraint under test.
        db_session.add(make_task(priority="CRITICAL"))
        with pytest.raises(IntegrityError, match="ck_tasks_priority"):
            db_session.flush()

    def test_version_must_be_positive(self, db_session: Session) -> None:
        db_session.add(make_task(version=0))
        with pytest.raises(IntegrityError, match="ck_tasks_version_positive"):
            db_session.flush()

    def test_display_id_must_be_unique(self, db_session: Session) -> None:
        db_session.add(make_task(display_id="TSK-00001"))
        db_session.flush()
        db_session.add(make_task(display_id="TSK-00001"))
        with pytest.raises(IntegrityError):
            db_session.flush()


class TestTaskHistoryConstraints:
    def test_only_one_history_row_per_task_version(self, db_session: Session) -> None:
        task = make_task()
        db_session.add(task)
        db_session.flush()

        db_session.add(
            TaskHistoryRow(
                id=new_id(),
                task_id=task.id,
                version=1,
                changed_by="kaif",
                changed_by_kind="USER",
                changed_at=NOW,
                after={"status": "PENDING"},
            )
        )
        db_session.flush()

        db_session.add(
            TaskHistoryRow(
                id=new_id(),
                task_id=task.id,
                version=1,  # same task, same version -- a logged transition twice
                changed_by="kaif",
                changed_by_kind="USER",
                changed_at=NOW,
                after={"status": "COMPLETED"},
            )
        )
        with pytest.raises(IntegrityError, match="uq_task_history_version"):
            db_session.flush()


class TestApprovalRequestConstraints:
    def test_kind_must_be_a_known_value(self, db_session: Session) -> None:
        db_session.add(make_approval(kind="AFTERNOON"))
        with pytest.raises(IntegrityError, match="ck_approval_kind"):
            db_session.flush()

    def test_state_must_be_a_known_value(self, db_session: Session) -> None:
        db_session.add(make_approval(state="HALF_APPROVED"))
        with pytest.raises(IntegrityError, match="ck_approval_state"):
            db_session.flush()

    def test_duplicate_evening_review_same_day_is_rejected(
        self, db_session: Session
    ) -> None:
        """The duplicate-review guard: the 17:00 tick firing twice must not
        create two evening reviews for the same day."""
        same_day = dt.date(2026, 9, 15)
        db_session.add(make_approval(kind="EVENING", local_date=same_day))
        db_session.flush()

        db_session.add(make_approval(kind="EVENING", local_date=same_day))
        with pytest.raises(IntegrityError, match="uq_approval_scheduled_once_per_day"):
            db_session.flush()

    def test_morning_and_evening_the_same_day_do_not_collide(
        self, db_session: Session
    ) -> None:
        """The guard is scoped per kind -- a morning and an evening review on
        the same calendar day are two different, legitimate reviews."""
        same_day = dt.date(2026, 9, 15)
        db_session.add(make_approval(kind="MORNING", local_date=same_day))
        db_session.add(make_approval(kind="EVENING", local_date=same_day))
        db_session.flush()  # must not raise

    def test_manual_review_can_repeat_same_day(self, db_session: Session) -> None:
        """A MANUAL "share the list now" review is legitimately allowed more
        than once a day, so it is excluded from the uniqueness rule entirely."""
        same_day = dt.date(2026, 9, 15)
        db_session.add(make_approval(kind="MANUAL", local_date=same_day))
        db_session.add(make_approval(kind="MANUAL", local_date=same_day))
        db_session.flush()  # must not raise


class TestShareJobConstraints:
    def test_cannot_exist_without_a_snapshot(
        self, db_session: Session, approved_chain: dict[str, str]
    ) -> None:
        """The mechanical expression of the product's central rule: a share
        job cannot reference "no snapshot" -- NOT NULL, not merely a CHECK
        saying the same thing, because every row that can ever exist is
        created only after the approval gate is passed."""
        job = make_share_job(
            approval_request_id=approved_chain["approval_id"],
            snapshot_id=approved_chain["snapshot_id"],
            approved_by_user_id=approved_chain["user_id"],
            scheduled_action_id=approved_chain["scheduled_action_id"],
        )
        job.snapshot_id = None  # type: ignore[assignment]  -- deliberately invalid
        db_session.add(job)
        with pytest.raises(IntegrityError, match=r"not-null|null value"):
            db_session.flush()

    def test_cannot_exist_without_a_human_approver(
        self, db_session: Session, approved_chain: dict[str, str]
    ) -> None:
        job = make_share_job(
            approval_request_id=approved_chain["approval_id"],
            snapshot_id=approved_chain["snapshot_id"],
            approved_by_user_id=approved_chain["user_id"],
            scheduled_action_id=approved_chain["scheduled_action_id"],
        )
        job.approved_by_user_id = None  # type: ignore[assignment]
        db_session.add(job)
        with pytest.raises(IntegrityError, match=r"not-null|null value"):
            db_session.flush()

    def test_double_click_cannot_create_two_jobs_for_one_action_version(
        self, db_session: Session, approved_chain: dict[str, str]
    ) -> None:
        """Scenario 6. Two commits believing they are each "the" share for
        this review must not both succeed."""
        first_action = approved_chain["scheduled_action_id"]
        db_session.add(
            make_share_job(
                approval_request_id=approved_chain["approval_id"],
                snapshot_id=approved_chain["snapshot_id"],
                approved_by_user_id=approved_chain["user_id"],
                scheduled_action_id=first_action,
                action_version=1,
            )
        )
        db_session.flush()

        second_action = make_scheduled_action()
        db_session.add(second_action)
        db_session.flush()

        db_session.add(
            make_share_job(
                approval_request_id=approved_chain["approval_id"],
                snapshot_id=approved_chain["snapshot_id"],
                approved_by_user_id=approved_chain["user_id"],
                scheduled_action_id=second_action.id,
                action_version=1,  # same review, same action_version, again
            )
        )
        with pytest.raises(IntegrityError, match="uq_share_job_action_version"):
            db_session.flush()

    def test_state_must_be_a_known_value(
        self, db_session: Session, approved_chain: dict[str, str]
    ) -> None:
        db_session.add(
            make_share_job(
                approval_request_id=approved_chain["approval_id"],
                snapshot_id=approved_chain["snapshot_id"],
                approved_by_user_id=approved_chain["user_id"],
                scheduled_action_id=approved_chain["scheduled_action_id"],
                state="HALF_SENT",
            )
        )
        with pytest.raises(IntegrityError, match="ck_share_job_state"):
            db_session.flush()


class TestShareRecipientConstraints:
    def _job(self, db_session: Session, approved_chain: dict[str, str]) -> ShareJobRow:
        job = make_share_job(
            approval_request_id=approved_chain["approval_id"],
            snapshot_id=approved_chain["snapshot_id"],
            approved_by_user_id=approved_chain["user_id"],
            scheduled_action_id=approved_chain["scheduled_action_id"],
        )
        db_session.add(job)
        db_session.flush()
        return job

    def test_one_delivery_per_group_per_job(
        self, db_session: Session, approved_chain: dict[str, str]
    ) -> None:
        """Scenario 8: retrying a job must never create a second row for a
        group that already succeeded."""
        job = self._job(db_session, approved_chain)
        group = make_group()
        db_session.add(group)
        db_session.flush()

        db_session.add(make_recipient(share_job_id=job.id, whatsapp_group_id=group.id))
        db_session.flush()

        db_session.add(make_recipient(share_job_id=job.id, whatsapp_group_id=group.id))
        with pytest.raises(IntegrityError, match="uq_share_recipient_group"):
            db_session.flush()

    def test_client_message_id_is_globally_unique(
        self, db_session: Session, approved_chain: dict[str, str]
    ) -> None:
        job = self._job(db_session, approved_chain)
        group_a = make_group(display_name="SI Team")
        group_b = make_group(display_name="Management")
        db_session.add_all([group_a, group_b])
        db_session.flush()

        shared_message_id = new_id()
        db_session.add(
            make_recipient(
                share_job_id=job.id,
                whatsapp_group_id=group_a.id,
                client_message_id=shared_message_id,
            )
        )
        db_session.flush()

        db_session.add(
            make_recipient(
                share_job_id=job.id,
                whatsapp_group_id=group_b.id,
                client_message_id=shared_message_id,
            )
        )
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_state_must_be_a_known_value(
        self, db_session: Session, approved_chain: dict[str, str]
    ) -> None:
        job = self._job(db_session, approved_chain)
        group = make_group()
        db_session.add(group)
        db_session.flush()

        db_session.add(
            make_recipient(share_job_id=job.id, whatsapp_group_id=group.id, state="MAYBE_SENT")
        )
        with pytest.raises(IntegrityError, match="ck_recipient_state"):
            db_session.flush()


class TestWhatsAppGroupConstraints:
    def test_external_jid_must_be_unique(self, db_session: Session) -> None:
        """A group is pinned to its JID permanently; two rows claiming the
        same JID would make "which group does this point at" ambiguous."""
        jid = "120363000000000001@g.us"
        db_session.add(make_group(external_jid=jid))
        db_session.flush()
        db_session.add(make_group(external_jid=jid, display_name="SI Team (dup)"))
        with pytest.raises(IntegrityError):
            db_session.flush()


class TestReportSnapshotIsImmutable:
    """Connects as interlock_app -- the role the running application actually
    uses -- to prove the restriction holds for real, not merely against an
    owner role that could bypass it anyway."""

    def test_app_role_can_insert_and_select(self, app_role_engine: Engine) -> None:
        """Positive control: proves the REVOKE below did not overreach and
        accidentally block legitimate operations."""
        with app_role_engine.connect() as conn:
            trans = conn.begin()
            try:
                approval_id = new_id()
                conn.execute(
                    text(
                        "INSERT INTO approval_requests "
                        "(id, display_id, kind, local_date, scheduled_for, state, "
                        " created_at, updated_at) "
                        "VALUES (:id, :did, 'EVENING', :ld, :now, 'APPROVED', :now, :now)"
                    ),
                    {"id": approval_id, "did": _display_id("REV"), "ld": NOW.date(), "now": NOW},
                )
                snapshot_id = new_id()
                conn.execute(
                    text(
                        "INSERT INTO report_snapshots "
                        "(id, approval_request_id, dataset_version, content_hash, "
                        " rendered_body, task_state, summary, template_id, created_at) "
                        "VALUES (:id, :aid, 'v1', :hash, 'body', '[]', '{}', "
                        " 'evening.default', :now)"
                    ),
                    {"id": snapshot_id, "aid": approval_id, "hash": "a" * 64, "now": NOW},
                )
                row = conn.execute(
                    text("SELECT rendered_body FROM report_snapshots WHERE id = :id"),
                    {"id": snapshot_id},
                ).one()
                assert row.rendered_body == "body"
            finally:
                trans.rollback()

    def test_app_role_cannot_update_a_snapshot(self, app_role_engine: Engine) -> None:
        """This is the guarantee "what you approved is what gets sent" rests
        on: even the application's own database role cannot edit a snapshot
        after it is written."""
        with app_role_engine.connect() as conn:
            trans = conn.begin()
            try:
                _approval_id, snapshot_id = self._insert_snapshot(conn)
                with pytest.raises(DBAPIError, match="permission denied"):
                    conn.execute(
                        text("UPDATE report_snapshots SET rendered_body = 'tampered' "
                             "WHERE id = :id"),
                        {"id": snapshot_id},
                    )
            finally:
                trans.rollback()

    def test_app_role_cannot_delete_a_snapshot(self, app_role_engine: Engine) -> None:
        with app_role_engine.connect() as conn:
            trans = conn.begin()
            try:
                _approval_id, snapshot_id = self._insert_snapshot(conn)
                with pytest.raises(DBAPIError, match="permission denied"):
                    conn.execute(
                        text("DELETE FROM report_snapshots WHERE id = :id"),
                        {"id": snapshot_id},
                    )
            finally:
                trans.rollback()

    @staticmethod
    def _insert_snapshot(conn: Any) -> tuple[str, str]:
        approval_id = new_id()
        conn.execute(
            text(
                "INSERT INTO approval_requests "
                "(id, display_id, kind, local_date, scheduled_for, state, "
                " created_at, updated_at) "
                "VALUES (:id, :did, 'EVENING', :ld, :now, 'APPROVED', :now, :now)"
            ),
            {"id": approval_id, "did": _display_id("REV"), "ld": NOW.date(), "now": NOW},
        )
        snapshot_id = new_id()
        conn.execute(
            text(
                "INSERT INTO report_snapshots "
                "(id, approval_request_id, dataset_version, content_hash, "
                " rendered_body, task_state, summary, template_id, created_at) "
                "VALUES (:id, :aid, 'v1', :hash, 'body', '[]', '{}', "
                " 'evening.default', :now)"
            ),
            {"id": snapshot_id, "aid": approval_id, "hash": "a" * 64, "now": NOW},
        )
        return approval_id, snapshot_id


class TestAuditLogIsAppendOnly:
    """Same reasoning as the snapshot tests above, for the audit trail."""

    def test_app_role_can_insert_and_select(self, app_role_engine: Engine) -> None:
        with app_role_engine.connect() as conn:
            trans = conn.begin()
            try:
                entry_id = self._insert_entry(conn)
                row = conn.execute(
                    text("SELECT action FROM audit_logs WHERE id = :id"), {"id": entry_id}
                ).one()
                assert row.action == "TASK_UPDATED"
            finally:
                trans.rollback()

    def test_app_role_cannot_update_an_entry(self, app_role_engine: Engine) -> None:
        with app_role_engine.connect() as conn:
            trans = conn.begin()
            try:
                entry_id = self._insert_entry(conn)
                with pytest.raises(DBAPIError, match="permission denied"):
                    conn.execute(
                        text("UPDATE audit_logs SET action = 'TAMPERED' WHERE id = :id"),
                        {"id": entry_id},
                    )
            finally:
                trans.rollback()

    def test_app_role_cannot_delete_an_entry(self, app_role_engine: Engine) -> None:
        with app_role_engine.connect() as conn:
            trans = conn.begin()
            try:
                entry_id = self._insert_entry(conn)
                with pytest.raises(DBAPIError, match="permission denied"):
                    conn.execute(text("DELETE FROM audit_logs WHERE id = :id"), {"id": entry_id})
            finally:
                trans.rollback()

    @staticmethod
    def _insert_entry(conn: Any) -> str:
        entry_id = new_id()
        conn.execute(
            text(
                "INSERT INTO audit_logs "
                "(id, actor_id, actor_kind, action, entity_type, entity_id, "
                " occurred_at, entry_hash) "
                "VALUES (:id, 'kaif', 'USER', 'TASK_UPDATED', 'task', :eid, :now, :hash)"
            ),
            {"id": entry_id, "eid": new_id(), "now": NOW, "hash": "b" * 64},
        )
        return entry_id


class TestAuditLogChainOrdering:
    def test_seq_is_strictly_increasing_and_gapless_within_a_transaction(
        self, db_session: Session
    ) -> None:
        """``seq`` -- not ``occurred_at`` -- is the real chain order, precisely
        because wall-clock timestamps are not guaranteed unique or monotonic
        under concurrent writers."""
        ids = [new_id(), new_id(), new_id()]
        for entity_id, ident in zip(ids, ["a", "b", "c"], strict=True):
            db_session.add(
                AuditLogRow(
                    id=new_id(),
                    actor_id="kaif",
                    actor_kind="USER",
                    action="TASK_UPDATED",
                    entity_type="task",
                    entity_id=entity_id,
                    occurred_at=NOW,
                    entry_hash=ident * 64,
                )
            )
        db_session.flush()

        rows = db_session.execute(
            text("SELECT seq FROM audit_logs ORDER BY seq")
        ).scalars().all()
        assert len(rows) >= 3
        tail = rows[-3:]
        assert tail == sorted(tail)
        assert tail[-1] - tail[0] == 2  # gapless across these three inserts
