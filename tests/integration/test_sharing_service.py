"""SharingService's /commit orchestration against a real database.

Scenario numbers below refer to the product brief's nine acceptance
scenarios -- this file proves the mechanism each one depends on at the
service layer, before the end-to-end acceptance tests exercise them through
the full stack in tests/acceptance/.
"""

from __future__ import annotations

import datetime as dt
import os
import threading

import pytest
from sqlalchemy.orm import Session, sessionmaker

from interlock.adapters.persistence.approval_repository import ApprovalRepository
from interlock.adapters.persistence.audit_sink import PostgresAuditSink
from interlock.adapters.persistence.base import make_engine
from interlock.adapters.persistence.scheduler_repository import SchedulerRepository
from interlock.adapters.persistence.sequences import PostgresSequences
from interlock.adapters.persistence.share_repository import ShareRepository
from interlock.adapters.persistence.task_repository import PostgresTaskRepository
from interlock.adapters.persistence.whatsapp_group_repository import WhatsAppGroupRepository
from interlock.domain.approvals.request import ApprovalKind
from interlock.domain.approvals.states import ApprovalState, RecipientState, ShareJobState
from interlock.domain.common.actor import Actor, ActorKind
from interlock.domain.common.clock import FrozenClock
from interlock.domain.common.errors import (
    IllegalTransitionError,
    NoRecipientsSelectedError,
)
from interlock.domain.tasks.entities import TaskStatus
from interlock.services.review_service import ReviewService
from interlock.services.sharing_service import CommitMode, SharingService
from interlock.services.task_service import BulkUpdateItem, TaskService

NOW = dt.datetime(2026, 9, 15, 17, 4, tzinfo=dt.UTC)
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
KAIF = Actor(kind=ActorKind.USER, id="u-1", label="kaif")
SCHEDULER = Actor(kind=ActorKind.SYSTEM, id="scheduler", label="system:scheduler")


def seed_user(session: Session, actor: Actor, *, now: dt.datetime = NOW) -> None:
    """``approval_requests.approved_by_user_id`` is a real FK to ``users`` --
    approving anything as ``actor`` requires a row to actually exist first.

    Called once per test's own session (or once in the concurrency test's
    single-threaded setup phase, never inside the racing threads themselves --
    50 threads all inserting "u-1" concurrently would just trade one race for
    another).
    """
    from interlock.adapters.persistence.models import UserRow

    if session.get(UserRow, actor.id) is None:
        session.add(
            UserRow(
                id=actor.id,
                email=f"{actor.id}@example.com",
                display_name=actor.label or actor.id,
                role="approver",
                is_active=True,
                created_at=now,
            )
        )
        session.flush()


def build(session: Session, *, at: dt.datetime = NOW):
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Asia/Kolkata")
    clock = FrozenClock(at)
    task_repo = PostgresTaskRepository(session)
    audit = PostgresAuditSink(session)
    approval_repo = ApprovalRepository(session)
    share_repo = ShareRepository(session)
    scheduler_repo = SchedulerRepository(session)
    group_repo = WhatsAppGroupRepository(session)
    sequences = PostgresSequences(session)

    task_service = TaskService(task_repo, audit, clock)
    review_service = ReviewService(approval_repo, task_repo, audit, tz)
    sharing_service = SharingService(
        approval_repo=approval_repo,
        share_repo=share_repo,
        scheduler_repo=scheduler_repo,
        sequences=sequences,
        task_service=task_service,
        task_repo=task_repo,
        audit=audit,
        tz=tz,
        max_message_length=4096,
    )
    return {
        "task_service": task_service,
        "review_service": review_service,
        "sharing_service": sharing_service,
        "group_repo": group_repo,
        "share_repo": share_repo,
        "scheduler_repo": scheduler_repo,
        "approval_repo": approval_repo,
    }


def make_review(
    services: dict, *, kind: ApprovalKind = ApprovalKind.EVENING, at: dt.datetime = NOW
):
    request, _ = services["review_service"].create_scheduled_review(kind, scheduled_for=at, now=at)
    return request


class TestScenario1UpdateOnly:
    def test_update_only_never_produces_a_share_job(self, db_session: Session) -> None:
        services = build(db_session)
        seed_user(db_session, KAIF)
        task = services["task_service"].create(title="Deploy production server", actor=KAIF)
        review = make_review(services)

        result = services["sharing_service"].commit(
            approval_request_id=review.id,
            mode=CommitMode.UPDATE_ONLY,
            action_version=1,
            actor=KAIF,
            now=NOW,
            task_updates=[
                BulkUpdateItem(task_id=task.id, changes={"remarks": "checked"}, expected_version=1)
            ],
        )

        assert result.job is None
        assert result.recipients == ()
        updated_request = services["review_service"].get(review.id)
        assert updated_request.state is ApprovalState.CLOSED_NO_SHARE
        assert services["task_service"].get(task.id).remarks == "checked"


class TestScenario2UpdateAndShare:
    def test_update_and_share_creates_an_approved_scheduled_job(self, db_session: Session) -> None:
        services = build(db_session)
        seed_user(db_session, KAIF)
        task = services["task_service"].create(
            title="Deploy customer server", actor=KAIF, status=TaskStatus.PENDING
        )
        review = make_review(services)
        group = services["group_repo"].add(
            display_name="SI Team", external_jid="120363000000000001@g.us", now=NOW
        )

        result = services["sharing_service"].commit(
            approval_request_id=review.id,
            mode=CommitMode.UPDATE_AND_SHARE,
            action_version=1,
            actor=KAIF,
            now=NOW,
            task_updates=[
                BulkUpdateItem(
                    task_id=task.id, changes={"status": TaskStatus.COMPLETED}, expected_version=1
                )
            ],
            recipient_group_ids=[group.id],
        )

        assert result.job is not None
        assert result.job.state is ShareJobState.SCHEDULED
        assert len(result.recipients) == 1
        assert result.recipients[0].state is RecipientState.QUEUED
        assert "Deploy customer server" in result.preview

        approved = services["review_service"].get(review.id)
        assert approved.state is ApprovalState.APPROVED
        assert approved.approved_by_user_id == "u-1"

    def test_approving_requires_a_human_actor(self, db_session: Session) -> None:
        """The mechanical enforcement of the product's central rule, exercised
        through the full commit path rather than just the state machine
        directly."""
        services = build(db_session)
        seed_user(db_session, KAIF)
        review = make_review(services)
        group = services["group_repo"].add(
            display_name="SI Team", external_jid="120363000000000002@g.us", now=NOW
        )

        with pytest.raises(Exception) as exc:  # ApprovalRequiredError
            services["sharing_service"].commit(
                approval_request_id=review.id,
                mode=CommitMode.SHARE_ONLY,
                action_version=1,
                actor=SCHEDULER,
                now=NOW,
                recipient_group_ids=[group.id],
            )
        assert exc.value.code == "APPROVAL_REQUIRED"  # type: ignore[attr-defined]


class TestScenario3ShareOnly:
    def test_share_only_does_not_touch_task_data(self, db_session: Session) -> None:
        services = build(db_session)
        seed_user(db_session, KAIF)
        task = services["task_service"].create(
            title="Database migration", actor=KAIF, status=TaskStatus.PENDING
        )
        review = make_review(services)
        group = services["group_repo"].add(
            display_name="Management", external_jid="120363000000000003@g.us", now=NOW
        )

        services["sharing_service"].commit(
            approval_request_id=review.id,
            mode=CommitMode.SHARE_ONLY,
            action_version=1,
            actor=KAIF,
            now=NOW,
            recipient_group_ids=[group.id],
        )

        unchanged = services["task_service"].get(task.id)
        assert unchanged.version == 1
        assert unchanged.status.value == "PENDING"

    def test_share_only_requires_at_least_one_recipient(self, db_session: Session) -> None:
        services = build(db_session)
        seed_user(db_session, KAIF)
        review = make_review(services)
        with pytest.raises(NoRecipientsSelectedError):
            services["sharing_service"].commit(
                approval_request_id=review.id,
                mode=CommitMode.SHARE_ONLY,
                action_version=1,
                actor=KAIF,
                now=NOW,
                recipient_group_ids=[],
            )


class TestScenario6DoubleClick:
    def test_committing_the_same_action_version_twice_is_a_no_op(self, db_session: Session) -> None:
        services = build(db_session)
        seed_user(db_session, KAIF)
        review = make_review(services)
        group = services["group_repo"].add(
            display_name="SI Team", external_jid="120363000000000004@g.us", now=NOW
        )

        first = services["sharing_service"].commit(
            approval_request_id=review.id,
            mode=CommitMode.SHARE_ONLY,
            action_version=7,
            actor=KAIF,
            now=NOW,
            recipient_group_ids=[group.id],
        )
        second = services["sharing_service"].commit(
            approval_request_id=review.id,
            mode=CommitMode.SHARE_ONLY,
            action_version=7,
            actor=KAIF,
            now=NOW,
            recipient_group_ids=[group.id],
        )

        assert not first.already_committed
        assert second.already_committed
        assert first.job.id == second.job.id
        assert len(first.recipients) == 1  # not duplicated by the second call

    def test_concurrent_commits_sharing_one_action_version_produce_exactly_one_job(
        self, migrated_engine: object
    ) -> None:
        """Exit criterion 2: 50 concurrent commits sharing one action_version
        must produce exactly one share job. Real threads, real separate
        transactions, real commits -- a single rolled-back session cannot
        exercise a genuine race.

        Uses its own engine with a pool wide enough for 50 simultaneous
        connections, rather than the shared migrated_engine fixture (default
        pool_size=5 + max_overflow=10 = 15). The first version of this test
        used a threading.Barrier sized to all 50 threads while sharing the
        default-pooled engine: only ~15 threads could ever acquire a
        connection and reach the barrier, so the other 35 sat forever waiting
        for a connection the pool could never hand out, and the 15 that had
        one sat forever waiting at the barrier for those 35. Not a database
        deadlock -- pg_stat_activity showed every connection idle, waiting on
        the client -- but a self-inflicted one in the test's own design.
        """
        stress_engine = make_engine(os.environ["TEST_DATABASE_URL"], pool_size=60, max_overflow=0)
        setup_conn = stress_engine.connect()
        try:
            setup_session = sessionmaker(bind=setup_conn, future=True)()
            services = build(setup_session)
            seed_user(setup_session, KAIF)
            review = make_review(services)
            group = services["group_repo"].add(
                display_name="SI Team", external_jid="120363000000000099@g.us", now=NOW
            )
            setup_session.commit()
            review_id, group_id = review.id, group.id
        finally:
            setup_conn.close()

        attempts = 50
        outcomes: list[str] = []
        errors: list[BaseException] = []
        barrier = threading.Barrier(attempts)

        def attempt() -> None:
            conn = stress_engine.connect()
            try:
                session = sessionmaker(bind=conn, future=True)()
                services = build(session)
                barrier.wait()
                try:
                    result = services["sharing_service"].commit(
                        approval_request_id=review_id,
                        mode=CommitMode.SHARE_ONLY,
                        action_version=1,
                        actor=KAIF,
                        now=NOW,
                        recipient_group_ids=[group_id],
                    )
                    session.commit()
                    outcomes.append("created" if not result.already_committed else "replayed")
                except Exception:
                    session.rollback()
                    raise
            except BaseException as exc:
                errors.append(exc)
            finally:
                conn.close()

        threads = [threading.Thread(target=attempt) for _ in range(attempts)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        try:
            assert not errors, f"{len(errors)} thread(s) raised: {errors[:3]}"
            assert len(outcomes) == attempts
            assert outcomes.count("created") == 1, (
                f"expected exactly one 'created', got {outcomes.count('created')}"
            )

            verify_conn = stress_engine.connect()
            try:
                verify_session = sessionmaker(bind=verify_conn, future=True)()
                from sqlalchemy import text

                job_count = verify_session.execute(
                    text(
                        "SELECT count(*) FROM share_jobs "
                        "WHERE approval_request_id = :rid AND action_version = 1"
                    ),
                    {"rid": review_id},
                ).scalar_one()
                assert job_count == 1

                recipient_count = verify_session.execute(
                    text(
                        "SELECT count(*) FROM share_recipients sr "
                        "JOIN share_jobs sj ON sj.id = sr.share_job_id "
                        "WHERE sj.approval_request_id = :rid"
                    ),
                    {"rid": review_id},
                ).scalar_one()
                assert recipient_count == 1  # not one per losing attempt

                # No orphaned scheduled_actions: every one of the 49 losers'
                # rows must have been cancelled, not left PENDING forever.
                orphaned = verify_session.execute(
                    text(
                        "SELECT count(*) FROM scheduled_actions sa "
                        "WHERE sa.kind = 'SEND_SHARE_JOB' AND sa.status = 'PENDING' "
                        "AND NOT EXISTS "
                        "(SELECT 1 FROM share_jobs sj WHERE sj.scheduled_action_id = sa.id)"
                    )
                ).scalar_one()
                assert orphaned == 0
            finally:
                verify_conn.close()
        finally:
            # Deletion order matters here and is exactly what bit this test
            # once already: report_snapshots also FK-references
            # approval_requests, and skipping it made the approval_requests
            # delete fail, which rolled back this entire cleanup block on
            # connection close -- silently leaking a real, permanently
            # committed, already-APPROVED review that then poisoned every
            # other test in this file defaulting to the same
            # (kind=EVENING, local_date) via make_review()'s idempotent
            # lookup. share_recipients -> share_jobs -> report_snapshots ->
            # approval_requests is the correct FK-respecting order.
            cleanup_conn = stress_engine.connect()
            try:
                from sqlalchemy import text

                cleanup_conn.execute(
                    text(
                        "DELETE FROM share_recipients WHERE share_job_id IN "
                        "(SELECT id FROM share_jobs WHERE approval_request_id = :rid)"
                    ),
                    {"rid": review_id},
                )
                cleanup_conn.execute(
                    text("DELETE FROM share_jobs WHERE approval_request_id = :rid"),
                    {"rid": review_id},
                )
                cleanup_conn.execute(
                    text("DELETE FROM report_snapshots WHERE approval_request_id = :rid"),
                    {"rid": review_id},
                )
                cleanup_conn.execute(
                    text("DELETE FROM approval_requests WHERE id = :rid"), {"rid": review_id}
                )
                cleanup_conn.execute(
                    text("DELETE FROM whatsapp_groups WHERE id = :gid"), {"gid": group_id}
                )
                cleanup_conn.commit()
            finally:
                cleanup_conn.close()
            stress_engine.dispose()


class TestScenario8MultiGroupPartialFailure:
    def test_each_recipient_is_independent(self, db_session: Session) -> None:
        services = build(db_session)
        seed_user(db_session, KAIF)
        review = make_review(services)
        si_team = services["group_repo"].add(
            display_name="SI Team", external_jid="120363000000000010@g.us", now=NOW
        )
        management = services["group_repo"].add(
            display_name="Management", external_jid="120363000000000011@g.us", now=NOW
        )

        result = services["sharing_service"].commit(
            approval_request_id=review.id,
            mode=CommitMode.SHARE_ONLY,
            action_version=1,
            actor=KAIF,
            now=NOW,
            recipient_group_ids=[si_team.id, management.id],
        )

        assert len(result.recipients) == 2
        assert {r.whatsapp_group_id for r in result.recipients} == {si_team.id, management.id}
        assert {r.client_message_id for r in result.recipients} == {
            r.client_message_id for r in result.recipients
        }  # each recipient has its own idempotency key
        client_ids = [r.client_message_id for r in result.recipients]
        assert len(set(client_ids)) == 2  # no shared/duplicate idempotency keys


class TestCommitRejectsClosedReviews:
    def test_cannot_commit_a_closed_review(self, db_session: Session) -> None:
        services = build(db_session)
        seed_user(db_session, KAIF)
        review = make_review(services)
        services["sharing_service"].commit(
            approval_request_id=review.id,
            mode=CommitMode.UPDATE_ONLY,
            action_version=1,
            actor=KAIF,
            now=NOW,
        )

        with pytest.raises(IllegalTransitionError):
            services["sharing_service"].commit(
                approval_request_id=review.id,
                mode=CommitMode.SHARE_ONLY,
                action_version=2,
                actor=KAIF,
                now=NOW,
                recipient_group_ids=["irrelevant"],
            )
