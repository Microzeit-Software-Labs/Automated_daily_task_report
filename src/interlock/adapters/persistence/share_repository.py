"""PostgreSQL-backed snapshots, share jobs, and share recipients.

The recipient CAS (``claim_recipient_for_sending``) is the delivery-side
counterpart to the task repository's optimistic-concurrency update: a
conditional ``UPDATE ... WHERE state = 'QUEUED'`` rather than a
fetch-then-write, so that if a redelivered worker message and a legitimate
retry ever raced on the same recipient, at most one of them can win the claim.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, cast

from sqlalchemy import CursorResult, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from interlock.adapters.persistence.models import (
    ReportSnapshotRow,
    ShareJobRow,
    ShareRecipientRow,
)
from interlock.domain.approvals.states import RecipientState, ShareJobState
from interlock.domain.common.errors import NotFoundError
from interlock.domain.common.ids import SHARE_PREFIX, format_display_id, new_id
from interlock.domain.sharing.job import ShareJob, ShareRecipient
from interlock.domain.sharing.snapshot import ReportSnapshot
from interlock.domain.sharing.validity import DeferReason


class ShareRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    # -- snapshots ------------------------------------------------------

    def save_snapshot(self, snapshot: ReportSnapshot, *, approval_request_id: str) -> None:
        self._session.add(
            ReportSnapshotRow(
                id=snapshot.id,
                approval_request_id=approval_request_id,
                dataset_version=snapshot.dataset_version,
                content_hash=snapshot.content_hash,
                rendered_body=snapshot.rendered_body,
                task_state=list(snapshot.task_state),
                summary=dict(snapshot.summary),
                template_id=snapshot.template_id,
                created_at=snapshot.created_at,
            )
        )
        self._session.flush()

    def get_snapshot(self, snapshot_id: str) -> ReportSnapshot | None:
        row = self._session.get(ReportSnapshotRow, snapshot_id)
        return None if row is None else _snapshot_to_entity(row)

    # -- share jobs -------------------------------------------------------

    def create_job(
        self,
        *,
        sequence: int,
        approval_request_id: str,
        snapshot_id: str,
        approved_by_user_id: str,
        scheduled_action_id: str,
        action_version: int,
        now: dt.datetime,
    ) -> tuple[ShareJob, bool]:
        """Create the job for ``(approval_request_id, action_version)``, or
        return the one that already exists.

        Mirrors ``ApprovalRepository.create_scheduled``: this is what makes
        Scenario 6 hold under genuine concurrency, not just sequential
        retries. Fifty callers can race here with the same action_version --
        the ``uq_share_job_action_version`` constraint lets exactly one INSERT
        succeed, and every other caller's IntegrityError is caught inside a
        SAVEPOINT (so it does not abort the caller's whole transaction) and
        resolved by fetching the row that won. Returns ``(job, created)``, so
        a caller that also created a scheduled_action for this attempt knows
        to cancel it when it loses the race, rather than leaving an orphaned
        row for the scheduler to puzzle over later.

        ``sequence`` is supplied by the caller (one ``next("share")`` call)
        rather than drawn internally, so the same ordinal can label both this
        job's display id and its snapshot's dataset-version string --
        SHR-00012 pairs with dataset version "...-v12" because they are
        literally the same number, not two independent counters that happen
        to often agree.
        """
        row = ShareJobRow(
            id=new_id(),
            display_id=format_display_id(SHARE_PREFIX, sequence),
            approval_request_id=approval_request_id,
            snapshot_id=snapshot_id,
            approved_by_user_id=approved_by_user_id,
            scheduled_action_id=scheduled_action_id,
            action_version=action_version,
            state=ShareJobState.PENDING.value,
            created_at=now,
            updated_at=now,
        )
        savepoint = self._session.begin_nested()
        try:
            self._session.add(row)
            self._session.flush()
            savepoint.commit()
            return _job_to_entity(row), True
        except IntegrityError:
            savepoint.rollback()
            existing = self.get_job_by_action_version(approval_request_id, action_version)
            if existing is None:
                raise
            return existing, False

    def get_job(self, job_id: str) -> ShareJob | None:
        row = self._session.get(ShareJobRow, job_id)
        return None if row is None else _job_to_entity(row)

    def get_job_by_scheduled_action_id(self, scheduled_action_id: str) -> ShareJob | None:
        row = self._session.execute(
            select(ShareJobRow).where(ShareJobRow.scheduled_action_id == scheduled_action_id)
        ).scalar_one_or_none()
        return None if row is None else _job_to_entity(row)

    def get_job_by_action_version(
        self, approval_request_id: str, action_version: int
    ) -> ShareJob | None:
        """Used to detect an already-committed duplicate: if a caller retries
        the same (approval_request_id, action_version) after the database's
        own UNIQUE constraint rejects the second INSERT, this is how the
        service recovers the row that already exists rather than erroring."""
        row = self._session.execute(
            select(ShareJobRow).where(
                ShareJobRow.approval_request_id == approval_request_id,
                ShareJobRow.action_version == action_version,
            )
        ).scalar_one_or_none()
        return None if row is None else _job_to_entity(row)

    def get_job_by_display_id(self, display_id: str) -> ShareJob | None:
        row = self._session.execute(
            select(ShareJobRow).where(ShareJobRow.display_id == display_id)
        ).scalar_one_or_none()
        return None if row is None else _job_to_entity(row)

    def latest_approved_job_for_review(self, approval_request_id: str) -> ShareJob | None:
        """The highest ``action_version`` job for this review, if any --
        used to detect that a job coming due has been superseded by a newer
        approval for the same review."""
        row = self._session.execute(
            select(ShareJobRow)
            .where(ShareJobRow.approval_request_id == approval_request_id)
            .order_by(ShareJobRow.action_version.desc())
            .limit(1)
        ).scalar_one_or_none()
        return None if row is None else _job_to_entity(row)

    def save_job_state(
        self,
        job_id: str,
        *,
        state: ShareJobState,
        deferred_at: dt.datetime | None = None,
        deferred_reason: DeferReason | None = None,
        sent_at: dt.datetime | None = None,
        now: dt.datetime,
    ) -> ShareJob:
        row = self._session.get(ShareJobRow, job_id)
        if row is None:
            raise NotFoundError(f"Share job {job_id} does not exist.", job_id=job_id)
        row.state = state.value
        row.updated_at = now
        if deferred_at is not None:
            row.deferred_at = deferred_at
        if deferred_reason is not None:
            row.deferred_reason = deferred_reason.value
        if sent_at is not None:
            row.sent_at = sent_at
        self._session.flush()
        return _job_to_entity(row)

    # -- recipients -------------------------------------------------------

    def create_recipient(
        self, *, share_job_id: str, whatsapp_group_id: str, now: dt.datetime
    ) -> ShareRecipient:
        row = ShareRecipientRow(
            id=new_id(),
            share_job_id=share_job_id,
            whatsapp_group_id=whatsapp_group_id,
            client_message_id=new_id(),
            state=RecipientState.QUEUED.value,
            attempts=0,
            error_detail="",
            created_at=now,
            updated_at=now,
        )
        self._session.add(row)
        self._session.flush()
        return _recipient_to_entity(row)

    def get_recipient(self, recipient_id: str) -> ShareRecipient | None:
        row = self._session.get(ShareRecipientRow, recipient_id)
        return None if row is None else _recipient_to_entity(row)

    def list_recipients_for_job(self, job_id: str) -> list[ShareRecipient]:
        rows = self._session.execute(
            select(ShareRecipientRow).where(ShareRecipientRow.share_job_id == job_id)
        ).scalars().all()
        return [_recipient_to_entity(row) for row in rows]

    def list_due_retry_recipients(
        self, *, now: dt.datetime, limit: int = 200
    ) -> list[ShareRecipient]:
        """QUEUED recipients whose ``next_attempt_at`` has passed.

        A recipient's *first* attempt happens inline when its job's
        scheduled_action comes due; this is what makes a *retry* happen on a
        later tick. share_jobs.scheduled_action_id is a strict 1:1 -- a job
        cannot be attached to a second scheduled_action for its retry -- so
        retries are found by scanning share_recipients directly, decoupled
        from the scheduled_actions claim entirely. Safe without SKIP LOCKED:
        the actual mutual exclusion happens at claim_recipient_for_sending's
        CAS, not at this SELECT, so two workers reading the same due row here
        simply race harmlessly at the claim step instead.
        """
        rows = (
            self._session.execute(
                select(ShareRecipientRow)
                .where(
                    ShareRecipientRow.state == RecipientState.QUEUED.value,
                    ShareRecipientRow.next_attempt_at.is_not(None),
                    ShareRecipientRow.next_attempt_at <= now,
                )
                .order_by(ShareRecipientRow.next_attempt_at)
                .limit(limit)
            )
            .scalars()
            .all()
        )
        return [_recipient_to_entity(row) for row in rows]

    def claim_recipient_for_sending(self, recipient_id: str, *, now: dt.datetime) -> bool:
        """Atomically move one recipient from QUEUED to SENDING.

        Returns ``True`` if this call won the claim, ``False`` if the
        recipient was not in QUEUED state (already claimed by another worker,
        already sent, or skipped). This is the primitive that makes a
        redelivered or duplicated worker message harmless: only one caller can
        ever observe ``True`` for a given recipient row.
        """
        # Cast to CursorResult: see the identical note in task_repository.py's
        # update() -- an UPDATE has a real .rowcount at runtime that the
        # generic Result[Any] stub does not declare.
        result = cast(
            "CursorResult[Any]",
            self._session.execute(
                update(ShareRecipientRow)
                .where(
                    ShareRecipientRow.id == recipient_id,
                    ShareRecipientRow.state == RecipientState.QUEUED.value,
                )
                .values(state=RecipientState.SENDING.value, updated_at=now)
            ),
        )
        self._session.flush()
        return result.rowcount > 0

    def save_recipient_outcome(
        self,
        recipient_id: str,
        *,
        state: RecipientState,
        now: dt.datetime,
        attempts: int | None = None,
        next_attempt_at: dt.datetime | None = None,
        provider_message_id: str | None = None,
        ack_level: str | None = None,
        error_code: str | None = None,
        error_detail: str | None = None,
        sent_at: dt.datetime | None = None,
    ) -> ShareRecipient:
        row = self._session.get(ShareRecipientRow, recipient_id)
        if row is None:
            raise NotFoundError(
                f"Share recipient {recipient_id} does not exist.", recipient_id=recipient_id
            )
        row.state = state.value
        row.updated_at = now
        if attempts is not None:
            row.attempts = attempts
        row.next_attempt_at = next_attempt_at
        if provider_message_id is not None:
            row.provider_message_id = provider_message_id
        if ack_level is not None:
            row.ack_level = ack_level
        row.error_code = error_code
        if error_detail is not None:
            row.error_detail = error_detail
        if sent_at is not None:
            row.sent_at = sent_at
        self._session.flush()
        return _recipient_to_entity(row)

    def requeue_recipient(
        self, recipient_id: str, *, next_attempt_at: dt.datetime, now: dt.datetime
    ) -> ShareRecipient:
        """FAILED -> QUEUED, for a transient failure the retry ladder will
        pick up again on the scheduler's next relevant tick.

        Keeps the same client_message_id deliberately: this is the *same*
        logical send attempt being reattempted after a transient blip, and
        reusing the id is what lets a correctly-idempotent provider recognise
        "did this actually go through despite the apparent failure" for the
        ambiguous-timeout case. Contrast with reset_for_manual_retry below.
        """
        return self.save_recipient_outcome(
            recipient_id,
            state=RecipientState.QUEUED,
            now=now,
            next_attempt_at=next_attempt_at,
        )

    def reset_for_manual_retry(self, recipient_id: str, *, now: dt.datetime) -> ShareRecipient:
        """A human explicitly asked to retry a FAILED recipient (Scenario 8).

        Mints a *fresh* client_message_id, unlike requeue_recipient. A
        permanent failure (GROUP_NOT_FOUND, say) is a send a correctly
        idempotent provider will report as permanently failed forever under
        its original id -- see MockWhatsAppProvider's own caching of
        permanent outcomes. A manual retry is a new, deliberate decision to
        try again, not a replay of the attempt that already concluded, so it
        needs an id of its own.
        """
        row = self._session.get(ShareRecipientRow, recipient_id)
        if row is None:
            raise NotFoundError(
                f"Share recipient {recipient_id} does not exist.", recipient_id=recipient_id
            )
        row.client_message_id = new_id()
        row.state = RecipientState.QUEUED.value
        row.next_attempt_at = now
        row.error_code = None
        row.error_detail = ""
        row.updated_at = now
        self._session.flush()
        return _recipient_to_entity(row)


def _snapshot_to_entity(row: ReportSnapshotRow) -> ReportSnapshot:
    return ReportSnapshot(
        id=row.id,
        dataset_version=row.dataset_version,
        content_hash=row.content_hash,
        rendered_body=row.rendered_body,
        task_state=tuple(row.task_state),
        summary=dict(row.summary),
        template_id=row.template_id,
        created_at=row.created_at,
    )


def _job_to_entity(row: ShareJobRow) -> ShareJob:
    return ShareJob(
        id=row.id,
        display_id=row.display_id,
        approval_request_id=row.approval_request_id,
        snapshot_id=row.snapshot_id,
        approved_by_user_id=row.approved_by_user_id,
        scheduled_action_id=row.scheduled_action_id,
        action_version=row.action_version,
        state=ShareJobState(row.state),
        created_at=row.created_at,
        updated_at=row.updated_at,
        deferred_at=row.deferred_at,
        deferred_reason=DeferReason(row.deferred_reason) if row.deferred_reason else None,
        sent_at=row.sent_at,
    )


def _recipient_to_entity(row: ShareRecipientRow) -> ShareRecipient:
    return ShareRecipient(
        id=row.id,
        share_job_id=row.share_job_id,
        whatsapp_group_id=row.whatsapp_group_id,
        client_message_id=row.client_message_id,
        state=RecipientState(row.state),
        attempts=row.attempts,
        created_at=row.created_at,
        updated_at=row.updated_at,
        next_attempt_at=row.next_attempt_at,
        provider_message_id=row.provider_message_id,
        ack_level=row.ack_level,
        error_code=row.error_code,
        error_detail=row.error_detail,
        sent_at=row.sent_at,
    )
