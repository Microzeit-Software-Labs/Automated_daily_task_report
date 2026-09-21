"""SchedulerService against a real database and the mock WhatsApp provider.

Covers exit criteria 3 and 4 (worker-restart survival, stale-vs-on-time
dispatch) and acceptance Scenarios 4, 5, 8 and 9 at the service layer.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from interlock.adapters.persistence.approval_repository import ApprovalRepository
from interlock.adapters.persistence.audit_sink import PostgresAuditSink
from interlock.adapters.persistence.models import UserRow
from interlock.adapters.persistence.scheduler_repository import SchedulerRepository
from interlock.adapters.persistence.sequences import PostgresSequences
from interlock.adapters.persistence.share_repository import ShareRepository
from interlock.adapters.persistence.task_repository import PostgresTaskRepository
from interlock.adapters.persistence.whatsapp_group_repository import WhatsAppGroupRepository
from interlock.adapters.whatsapp.mock import MockWhatsAppProvider
from interlock.domain.approvals.request import ApprovalKind
from interlock.domain.approvals.states import RecipientState, ShareJobState
from interlock.domain.common.actor import Actor, ActorKind
from interlock.domain.common.clock import FrozenClock
from interlock.domain.ports.whatsapp import ConnectionState, ErrorClass
from interlock.services.review_service import ReviewService
from interlock.services.scheduler_service import SchedulerService
from interlock.services.sharing_service import CommitMode, SharingService
from interlock.services.task_service import TaskService

TZ = ZoneInfo("Asia/Kolkata")
T0 = dt.datetime(2026, 9, 15, 17, 4, tzinfo=dt.UTC)
KAIF = Actor(kind=ActorKind.USER, id="u-1", label="kaif")
RETRY_DELAYS = (0, 30, 120, 600)


def build(session: Session, *, at: dt.datetime, provider: MockWhatsAppProvider | None = None):
    if session.get(UserRow, KAIF.id) is None:
        session.add(
            UserRow(
                id=KAIF.id,
                email="kaif@example.com",
                display_name="kaif",
                role="approver",
                is_active=True,
                created_at=at,
            )
        )
        session.flush()

    clock = FrozenClock(at)
    task_repo = PostgresTaskRepository(session)
    audit = PostgresAuditSink(session)
    approval_repo = ApprovalRepository(session)
    share_repo = ShareRepository(session)
    scheduler_repo = SchedulerRepository(session)
    group_repo = WhatsAppGroupRepository(session)
    sequences = PostgresSequences(session)
    whatsapp = provider or MockWhatsAppProvider(clock)

    task_service = TaskService(task_repo, audit, clock)
    review_service = ReviewService(approval_repo, task_repo, audit, TZ)
    sharing_service = SharingService(
        approval_repo=approval_repo,
        share_repo=share_repo,
        scheduler_repo=scheduler_repo,
        sequences=sequences,
        task_service=task_service,
        task_repo=task_repo,
        audit=audit,
        tz=TZ,
        max_message_length=4096,
    )
    scheduler_service = SchedulerService(
        scheduler_repo=scheduler_repo,
        share_repo=share_repo,
        group_repo=group_repo,
        whatsapp=whatsapp,
        audit=audit,
        tz=TZ,
        send_grace=dt.timedelta(minutes=60),
        retry_delays=RETRY_DELAYS,
        worker_id="test-worker",
        claim_lease=dt.timedelta(minutes=2),
        claim_batch_size=50,
    )
    return {
        "task_service": task_service,
        "review_service": review_service,
        "sharing_service": sharing_service,
        "scheduler_service": scheduler_service,
        "scheduler_repo": scheduler_repo,
        "share_repo": share_repo,
        "group_repo": group_repo,
        "whatsapp": whatsapp,
    }


def make_review(services: dict, *, at: dt.datetime):
    request, _ = services["review_service"].create_scheduled_review(
        ApprovalKind.EVENING, scheduled_for=at, now=at
    )
    return request


class TestImmediateSend:
    def test_send_now_job_is_claimed_and_delivered(self, db_session: Session) -> None:
        services = build(db_session, at=T0)
        review = make_review(services, at=T0)
        group = services["group_repo"].add(display_name="SI Team", external_jid="si@g.us", now=T0)

        result = services["sharing_service"].commit(
            approval_request_id=review.id,
            mode=CommitMode.SHARE_ONLY,
            action_version=1,
            actor=KAIF,
            now=T0,
            recipient_group_ids=[group.id],
        )
        recipient_id = result.recipients[0].id

        tick = services["scheduler_service"].tick(now=T0 + dt.timedelta(seconds=1))

        assert tick.actions_claimed == 1
        assert tick.sent == 1
        recipient = services["share_repo"].get_recipient(recipient_id)
        assert recipient.state is RecipientState.SENT
        job = services["share_repo"].get_job(result.job.id)
        assert job.state is ShareJobState.SENT
        assert services["whatsapp"].delivered_count("si@g.us") == 1


class TestScenario4FiveMinuteSchedule:
    def test_future_job_is_not_claimed_before_its_time(self, db_session: Session) -> None:
        services = build(db_session, at=T0)
        review = make_review(services, at=T0)
        group = services["group_repo"].add(display_name="SI Team", external_jid="si@g.us", now=T0)
        send_at = T0 + dt.timedelta(minutes=5)

        services["sharing_service"].commit(
            approval_request_id=review.id,
            mode=CommitMode.SHARE_ONLY,
            action_version=1,
            actor=KAIF,
            now=T0,
            recipient_group_ids=[group.id],
            send_at=send_at,
        )

        too_early = services["scheduler_service"].tick(now=T0 + dt.timedelta(minutes=2))
        assert too_early.actions_claimed == 0
        assert services["whatsapp"].delivered_count("si@g.us") == 0

        on_time = services["scheduler_service"].tick(now=send_at + dt.timedelta(seconds=1))
        assert on_time.actions_claimed == 1
        assert on_time.sent == 1
        assert services["whatsapp"].delivered_count("si@g.us") == 1


class TestMissedWindowRule:
    def test_a_report_stale_beyond_grace_defers_instead_of_sending(
        self, db_session: Session
    ) -> None:
        """The laptop-asleep-all-night case: never silently send a report
        hours late, and never silently drop it either."""
        services = build(db_session, at=T0)
        review = make_review(services, at=T0)
        group = services["group_repo"].add(display_name="SI Team", external_jid="si@g.us", now=T0)
        result = services["sharing_service"].commit(
            approval_request_id=review.id,
            mode=CommitMode.SHARE_ONLY,
            action_version=1,
            actor=KAIF,
            now=T0,
            recipient_group_ids=[group.id],
        )

        way_too_late = T0 + dt.timedelta(hours=2)  # grace is 60 minutes
        tick = services["scheduler_service"].tick(now=way_too_late)

        assert tick.actions_claimed == 1
        assert tick.sent == 0
        job = services["share_repo"].get_job(result.job.id)
        assert job.state is ShareJobState.DEFERRED
        assert job.deferred_reason is not None
        # Never auto-sends after deferring -- recipients are untouched.
        recipient = services["share_repo"].get_recipient(result.recipients[0].id)
        assert recipient.state is RecipientState.QUEUED
        assert services["whatsapp"].delivered_count("si@g.us") == 0


class TestExitCriterion3WorkerRestart:
    def test_a_crashed_claim_is_recovered_with_exactly_one_delivery(
        self, db_session: Session
    ) -> None:
        """Simulates a worker dying mid-send: claim the action (as tick()
        would), but never finish processing it -- then let the lease expire
        and confirm a fresh tick recovers it and sends exactly once."""
        services = build(db_session, at=T0)
        review = make_review(services, at=T0)
        group = services["group_repo"].add(display_name="SI Team", external_jid="si@g.us", now=T0)
        result = services["sharing_service"].commit(
            approval_request_id=review.id,
            mode=CommitMode.SHARE_ONLY,
            action_version=1,
            actor=KAIF,
            now=T0,
            recipient_group_ids=[group.id],
        )

        # Manually claim, simulating tick() having started but the process
        # dying before it could dispatch -- the row sits CLAIMED with a lease.
        claimed = services["scheduler_repo"].claim_due(
            now=T0 + dt.timedelta(seconds=1),
            worker_id="dead-worker",
            lease=dt.timedelta(minutes=2),
            batch_size=10,
        )
        assert len(claimed) == 1
        assert services["whatsapp"].delivered_count("si@g.us") == 0  # nothing sent yet

        # Before the lease expires, a second tick must not double-claim it.
        too_soon = services["scheduler_service"].tick(now=T0 + dt.timedelta(seconds=30))
        assert too_soon.actions_claimed == 0

        # After the lease expires, the sweep recovers it and this tick sends.
        past_lease = T0 + dt.timedelta(minutes=3)
        recovered = services["scheduler_service"].tick(now=past_lease)
        assert recovered.leases_swept == 1
        assert recovered.actions_claimed == 1
        assert recovered.sent == 1

        assert services["whatsapp"].delivered_count("si@g.us") == 1  # exactly once
        recipient = services["share_repo"].get_recipient(result.recipients[0].id)
        assert recipient.state is RecipientState.SENT


class TestScenario8MultiGroupAndManualRetry:
    def test_partial_failure_then_retry_only_the_failed_group(self, db_session: Session) -> None:
        services = build(db_session, at=T0)
        whatsapp: MockWhatsAppProvider = services["whatsapp"]
        review = make_review(services, at=T0)
        si_team = services["group_repo"].add(display_name="SI Team", external_jid="si@g.us", now=T0)
        management = services["group_repo"].add(
            display_name="Management", external_jid="mgmt@g.us", now=T0
        )
        ai_eng = services["group_repo"].add(
            display_name="AI Engineering", external_jid="ai@g.us", now=T0
        )
        whatsapp.given_failure("ai@g.us", code="GROUP_NOT_FOUND", error_class=ErrorClass.PERMANENT)

        result = services["sharing_service"].commit(
            approval_request_id=review.id,
            mode=CommitMode.SHARE_ONLY,
            action_version=1,
            actor=KAIF,
            now=T0,
            recipient_group_ids=[si_team.id, management.id, ai_eng.id],
        )

        tick = services["scheduler_service"].tick(now=T0 + dt.timedelta(seconds=1))
        assert tick.sent == 2
        assert tick.failed_sends == 1

        job = services["share_repo"].get_job(result.job.id)
        assert job.state is ShareJobState.PARTIALLY_SENT

        recipients = {
            r.whatsapp_group_id: r for r in services["share_repo"].list_recipients_for_job(job.id)
        }
        assert recipients[si_team.id].state is RecipientState.SENT
        assert recipients[management.id].state is RecipientState.SENT
        failed_recipient = recipients[ai_eng.id]
        assert failed_recipient.state is RecipientState.FAILED

        # SI Team's and Management's success must not be touched by retrying
        # only the failed one.
        whatsapp.clear_failures()
        services["scheduler_service"].retry_recipient(
            failed_recipient.id, now=T0 + dt.timedelta(minutes=1)
        )

        final = {
            r.whatsapp_group_id: r for r in services["share_repo"].list_recipients_for_job(job.id)
        }
        assert final[si_team.id].state is RecipientState.SENT
        assert final[management.id].state is RecipientState.SENT
        assert final[ai_eng.id].state is RecipientState.SENT
        assert whatsapp.delivered_count("si@g.us") == 1  # not re-sent
        assert whatsapp.delivered_count("mgmt@g.us") == 1  # not re-sent
        assert whatsapp.delivered_count("ai@g.us") == 1  # the retry landed

        final_job = services["share_repo"].get_job(job.id)
        assert final_job.state is ShareJobState.SENT


class TestScenario9WhatsAppDown:
    def test_whatsapp_unavailable_does_not_lose_or_corrupt_task_data(
        self, db_session: Session
    ) -> None:
        services = build(db_session, at=T0)
        whatsapp: MockWhatsAppProvider = services["whatsapp"]
        whatsapp.given_connection(ConnectionState.UNAVAILABLE)

        task = services["task_service"].create(title="Deploy production server", actor=KAIF)
        updated = services["task_service"].update(
            task.id, {"remarks": "done despite WhatsApp being down"}, expected_version=1, actor=KAIF
        )
        assert updated.remarks == "done despite WhatsApp being down"

        review = make_review(services, at=T0)
        group = services["group_repo"].add(display_name="SI Team", external_jid="si@g.us", now=T0)
        result = services["sharing_service"].commit(
            approval_request_id=review.id,
            mode=CommitMode.SHARE_ONLY,
            action_version=1,
            actor=KAIF,
            now=T0,
            recipient_group_ids=[group.id],
        )

        tick = services["scheduler_service"].tick(now=T0 + dt.timedelta(seconds=1))
        assert tick.sent == 0

        recipient = services["share_repo"].get_recipient(result.recipients[0].id)
        # Provider-down is transient -- queued for retry, never a permanent
        # failure, and the task edit above is completely unaffected.
        assert recipient.state is RecipientState.QUEUED
        assert services["task_service"].get(task.id).remarks == "done despite WhatsApp being down"

    def test_retry_succeeds_once_whatsapp_recovers(self, db_session: Session) -> None:
        services = build(db_session, at=T0)
        whatsapp: MockWhatsAppProvider = services["whatsapp"]
        whatsapp.given_connection(ConnectionState.UNAVAILABLE)

        review = make_review(services, at=T0)
        group = services["group_repo"].add(display_name="SI Team", external_jid="si@g.us", now=T0)
        services["sharing_service"].commit(
            approval_request_id=review.id,
            mode=CommitMode.SHARE_ONLY,
            action_version=1,
            actor=KAIF,
            now=T0,
            recipient_group_ids=[group.id],
        )

        first = services["scheduler_service"].tick(now=T0 + dt.timedelta(seconds=1))
        assert first.sent == 0

        whatsapp.given_connection(ConnectionState.CONNECTED)
        # First retry delay is 30s (RETRY_DELAYS[1]).
        recovered = services["scheduler_service"].tick(now=T0 + dt.timedelta(seconds=31))
        assert recovered.sent == 1
        assert whatsapp.delivered_count("si@g.us") == 1


class TestRetryLadder:
    def test_transient_failures_retry_with_increasing_delay_then_succeed(
        self, db_session: Session
    ) -> None:
        services = build(db_session, at=T0)
        whatsapp: MockWhatsAppProvider = services["whatsapp"]
        review = make_review(services, at=T0)
        group = services["group_repo"].add(display_name="SI Team", external_jid="si@g.us", now=T0)
        whatsapp.given_failure("si@g.us", error_class=ErrorClass.TRANSIENT, times=2)

        result = services["sharing_service"].commit(
            approval_request_id=review.id,
            mode=CommitMode.SHARE_ONLY,
            action_version=1,
            actor=KAIF,
            now=T0,
            recipient_group_ids=[group.id],
        )
        recipient_id = result.recipients[0].id

        # Attempt 1 fails -> queued with a 30s delay (RETRY_DELAYS[1]).
        services["scheduler_service"].tick(now=T0 + dt.timedelta(seconds=1))
        r1 = services["share_repo"].get_recipient(recipient_id)
        assert r1.state is RecipientState.QUEUED
        assert r1.attempts == 1

        # Too soon: the retry scan must not fire before next_attempt_at.
        services["scheduler_service"].tick(now=T0 + dt.timedelta(seconds=15))
        r1b = services["share_repo"].get_recipient(recipient_id)
        assert r1b.attempts == 1

        # Attempt 2 fails -> queued with a 120s delay (RETRY_DELAYS[2]).
        services["scheduler_service"].tick(now=T0 + dt.timedelta(seconds=31))
        r2 = services["share_repo"].get_recipient(recipient_id)
        assert r2.state is RecipientState.QUEUED
        assert r2.attempts == 2

        # Attempt 3 succeeds (given_failure's `times=2` is exhausted).
        services["scheduler_service"].tick(now=T0 + dt.timedelta(seconds=31 + 121))
        r3 = services["share_repo"].get_recipient(recipient_id)
        assert r3.state is RecipientState.SENT
        assert whatsapp.delivered_count("si@g.us") == 1
