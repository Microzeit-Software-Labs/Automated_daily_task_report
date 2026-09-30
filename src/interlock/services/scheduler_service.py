"""The durable scheduler tick and delivery execution.

Callable directly from a test with no broker and no background process --
that is what "test scheduling without touching a message queue" means in
practice (see the Phase 0 deviation from Celery ETA tasks). The real worker
loop (``workers/loop.py``) is a thin ``while True: tick(); sleep(...)``
wrapped around exactly this.

One tick does three independent things, in order:

1. Sweep expired leases -- a worker that crashed mid-send left its claimed
   rows stuck; return them to PENDING so another worker can pick them up.
2. Claim due ``scheduled_actions`` (``SKIP LOCKED``) and dispatch each one:
   evaluate whether the frozen report is still valid to send unattended (see
   ``domain/sharing/validity.py``), then attempt every recipient once.
3. Scan for recipients whose retry delay has elapsed and attempt those --
   decoupled from the scheduled_actions claim, because a share_job's 1:1
   relationship to its scheduled_action means a retry cannot get a second one
   of its own.

The per-recipient compare-and-swap (``claim_recipient_for_sending``) is what
makes a redelivered or duplicated dispatch harmless: two workers racing on the
same recipient can both attempt the claim, but only one can win it.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from zoneinfo import ZoneInfo

from interlock.adapters.persistence.scheduler_repository import SchedulerRepository
from interlock.adapters.persistence.share_repository import ShareRepository
from interlock.adapters.persistence.whatsapp_group_repository import WhatsAppGroupRepository
from interlock.domain.approvals.machine import roll_up_job_state, transition
from interlock.domain.approvals.states import RecipientState, ShareJobState
from interlock.domain.common.actor import system_actor
from interlock.domain.common.errors import NotFoundError
from interlock.domain.ports.repositories import AuditSink
from interlock.domain.ports.whatsapp import ErrorClass, WhatsAppProvider
from interlock.domain.scheduling.action import SEND_SHARE_JOB, ScheduledAction
from interlock.domain.sharing.job import ShareJob, ShareRecipient
from interlock.domain.sharing.snapshot import ReportSnapshot
from interlock.domain.sharing.validity import (
    DeferReason,
    Verdict,
    evaluate_send_validity,
    missed_its_day,
)

WORKER_ACTOR = system_actor("worker")

# Kept in scheduled_actions.last_error when a due action is handed back
# because WhatsApp could not send. If that action is later judged too late,
# this is what lets the hold-back say "WhatsApp was down" instead of "stale".
WAITING_FOR_WHATSAPP = "waiting_for_whatsapp"


@dataclasses.dataclass(frozen=True, slots=True)
class TickResult:
    leases_swept: int = 0
    actions_claimed: int = 0
    retries_attempted: int = 0
    sent: int = 0
    failed_sends: int = 0


class SchedulerService:
    def __init__(
        self,
        *,
        scheduler_repo: SchedulerRepository,
        share_repo: ShareRepository,
        group_repo: WhatsAppGroupRepository,
        whatsapp: WhatsAppProvider,
        audit: AuditSink,
        tz: ZoneInfo,
        send_grace: dt.timedelta,
        retry_delays: tuple[int, ...],
        worker_id: str,
        claim_lease: dt.timedelta,
        claim_batch_size: int,
        enforce_day_boundary: bool = True,
        strict_data_drift: bool = False,
    ) -> None:
        self._scheduler = scheduler_repo
        self._shares = share_repo
        self._groups = group_repo
        self._whatsapp = whatsapp
        self._audit = audit
        self._tz = tz
        self._send_grace = send_grace
        self._retry_delays = retry_delays
        self._worker_id = worker_id
        self._claim_lease = claim_lease
        self._claim_batch_size = claim_batch_size
        self._enforce_day_boundary = enforce_day_boundary
        self._strict_data_drift = strict_data_drift

    def tick(self, *, now: dt.datetime) -> TickResult:
        swept = self._scheduler.sweep_expired_leases(now=now)

        claimed = self._scheduler.claim_due(
            now=now,
            worker_id=self._worker_id,
            lease=self._claim_lease,
            batch_size=self._claim_batch_size,
        )
        # Asked once per tick: while WhatsApp can't send, due reports wait
        # (re-judged every tick) instead of burning their retry attempts on
        # 20-second timeouts that cannot succeed.
        can_send = self._whatsapp.health().can_send

        sent = failed = 0
        for action in claimed:
            outcome = self._dispatch(action, now=now, can_send=can_send)
            sent += outcome[0]
            failed += outcome[1]

        due_retries = self._shares.list_due_retry_recipients(now=now) if can_send else []
        for recipient in due_retries:
            if self._enforce_day_boundary and self._retry_missed_its_day(recipient, now=now):
                failed += 1
                self._reroll_job(recipient.share_job_id, now=now)
                continue
            retry_outcome = self._attempt_recipient(recipient, now=now)
            if retry_outcome is True:
                sent += 1
            elif retry_outcome is False:
                failed += 1
            self._reroll_job(recipient.share_job_id, now=now)

        return TickResult(
            leases_swept=swept,
            actions_claimed=len(claimed),
            retries_attempted=len(due_retries),
            sent=sent,
            failed_sends=failed,
        )

    def retry_recipient(self, recipient_id: str, *, now: dt.datetime) -> ShareRecipient:
        """Scenario 8: retry only the group that failed, immediately, on
        explicit user request -- not the automatic retry ladder's job."""
        recipient = self._shares.get_recipient(recipient_id)
        if recipient is None:
            raise NotFoundError(
                f"Share recipient {recipient_id} does not exist.", recipient_id=recipient_id
            )
        if recipient.state is not RecipientState.FAILED:
            raise NotFoundError(  # reuse: "nothing here to retry" is a 404-shaped fact
                f"Recipient {recipient_id} is {recipient.state.value}, not FAILED.",
                recipient_id=recipient_id,
                state=recipient.state.value,
            )
        # The job itself must pass through SENDING before landing on whatever
        # the roll-up computes next: PARTIALLY_SENT -> SENT is not a direct
        # edge in the state machine (a job's terminal outcome is always
        # reached *from* an active send, never jumped to directly), and
        # FAILED -> SENDING -> {SENT, PARTIALLY_SENT} models a retry after
        # total failure the same way.
        job = self._shares.get_job(recipient.share_job_id)
        if job is not None and job.state is not ShareJobState.SENDING:
            self._transition_job(job, ShareJobState.SENDING, now=now, reason="manual retry")

        reset = self._shares.reset_for_manual_retry(recipient_id, now=now)
        self._attempt_recipient(reset, now=now)
        self._reroll_job(recipient.share_job_id, now=now)
        result = self._shares.get_recipient(recipient_id)
        if result is None:
            # Cannot happen under correct operation -- this row was written
            # moments ago in this same transaction. Surfaced as a proper
            # domain error rather than a bare assert, consistent with how
            # every other "this should be impossible" case in this codebase
            # is handled.
            raise NotFoundError(
                f"Share recipient {recipient_id} vanished during its own retry.",
                recipient_id=recipient_id,
            )
        return result

    # -- internals ------------------------------------------------------

    def _dispatch(
        self, action: ScheduledAction, *, now: dt.datetime, can_send: bool = True
    ) -> tuple[int, int]:
        if action.kind != SEND_SHARE_JOB:
            self._scheduler.mark_failed(
                action.id, error=f"unknown scheduled action kind: {action.kind}", now=now
            )
            return (0, 0)

        job = self._shares.get_job_by_scheduled_action_id(action.id)
        if job is None:
            # No share_job ever attached -- an orphan from a lost race at
            # commit time (see SharingService.commit). Nothing to send.
            self._scheduler.mark_done(action.id, now=now)
            return (0, 0)

        snapshot = self._shares.get_snapshot(job.snapshot_id)
        if snapshot is None:
            self._scheduler.mark_failed(action.id, error="snapshot missing", now=now)
            return (0, 0)

        latest = self._shares.latest_approved_job_for_review(job.approval_request_id)
        superseded_by = latest.id if (latest is not None and latest.id != job.id) else None

        decision = evaluate_send_validity(
            job_state=job.state,
            snapshot_created_at=snapshot.created_at,
            snapshot_content_hash=snapshot.content_hash,
            current_content_hash=None,  # strict_data_drift is off by default in Phase 1
            now=now,
            tz=self._tz,
            grace=self._send_grace,
            enforce_day_boundary=self._enforce_day_boundary,
            strict_data_drift=self._strict_data_drift,
            superseded_by=superseded_by,
            run_at=action.run_at,
        )
        if (
            decision.verdict is Verdict.DEFER
            and (not can_send or action.last_error == WAITING_FOR_WHATSAPP)
            and decision.reason in {DeferReason.CROSSED_DAY_BOUNDARY, DeferReason.SNAPSHOT_STALE}
        ):
            # Too late *because* WhatsApp was down when it came due -- say so.
            decision = dataclasses.replace(decision, reason=DeferReason.WHATSAPP_DISCONNECTED)

        if decision.verdict is Verdict.DROP:
            self._scheduler.mark_done(action.id, now=now)
            return (0, 0)

        if decision.verdict is Verdict.SUPERSEDE:
            self._transition_job(job, ShareJobState.SUPERSEDED, now=now, reason="superseded")
            self._scheduler.mark_done(action.id, now=now)
            return (0, 0)

        if decision.verdict is Verdict.DEFER:
            record = transition(
                job.state,
                ShareJobState.DEFERRED,
                entity_type="share_job",
                entity_id=job.id,
                actor=WORKER_ACTOR,
                at=now,
                reason=decision.detail,
            )
            self._audit.record(
                action="SHARE_JOB_DEFERRED",
                entity_type="share_job",
                entity_id=job.id,
                actor=record.actor,
                actor_kind=record.actor_kind,
                at=now,
                after={"reason": decision.reason.value if decision.reason else None},
            )
            self._shares.save_job_state(
                job.id,
                state=ShareJobState.DEFERRED,
                deferred_at=now,
                deferred_reason=decision.reason,
                now=now,
            )
            self._scheduler.mark_done(action.id, now=now)
            return (0, 0)

        if not can_send:
            # Still valid, but WhatsApp is down: hand the action back
            # untouched -- same run_at, so the grace window keeps counting
            # from the intended time -- and judge it again next tick. It goes
            # out as soon as WhatsApp reconnects, or is held back once too late.
            self._scheduler.reschedule(
                action.id, run_at=action.run_at, now=now, note=WAITING_FOR_WHATSAPP
            )
            return (0, 0)

        # SEND
        self._transition_job(job, ShareJobState.SENDING, now=now, reason="dispatching")

        recipients = self._shares.list_recipients_for_job(job.id)
        sent = failed = 0
        for recipient in recipients:
            outcome = self._attempt_recipient(
                recipient, now=now, snapshot=snapshot
            )
            if outcome is True:
                sent += 1
            elif outcome is False:
                failed += 1

        self._reroll_job(job.id, now=now)
        self._scheduler.mark_done(action.id, now=now)
        return (sent, failed)

    def _attempt_recipient(
        self,
        recipient: ShareRecipient,
        *,
        now: dt.datetime,
        snapshot: ReportSnapshot | None = None,
    ) -> bool | None:
        """Returns True (sent), False (failed permanently or exhausted
        retries), or None (claim lost / nothing to do -- not this caller's
        outcome to report)."""
        won = self._shares.claim_recipient_for_sending(recipient.id, now=now)
        if not won:
            return None

        group = self._groups.get(recipient.whatsapp_group_id)
        if group is None or not group.enabled:
            self._shares.save_recipient_outcome(
                recipient.id,
                state=RecipientState.SKIPPED,
                now=now,
                error_detail="group disabled or removed between approval and send",
            )
            self._audit.record(
                action="RECIPIENT_SKIPPED",
                entity_type="share_recipient",
                entity_id=recipient.id,
                actor=str(WORKER_ACTOR),
                actor_kind=WORKER_ACTOR.kind.value,
                at=now,
            )
            return None

        if snapshot is None:
            job = self._shares.get_job(recipient.share_job_id)
            snapshot = self._shares.get_snapshot(job.snapshot_id) if job else None

        # The frozen bytes, text and image alike -- never a fresh render.
        outcome = self._whatsapp.send_text(
            external_jid=group.external_jid,
            body=snapshot.rendered_body if snapshot else "",
            image_png=snapshot.rendered_image if snapshot else None,
            client_message_id=recipient.client_message_id,
        )

        if outcome.accepted:
            self._shares.save_recipient_outcome(
                recipient.id,
                state=RecipientState.SENT,
                now=now,
                provider_message_id=outcome.provider_message_id,
                ack_level=outcome.ack.value,
                sent_at=now,
            )
            self._groups.mark_used(group.id, at=now)
            self._audit.record(
                action="RECIPIENT_SENT",
                entity_type="share_recipient",
                entity_id=recipient.id,
                actor=str(WORKER_ACTOR),
                actor_kind=WORKER_ACTOR.kind.value,
                at=now,
                after={
                    "group": group.display_name,
                    "provider_message_id": outcome.provider_message_id,
                },
            )
            return True

        attempts = recipient.attempts + 1
        if outcome.error_class is ErrorClass.TRANSIENT and attempts < len(self._retry_delays):
            delay_seconds = self._retry_delays[attempts]
            self._shares.save_recipient_outcome(
                recipient.id,
                state=RecipientState.QUEUED,
                now=now,
                attempts=attempts,
                next_attempt_at=now + dt.timedelta(seconds=delay_seconds),
                error_code=outcome.error_code,
                error_detail=outcome.error_detail,
            )
            self._audit.record(
                action="RECIPIENT_RETRY_SCHEDULED",
                entity_type="share_recipient",
                entity_id=recipient.id,
                actor=str(WORKER_ACTOR),
                actor_kind=WORKER_ACTOR.kind.value,
                at=now,
                after={
                    "attempt": attempts,
                    "delay_seconds": delay_seconds,
                    "error": outcome.error_code,
                },
            )
            return None

        self._shares.save_recipient_outcome(
            recipient.id,
            state=RecipientState.FAILED,
            now=now,
            attempts=attempts,
            error_code=outcome.error_code,
            error_detail=outcome.error_detail,
        )
        self._audit.record(
            action="RECIPIENT_FAILED",
            entity_type="share_recipient",
            entity_id=recipient.id,
            actor=str(WORKER_ACTOR),
            actor_kind=WORKER_ACTOR.kind.value,
            at=now,
            after={"error": outcome.error_code, "detail": outcome.error_detail},
        )
        return False

    def _retry_missed_its_day(self, recipient: ShareRecipient, *, now: dt.datetime) -> bool:
        """Fail an automatic retry whose report's day has ended, rather than
        deliver yesterday's report after a long outage. True if it did."""
        job = self._shares.get_job(recipient.share_job_id)
        snapshot = self._shares.get_snapshot(job.snapshot_id) if job else None
        if job is None or snapshot is None:
            return False
        action = self._scheduler.get(job.scheduled_action_id)
        if not missed_its_day(
            snapshot_created_at=snapshot.created_at,
            run_at=action.run_at if action else None,
            now=now,
            tz=self._tz,
        ):
            return False

        # QUEUED -> SENDING -> FAILED, the same legal path a real attempt
        # takes (QUEUED -> FAILED is not an edge). Losing the claim means
        # another worker has it; either way this one must not send it.
        if not self._shares.claim_recipient_for_sending(recipient.id, now=now):
            return True
        detail = "Not sent: its day ended before WhatsApp could deliver it."
        self._shares.save_recipient_outcome(
            recipient.id,
            state=RecipientState.FAILED,
            now=now,
            attempts=recipient.attempts,
            error_code="MISSED_DAY",
            error_detail=detail,
        )
        self._audit.record(
            action="RECIPIENT_FAILED",
            entity_type="share_recipient",
            entity_id=recipient.id,
            actor=str(WORKER_ACTOR),
            actor_kind=WORKER_ACTOR.kind.value,
            at=now,
            after={"error": "MISSED_DAY", "detail": detail},
        )
        return True

    def _reroll_job(self, job_id: str, *, now: dt.datetime) -> None:
        job = self._shares.get_job(job_id)
        if job is None:
            return
        recipients = self._shares.list_recipients_for_job(job_id)
        rolled = roll_up_job_state([r.state for r in recipients])
        if rolled is job.state:
            return
        sent_at = now if rolled in {ShareJobState.SENT, ShareJobState.PARTIALLY_SENT} else None
        self._transition_job(
            job, rolled, now=now, reason="recipient outcomes rolled up", sent_at=sent_at
        )

    def _transition_job(
        self,
        job: ShareJob,
        target: ShareJobState,
        *,
        now: dt.datetime,
        reason: str,
        sent_at: dt.datetime | None = None,
    ) -> None:
        record = transition(
            job.state,
            target,
            entity_type="share_job",
            entity_id=job.id,
            actor=WORKER_ACTOR,
            at=now,
            reason=reason,
        )
        self._audit.record(
            action="SHARE_JOB_STATE_CHANGED",
            entity_type="share_job",
            entity_id=job.id,
            actor=record.actor,
            actor_kind=record.actor_kind,
            at=now,
            before={"state": record.from_state},
            after={"state": record.to_state},
        )
        self._shares.save_job_state(job.id, state=target, now=now, sent_at=sent_at)
